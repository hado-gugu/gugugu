"""Gugugu: local pixels and reminders, no game input."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import sys
import time

IS_MACOS = sys.platform == "darwin"
if IS_MACOS:
    import mac_native as native
    from mac_capture import DisplayCapture
else:
    import native
    from vision import WindowCapture

    native.enable_dpi_awareness()

from PySide6.QtCore import Qt, QTimer, QRectF, QSize
from PySide6.QtGui import QPainter, QPixmap, QIcon, QFontDatabase
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QComboBox, QMessageBox, QToolButton)

from calibration import load_profiles, save_profile, profile_key, profile_matches, open_calibration
from planner import Catalog, PlannerStore
from vision import CaptureUnavailable
from border_vision import BorderVisionEngine as VisionEngine
from official_vision import OfficialShopEngine
from responsiveness import (name_frames_stable, confirm_independent_names,
                            filter_current_candidates, name_verification_regions, migrate_legacy_data)
from marker_stability import SlotAnchorTracker, anchor_border_candidates
from branding import APP_NAME, APP_VERSION, ICON_PATH
from performance_settings import load_settings, save_settings, resolve_ocr_threads
from opacity_settings import get_window_opacities, save_window_opacities
from window_opacity_dialog import WindowOpacityDialog

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DEFAULT_DATA_DIR = (Path.home() / "Library" / "Application Support" / "Gugugu"
                    if IS_MACOS else APP_ROOT / "data")
MAC_CAPTURE_SETTINGS = ("请在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」（旧版为「屏幕录制」）中开启权限。"
                        "授权后请完全退出并重新打开咕顾谷，再点「检测录屏」。")


def is_official_target(target):
    return Path((target or {}).get("exe", "")).name.casefold() == "arknights.exe"


def _display_context(geometry, pid):
    return (pid, tuple(geometry["client_screen_rect"]), geometry.get("display_pixel_size"),
            geometry.get("display_rotation"), geometry.get("profile_id"))

STYLE = """
QWidget { background:#101813; color:#e7f0e9; font:14px 'Microsoft YaHei UI'; }
QLabel { background:transparent; }
QPushButton,QComboBox,QLineEdit,QSpinBox { background:#202d25; border:1px solid #3a5141; padding:7px; min-height:22px; }
QPushButton:hover { border-color:#59f4ca; } QPushButton:disabled { color:#64796b; }
QPushButton#primary { background:#59f4ca; color:#08150e; border-color:#59f4ca; }
QPushButton#tutorialButton { color:#a9f5df; }
QToolButton#wingButton { background:transparent; border:1px solid transparent; border-radius:6px; padding:0; }
QToolButton#wingButton:hover, QToolButton#wingButton:focus { border-color:#59f4ca; }
QListWidget,QTreeWidget,QTextEdit,QPlainTextEdit { background:#0c130f; border:1px solid #34493c; }
QListWidget::item { padding:7px; } QListWidget::item:selected { background:#234d38; }
QTabBar::tab { background:#202d25; padding:9px; border:1px solid #34493c; }
QTabBar::tab:selected { background:#28513c; color:#59f4ca; }
QHeaderView::section { background:#202d25; color:#e7f0e9; border:0; padding:6px; }
QCheckBox { spacing:6px; } QToolTip { background:#202d25; color:#e7f0e9; border:1px solid #59f4ca; }
"""


class RecognitionPreparationError(RuntimeError):
    """Native inference configuration could not be prepared by the worker."""


class ReminderOverlay(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput)
        self.setWindowTitle(f"{APP_NAME} · 商店提醒")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if IS_MACOS:
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setStyleSheet("background: transparent; border: none;")
        self.setAutoFillBackground(False)
        self.marker_opacity = 1.0
        self.candidates = []
        self.art = {kind: QPixmap(str(RESOURCE_ROOT / "assets" / "markers" / f"{kind}.png"))
                    for kind in ("target", "transition")}
        self.native_status = {}

    def present(self, rectangle, candidates):
        self.candidates = list(candidates)
        if not candidates:
            self.hide()
            return
        left, top, right, bottom = rectangle
        if IS_MACOS:
            # The display rectangle is in global points, independent of capture pixels.
            self.setGeometry(left, top, right-left, bottom-top)
        if not self.isVisible():
            self.show()
            self.native_status = native.configure_overlay(int(self.winId()))
            if IS_MACOS and not self.native_status.get("click_through"):
                self.hide()
                raise native.NativeError("无法确认提醒层的鼠标穿透。")
        if not IS_MACOS:
            native.place_overlay(int(self.winId()), left, top, right-left, bottom-top)
        self.update()

    def clear(self):
        self.candidates = []
        self.hide()

    def set_marker_opacity(self, opacity):
        self.marker_opacity = opacity
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(self.marker_opacity)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for candidate in self.candidates:
            left, top = candidate.get("anchor_norm", candidate["box_norm"][:2])
            size = max(28., min(48., self.width() * .027))
            x = max(0., min(self.width()-size, left * self.width()))
            y = max(0., min(self.height()-size, top * self.height()-size-3))
            painter.drawPixmap(QRectF(x, y, size, size), self.art[candidate["reminder"]],
                               QRectF(self.art[candidate["reminder"]].rect()))


class Controller(QWidget):
    def __init__(self, data_dir=None):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(RESOURCE_ROOT / ICON_PATH)))
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(RESOURCE_ROOT / "catalog.json")
        self.store = PlannerStore(self.data_dir / "planner.json", self.catalog)
        self.calibration_path = self.data_dir / "calibration.json"
        self.settings_path = self.data_dir / "settings.json"
        self.performance_settings, self.settings_warnings = load_settings(self.settings_path)
        self.window_opacities = get_window_opacities(self.performance_settings)
        self.setWindowOpacity(self.window_opacities["controller"] / 100)
        self.requested_threads = resolve_ocr_threads(self.performance_settings["ocr_threads"])
        self.capture = None
        self.capture_running = False
        self.target = None
        self.profile = None
        if IS_MACOS:
            self.mac_capture_allowed = False
            self.mac_capture_verified = False
            self.mac_capture_denied = False
            self.capture_probe_requested = False
            self.pending_display = None
            self.display_switch_pending = False
            self.display_context = None
            self.engine_display_context = None
        self.engine = None
        self.engine_threads = None
        self.engine_official_mode = None
        self.applied_threads = {}
        self.editor = None
        self.lookup = None
        self.opacity_dialog = None
        self.tutorial_dialog = None
        self.wing_flight = None
        self.overlay = ReminderOverlay()
        self.overlay.set_marker_opacity(self.window_opacities["overlay"] / 100)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="visual-recognition")
        self.future = None
        self.job_kind = None
        self.job_generation = 0
        self.generation = 0
        self.scanning = False
        self.closing = False
        self.collapsed = False
        self.last_submit = 0.
        self.last_good = 0.
        self.last_sequence = -1
        self.previous_keys = set()
        self.marker_anchors = SlotAnchorTracker()
        self.uncertain_since = None
        self.matches = []
        self.last_observation = None
        self.calibration_requested = False
        self.diagnostics = {"capture": "ScreenCaptureKit" if IS_MACOS else "Windows Graphics Capture",
                            "frames": 0, "observations": 0}
        self.resize(370, 560)
        self.setMinimumWidth(340)
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(10)
        header = QHBoxLayout()
        self.wing_art = QPixmap(str(RESOURCE_ROOT / ICON_PATH))
        logo = QToolButton()
        logo.setObjectName("wingButton")
        logo.setIcon(QIcon(self.wing_art))
        logo.setIconSize(QSize(42, 42))
        logo.setFixedSize(46, 46)
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        logo.setToolTip("放飞萨科塔之翼")
        logo.setAccessibleName("放飞萨科塔之翼")
        logo.clicked.connect(self.launch_wing)
        header.addWidget(logo)
        self.heading = QLabel(APP_NAME)
        header.addWidget(self.heading, 1)
        tutorial_button = QPushButton("教程")
        tutorial_button.setObjectName("tutorialButton")
        tutorial_button.setToolTip("查看图文使用教程")
        tutorial_button.clicked.connect(self.open_tutorial)
        header.addWidget(tutorial_button)
        self.fold_button = QPushButton("收起")
        self.fold_button.clicked.connect(self.toggle_fold)
        header.addWidget(self.fold_button)
        self.layout.addLayout(header)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(QLabel("游戏 / 网页所在显示器" if IS_MACOS else "游戏 / 网页所在窗口"))
        source_row = QHBoxLayout()
        source_picker = QComboBox()
        source_picker.setMinimumContentsLength(15)
        source_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        source_row.addWidget(source_picker, 1)
        refresh = QPushButton("刷新显示器" if IS_MACOS else "刷新")
        source_row.addWidget(refresh)
        body.addLayout(source_row)
        if IS_MACOS:
            self.displays = source_picker
            self.displays.currentIndexChanged.connect(self.select_display)
            refresh.clicked.connect(self.refresh_displays)
            self.permission_button = QPushButton("授权屏幕录制")
            self.permission_button.clicked.connect(self.request_mac_capture_permission)
            self.capture_probe_button = QPushButton("检测录屏")
            self.capture_probe_button.setToolTip("读取一帧所选显示器画面，确认系统是否能取图。画面只在内存中使用。")
            self.capture_probe_button.clicked.connect(self.diagnose_capture)
            permission_row = QHBoxLayout()
            permission_row.addWidget(self.permission_button, 1)
            permission_row.addWidget(self.capture_probe_button, 1)
            body.addLayout(permission_row)
        else:
            self.windows = source_picker
            refresh.clicked.connect(self.refresh_windows)
            bind = QPushButton("绑定所选窗口")
            bind.clicked.connect(self.bind_selected)
            body.addWidget(bind)
        self.calibration_status = QLabel("默认使用主显示器。展开商店后，首次使用请手动框选。" if IS_MACOS
                                          else "首次使用先绑定窗口。网页商店需要手动框选。")
        self.calibration_status.setWordWrap(True)
        body.addWidget(self.calibration_status)
        self.calibrate_button = QPushButton("框选 / 重新校准商店")
        self.calibrate_button.clicked.connect(self.calibrate)
        body.addWidget(self.calibrate_button)
        body.addWidget(QLabel("当前配队预设"))
        self.presets = QComboBox()
        self.presets.currentIndexChanged.connect(self.select_preset)
        body.addWidget(self.presets)
        tool_row = QHBoxLayout()
        editor_button = QPushButton("配队预设")
        editor_button.clicked.connect(self.open_editor)
        tool_row.addWidget(editor_button, 1)
        lookup_button = QPushButton("干员速查")
        lookup_button.clicked.connect(self.open_lookup)
        tool_row.addWidget(lookup_button, 1)
        opacity_button = QPushButton("透明度")
        opacity_button.setToolTip("分别调整窗口透明度")
        opacity_button.clicked.connect(self.open_opacity_settings)
        tool_row.addWidget(opacity_button, 1)
        body.addLayout(tool_row)
        thread_row = QHBoxLayout()
        thread_row.addWidget(QLabel("识别线程"))
        self.thread_selector = QComboBox()
        self.thread_selector.setToolTip("调整文字识别的 CPU 线程。运行中修改会等待本轮结束，然后重新准备模型。")
        self.thread_selector.addItem(f"自动（{resolve_ocr_threads('auto')} 线程）", "auto")
        for count in (1, 2, 4, 8):
            self.thread_selector.addItem(f"{count} 线程", count)
        self.thread_selector.setCurrentIndex(self.thread_selector.findData(self.performance_settings["ocr_threads"]))
        self.thread_selector.currentIndexChanged.connect(self.change_thread_choice)
        thread_row.addWidget(self.thread_selector, 1)
        body.addLayout(thread_row)
        self.thread_status = QLabel()
        self.thread_status.setWordWrap(True)
        body.addWidget(self.thread_status)
        self.update_thread_hint()
        self.layout.addWidget(self.body)
        edition = "macOS 测试版" if IS_MACOS else "正式版"
        self.status = QLabel(f"{APP_VERSION} {edition} · 扫描已关闭")
        self.status.setWordWrap(True)
        self.layout.addWidget(self.status)
        self.power = QPushButton("开启扫描")
        self.power.setObjectName("primary")
        self.power.clicked.connect(self.toggle_scan)
        self.layout.addWidget(self.power)
        self.details = QLabel("在本机捕获所选显示器画面，仅识别框选范围。" if IS_MACOS
                              else "只读取所选窗口画面，不自动点击游戏。")
        self.details.setWordWrap(True)
        self.layout.addWidget(self.details)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.refresh_presets()
        if IS_MACOS:
            self.refresh_displays()
        else:
            self.refresh_windows()
        if self.settings_warnings:
            logging.warning("Performance settings: %s", self.settings_warnings)
            self.set_status("线程设置无法读取，已使用 2 线程；重新选择可保存。")

    def update_thread_hint(self):
        if self.engine_threads != self.requested_threads:
            suffix = "当前轮结束后应用" if self.scanning else "下次开启扫描时应用"
            self.thread_status.setText(f"将使用 {self.requested_threads} 线程 · {suffix}")
            return
        values = (self.applied_threads.get("det"), self.applied_threads.get("rec"))
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            if values == (0, 0):
                actual = "实际由运行库自动分配"
            elif values[0] == values[1]:
                actual = f"实际 {values[0]} 线程"
            else:
                actual = f"定位 {values[0] or '自动'} / 识字 {values[1] or '自动'} 线程"
        else:
            actual = "实际线程数未确认"
        self.thread_status.setText(f"已选 {self.requested_threads} 线程 · {actual}")

    def change_thread_choice(self, index):
        choice = self.thread_selector.itemData(index)
        previous = self.performance_settings["ocr_threads"]
        try:
            updated = save_settings(self.settings_path, {**self.performance_settings, "ocr_threads": choice})
            requested = resolve_ocr_threads(updated["ocr_threads"])
        except (OSError, ValueError, TypeError) as error:
            self.thread_selector.blockSignals(True)
            self.thread_selector.setCurrentIndex(self.thread_selector.findData(previous))
            self.thread_selector.blockSignals(False)
            self.set_status(f"线程设置未保存，保留原选项：{error}")
            return
        changed = requested != self.requested_threads
        self.performance_settings = updated
        self.requested_threads = requested
        if changed:
            # Native sessions are owned exclusively by the single worker. An
            # in-flight result belongs to the old generation and cannot re-mark.
            self.invalidate()
            if self.scanning:
                self.set_status("线程设置已保存，当前识别结束后应用……")
            else:
                self.set_status("线程设置已保存，下次开启扫描时应用。")
        self.update_thread_hint()

    def set_status(self, message):
        self.status.setText(message)
        if self.collapsed:
            preset = self.store.active()
            name = preset["name"] if preset else "无预设"
            self.heading.setText(f"{APP_NAME} · {name[:6]} · {'扫描中' if self.scanning else '已关闭'}")

    def refresh_windows(self):
        current = self.windows.currentData()
        self.windows.clear()
        try:
            available = native.list_windows()
        except Exception as error:
            self.set_status("无法列出窗口：" + str(error))
            return
        for window in available:
            self.windows.addItem(window["title"][:75], window["hwnd"])
        if current:
            index = self.windows.findData(current)
            if index >= 0:
                self.windows.setCurrentIndex(index)
        if not self.windows.count():
            self.set_status("未找到可见窗口，请打开游戏或网页后点刷新。")

    def refresh_displays(self):
        current = self.displays.currentData()
        try:
            self.mac_capture_allowed = native.has_capture_permission()
        except Exception as error:
            self.mac_capture_allowed = False
            logging.warning("macOS capture permission preflight failed: %s", error)
        try:
            available = native.list_displays()
        except Exception as error:
            self.update_display_controls()
            self.set_status(f"无法列出显示器：{error}")
            return
        self.displays.blockSignals(True)
        self.displays.clear()
        for display in available:
            self.displays.addItem(display["title"], display)
        current_id = current.get("display_id") if current else None
        default = next((i for i, display in enumerate(available) if display.get("is_main")), 0)
        index = next((i for i, display in enumerate(available) if display["display_id"] == current_id), default)
        self.displays.setCurrentIndex(index if available else -1)
        self.displays.blockSignals(False)
        self.select_display(self.displays.currentIndex())
        if not available:
            self.set_status("未找到可用显示器，请连接显示器后刷新。")
        elif self.mac_capture_denied:
            self.set_status(f"macOS 拒绝了录屏请求。{MAC_CAPTURE_SETTINGS}")
        elif not (self.mac_capture_allowed or self.mac_capture_verified):
            self.set_status("系统权限检查尚未确认授权。可点「授权屏幕录制」，也可点「检测录屏」检查取图。")

    def select_display(self, index):
        display = self.displays.itemData(index) if index >= 0 else None
        if not self.display_switch_pending:
            if display is None and self.target is None:
                self.update_display_controls()
                return
            if (display and self.target and display["display_id"] == self.target["display_id"]
                    and str(display["profile_id"]) == self.target["class_name"]):
                self.update_display_controls()
                return
        self.pending_display = display
        self.display_switch_pending = True
        self.calibration_requested = False
        self.capture_probe_requested = False
        self.stop_scan()
        self.finish_display_switch()
        if self.display_switch_pending:
            self.set_status("正在结束当前识别，随后切换显示器……")
        self.update_display_controls()

    def finish_display_switch(self):
        if not self.display_switch_pending or self.future:
            return
        self.request_capture_stop()
        if self.future:
            return
        display, self.pending_display = self.pending_display, None
        self.display_switch_pending = False
        self.display_context = None
        self.capture = self.target = self.profile = None
        if display:
            self.capture = DisplayCapture(int(display["display_id"]))
            self.target = {"display_id": display["display_id"], "title": display["title"],
                           "exe": "macos-display", "class_name": str(display["profile_id"])}
            try:
                self.profile = load_profiles(self.calibration_path)["profiles"].get(profile_key(self.target))
            except (ValueError, OSError) as error:
                self.set_status(str(error))
            range_status = ("已载入此显示器的商店范围。" if self.profile
                            else "请展开商店，再手动框选整排卡片。")
            self.calibration_status.setText(f"{display['title']}\n{range_status}")
        else:
            self.calibration_status.setText("没有可用显示器。")
        self.update_display_controls()

    def update_display_controls(self):
        self.permission_button.setVisible(self.mac_capture_denied or
                                          not (self.mac_capture_allowed or self.mac_capture_verified))
        probing = self.capture_probe_requested or self.job_kind == "capture_probe"
        available = self.capture is not None and not self.display_switch_pending and not probing
        self.capture_probe_button.setEnabled(available)
        self.calibrate_button.setEnabled(available)
        self.power.setEnabled(available and self.profile is not None)

    def request_mac_capture_permission(self):
        try:
            if native.request_capture_permission():
                self.mac_capture_allowed = True
                self.set_status("系统权限检查已通过。若刚授权，请完全退出并重新打开咕顾谷，再点「检测录屏」。")
            else:
                self.mac_capture_allowed = False
                self.set_status(f"系统权限检查尚未确认授权。{MAC_CAPTURE_SETTINGS}")
            self.update_display_controls()
        except Exception as error:
            self.set_status("无法请求屏幕录制权限：" + str(error))

    def diagnose_capture(self):
        if not self.capture or self.display_switch_pending:
            self.set_status("未找到可用显示器，请刷新显示器。")
            return
        self.calibration_requested = False
        self.stop_scan()
        if self.future:
            self.capture_probe_requested = True
            self.set_status("正在结束当前操作，随后检测录屏……")
        else:
            self.capture_probe_requested = False
            self.set_status("正在读取一帧所选显示器画面，检测录屏……")
            self.submit("capture_probe", self.probe_capture)
        self.update_display_controls()

    def probe_capture(self):
        capture = self.capture
        try:
            frame = self.get_frame()
            height, width = frame.image_bgr.shape[:2]
            return width, height
        finally:
            self.stop_capture_worker(capture)

    def confirm_mac_capture(self):
        if not self.mac_capture_verified or self.mac_capture_denied:
            self.mac_capture_verified = True
            self.mac_capture_denied = False
            self.update_display_controls()

    def bind_selected(self):
        hwnd = self.windows.currentData()
        if not hwnd:
            self.set_status("请先选择一个可见窗口。")
            return
        try:
            self.bind_target(hwnd)
        except Exception as error:
            self.set_status(str(error))

    def bind_target(self, hwnd):
        self.stop_scan()
        if self.future:
            raise RuntimeError("上一次识别正在结束，请稍后再绑定。")
        if self.capture:
            self.stop_capture_worker(self.capture)
        if self.engine is not None:
            self.engine.reset_layout()
        self.target = native.window_identity(int(hwnd))
        try:
            window_info = native.get_window_info(int(hwnd))
            self.target["title"] = window_info.get("title", "所选窗口")
        except Exception:
            window_info = {}
            self.target["title"] = "所选窗口"
        self.capture = WindowCapture(int(hwnd))
        try:
            self.profile = load_profiles(self.calibration_path)["profiles"].get(profile_key(self.target))
        except (ValueError, OSError) as error:
            self.profile = None
            self.set_status(str(error))
        if (self.profile is None and is_official_target(self.target) and window_info.get("client_screen_rect")):
            left, top, right, bottom = window_info["client_screen_rect"]
            self.profile = {"roi": [0.0, .73, 1.0, 1.0], "reference_size": [right-left, bottom-top],
                            "auto_official": True}
        title = self.target.get("title", "所选窗口")
        if self.profile and self.profile.get("auto_official"):
            range_status = "官方商店自动定位，展开后可开启扫描。"
        else:
            range_status = "已载入上次范围；可以开启扫描。" if self.profile else "尚未保存范围，请框选商店。"
            range_status += "\n框选请包含完整卡片边缘，并向左留出升级空间。"
        self.calibration_status.setText(f"已绑定：{title[:34]}\n{range_status}")
        self.set_status("已绑定窗口 · 扫描已关闭")

    def get_frame(self):
        if self.closing:
            raise RuntimeError("程序正在关闭。")
        if not self.capture:
            raise RuntimeError("未找到可用显示器，请刷新显示器。" if IS_MACOS else "请先绑定窗口。")
        self.capture.start()
        self.capture_running = True
        deadline = time.monotonic() + 5
        retryable = ("no_frame", "starting", "frame_unavailable", "capture_starting", "frame_pending", "window_resizing")
        if IS_MACOS:
            retryable += ("display_reconfiguring", "capture_timeout")
        while True:
            if self.closing:
                self.capture.stop()
                raise RuntimeError("程序正在关闭。")
            try:
                return self.capture.latest_frame(max_age_ms=2000)
            except CaptureUnavailable as error:
                if error.code == "frame_stale" and time.monotonic() < deadline:
                    self.capture.stop()
                    self.capture.start()
                elif error.code not in retryable or time.monotonic() >= deadline:
                    raise
                time.sleep(.06)

    def submit(self, kind, function):
        if self.future:
            return False
        self.job_kind = kind
        self.job_generation = self.generation
        self.future = self.executor.submit(function)
        self.last_submit = time.monotonic()
        return True

    def calibrate(self):
        if IS_MACOS and self.display_switch_pending:
            self.set_status("显示器正在切换，请稍后框选商店。")
            return
        if IS_MACOS and (self.capture_probe_requested or self.job_kind == "capture_probe"):
            self.set_status("正在检测录屏，请稍后框选商店。")
            return
        if not self.capture:
            self.set_status("未找到可用显示器，请刷新显示器。" if IS_MACOS else "请先绑定游戏 / 网页窗口。")
            return
        self.stop_scan()
        if self.future:
            self.calibration_requested = True
            self.set_status("正在结束本轮识别，随后打开校准画面……")
            return
        self.set_status("正在获取所选显示器画面……" if IS_MACOS else "正在获取所选窗口画面……")
        self.submit("calibration", self.calibration_frame)

    def stop_capture_worker(self, capture):
        try:
            capture.stop()
        finally:
            if self.capture is capture:
                self.capture_running = False

    def request_capture_stop(self):
        if self.capture and self.capture_running and self.future is None:
            capture = self.capture
            self.submit("stop_capture", lambda: self.stop_capture_worker(capture))

    def calibration_frame(self):
        capture = self.capture
        frame = self.get_frame()
        self.stop_capture_worker(capture)
        return frame

    def apply_calibration(self, roi, size):
        if not self.target:
            raise ValueError("未找到可用显示器，请刷新显示器。" if IS_MACOS else "请先绑定窗口。")
        profile = save_profile(self.calibration_path, self.target, roi, size)
        self.profile = profile
        if self.engine is not None:
            self.engine.reset_layout()
        self.invalidate()
        if IS_MACOS:
            self.calibration_status.setText(f"{self.target['title']}\n商店范围已保存。商店位置或大小变化后请重新框选。")
            self.update_display_controls()
        else:
            self.calibration_status.setText("商店范围已保存。窗口移动和等比例缩放时自动跟随。")

    def refresh_presets(self):
        self.presets.blockSignals(True)
        self.presets.clear()
        state = self.store.state
        for preset in state["presets"]:
            self.presets.addItem(preset["name"], preset["id"])
        index = self.presets.findData(state["activeId"])
        self.presets.setCurrentIndex(index)
        self.presets.blockSignals(False)

    def select_preset(self, index):
        preset_id = self.presets.itemData(index)
        if preset_id and preset_id != self.store.state["activeId"]:
            try:
                self.store.select_preset(preset_id)
                self.on_planner_changed()
                if self.editor:
                    self.editor.refresh()
            except Exception as error:
                QMessageBox.warning(self, "配队未保存", str(error))

    def on_planner_changed(self):
        self.refresh_presets()
        self.invalidate()
        self.set_status("预设已更新，正在重新识别……" if self.scanning else "预设已更新 · 扫描已关闭")

    def open_editor(self):
        if self.editor is None:
            from editor import PresetEditor
            self.editor = PresetEditor(self.store, self.catalog, self.on_planner_changed, RESOURCE_ROOT)
            self.editor.setWindowOpacity(self.window_opacities["planner"] / 100)
        self.editor.refresh()
        self.editor.show()
        self.editor.raise_()
        self.editor.activateWindow()

    def open_lookup(self):
        if self.lookup is None:
            from officer_lookup import OperatorLookup
            self.lookup = OperatorLookup(self.catalog, RESOURCE_ROOT)
            self.lookup.setWindowOpacity(self.window_opacities["lookup"] / 100)
            self.lookup.detail.setWindowOpacity(self.window_opacities["detail"] / 100)
        self.lookup.show()
        self.lookup.raise_()
        self.lookup.activateWindow()

    def open_opacity_settings(self):
        if self.opacity_dialog is None:
            self.opacity_dialog = WindowOpacityDialog(self, self.preview_window_opacity, self.save_opacities)
        if not self.opacity_dialog.isVisible():
            self.opacity_dialog.load_values(get_window_opacities(self.performance_settings))
            self.opacity_dialog.show()
        self.opacity_dialog.raise_()
        self.opacity_dialog.activateWindow()

    def open_tutorial(self):
        if self.tutorial_dialog is None:
            from tutorial import TutorialDialog
            self.tutorial_dialog = TutorialDialog(self, RESOURCE_ROOT, macos=IS_MACOS)
            self.tutorial_dialog.setWindowOpacity(self.window_opacities["tutorial"] / 100)
        self.tutorial_dialog.show()
        self.tutorial_dialog.raise_()
        self.tutorial_dialog.activateWindow()

    def launch_wing(self):
        if self.wing_flight is not None or self.wing_art.isNull():
            return
        from wing_flight import WingFlight
        flight = WingFlight(self.wing_art, self.screen(), self)
        self.wing_flight = flight
        flight.finished.connect(self._wing_finished)
        flight.finished.connect(flight.deleteLater)
        if not flight.start():
            self._wing_finished()
            flight.deleteLater()

    def _wing_finished(self):
        self.wing_flight = None

    def preview_window_opacity(self, key, percent):
        self.window_opacities[key] = percent
        opacity = percent / 100
        if key == "overlay":
            self.overlay.set_marker_opacity(opacity)
            return
        window = {
            "controller": self,
            "planner": self.editor,
            "lookup": self.lookup,
            "detail": self.lookup.detail if self.lookup is not None else None,
            "tutorial": self.tutorial_dialog,
        }.get(key)
        if window is not None:
            window.setWindowOpacity(opacity)

    def save_opacities(self, changes):
        self.performance_settings = save_window_opacities(self.settings_path, changes)

    def toggle_fold(self):
        self.collapsed = not self.collapsed
        self.body.setVisible(not self.collapsed)
        self.details.setVisible(not self.collapsed)
        self.fold_button.setText("展开" if self.collapsed else "收起")
        if not self.collapsed:
            self.heading.setText(APP_NAME)
        self.set_status(self.status.text())
        self.adjustSize()

    def invalidate(self):
        self.generation += 1
        self.previous_keys.clear()
        self.matches = []
        self.last_good = 0
        self.last_sequence = -1
        self.marker_anchors.reset()
        self.uncertain_since = None
        self.overlay.clear()

    def start_scan(self):
        self.calibration_requested = False
        if IS_MACOS and self.display_switch_pending:
            self.set_status("显示器正在切换，请稍后开启扫描。")
            return False
        if IS_MACOS and (self.capture_probe_requested or self.job_kind == "capture_probe"):
            self.set_status("正在检测录屏，请稍后开启扫描。")
            return False
        if not self.target or not self.profile:
            self.set_status("请在所选显示器上展开并框选商店。" if IS_MACOS else "请先绑定窗口并框选商店。")
            return False
        self.invalidate()
        self.scanning = True
        self.power.setText("关闭扫描")
        self.set_status("正在初始化本地识别……")
        self.last_submit = 0
        self.update_thread_hint()
        return True

    def stop_scan(self):
        self.scanning = False
        self.invalidate()
        self.power.setText("开启扫描")
        self.set_status("扫描已关闭")
        self.request_capture_stop()
        self.update_thread_hint()

    def toggle_scan(self):
        self.stop_scan() if self.scanning else self.start_scan()

    @staticmethod
    def check_display_frame(frame, expected):
        context = _display_context(frame.geometry, frame.geometry.get("foreground_pid"))
        if context != expected:
            raise CaptureUnavailable("display_reconfiguring", "前台应用或显示器已变化，正在等待新画面。")

    def recognize(self):
        job_started = time.perf_counter()
        preparation_ms = 0.0
        passes = []

        def record_pass(label, result):
            diagnostics = result.get("diagnostics", {})
            passes.append({"pass": label, "sequence": result["sequence"],
                           "path": diagnostics.get("path"),
                           "full_refresh_reason": diagnostics.get("full_refresh_reason"),
                           "det_ms": diagnostics.get("det_ms", 0),
                           "rec_ms": diagnostics.get("rec_ms", 0),
                           "geometry_source": diagnostics.get("geometry_source"),
                           "geometry_ms": diagnostics.get("geometry_ms", 0),
                           "slot_count": diagnostics.get("slot_count", 0),
                           "changed_slots": diagnostics.get("changed_slots", 0),
                           "reused_slots": diagnostics.get("reused_slots", 0),
                           "elapsed_ms": result["elapsed_ms"],
                           "ocr_reused": result.get("ocr_reused", False)})

        profile = self.profile.copy()
        display_context = self.display_context if IS_MACOS else None
        requested_threads = self.requested_threads
        official_mode = None
        if IS_MACOS:
            # Detect official cards by their pixels, regardless of launcher.
            official_mode = "manual"
        elif is_official_target(self.target):
            official_mode = "auto" if profile.get("auto_official") else "manual"
        if (self.engine is None or self.engine_threads != requested_threads
                or self.engine_official_mode != official_mode):
            preparation_started = time.perf_counter()
            try:
                obsolete, self.engine = self.engine, None
                self.engine_threads = None
                self.engine_official_mode = None
                self.applied_threads = {}
                if obsolete is not None:
                    obsolete.close()
                options = {"min_score": .85, "intra_op_num_threads": requested_threads}
                if official_mode is None:
                    self.engine = VisionEngine(self.catalog.recognized_names, **options)
                else:
                    self.engine = OfficialShopEngine(self.catalog.recognized_names,
                                                     allow_legacy=official_mode == "manual", **options)
                self.engine_threads = requested_threads
                self.engine_official_mode = official_mode
                info = getattr(self.engine, "model_info", {})
                self.applied_threads = dict(info.get("effective_threads", {})) if isinstance(info, dict) else {}
            except Exception as error:
                raise RecognitionPreparationError(f"无法应用 {requested_threads} 线程，请调整后重新开启扫描：{error}") from error
            preparation_ms = (time.perf_counter()-preparation_started)*1000
        if IS_MACOS and self.engine_display_context != display_context:
            self.engine.reset_layout()
            self.engine_display_context = display_context
        # Get the source AFTER cold model load, so initialization does not age it.
        capture_started = time.perf_counter()
        frame = self.get_frame()
        if IS_MACOS:
            self.check_display_frame(frame, display_context)
        capture_wait_ms = (time.perf_counter()-capture_started)*1000
        height, width = frame.image_bgr.shape[:2]
        if not profile_matches(profile, width, height):
            raise RuntimeError("显示器比例已变化，请重新框选商店。" if IS_MACOS
                               else "窗口比例已变化，请重新框选商店。")
        observation = self.engine.analyze(frame.image_bgr, profile["roi"], sequence=frame.sequence, timestamp=frame.timestamp)
        record_pass("source", observation)
        observation["frame_stable"] = None
        if observation.get("candidates"):
            try:
                latest = self.capture.latest_frame(max_age_ms=1500)
                if IS_MACOS:
                    self.check_display_frame(latest, display_context)
                stable = name_frames_stable(frame, latest, name_verification_regions(observation))
                observation["frame_stable"] = stable
                if stable is False:
                    # Re-read the newer frame once; a single changed card must
                    # not invalidate already confirmed names in other slots.
                    current = self.engine.analyze(latest.image_bgr, profile["roi"],
                                                  sequence=latest.sequence, timestamp=latest.timestamp)
                    record_pass("latest", current)
                    current["frame_stable"] = None
                    current["candidates"] = confirm_independent_names(observation, current)
                    current["diagnostics"]["verification"] = "independent_name_reads"
                    current["elapsed_ms"] += observation["elapsed_ms"]
                    if current["candidates"]:
                        after = self.capture.latest_frame(max_age_ms=1500)
                        if IS_MACOS:
                            self.check_display_frame(after, display_context)
                        after_stable = name_frames_stable(latest, after, name_verification_regions(current))
                        current["frame_stable"] = after_stable
                        if after_stable is False:
                            current["candidates"] = filter_current_candidates(current, latest, after)
                            current["frame_stable"] = None
                            if not current["candidates"]:
                                current["status"] = "no_candidates"
                                current["error"] = "名称已变化，正在读取最新商店……"
                    # Briefly unreadable B may be a transition. Explicit B/C
                    # changes above have already removed their affected slots.
                    if current.get("status") == "no_candidates":
                        if not current.get("error"):
                            changed_border = (current.get("diagnostics", {}).get("geometry_source") == "borders"
                                              and current.get("diagnostics", {}).get("changed_slots", 0) > 0)
                            current["transient_uncertain"] = not changed_border
                            current["error"] = "名称暂时无法确认，正在核对最新画面……"
                    observation = current
            except CaptureUnavailable as error:
                if IS_MACOS and error.code == "permission_denied":
                    raise
                observation["status"] = "frame_stale"
                observation["error"] = ("显示器画面不可用，等待新画面。" if IS_MACOS
                                        else "窗口画面不可用，等待新画面。")
                observation["candidates"] = []
        if IS_MACOS:
            observation["display_context"] = display_context
        observation["recognition_passes"] = passes
        observation["model_preparation_ms"] = preparation_ms
        observation["capture_wait_ms"] = capture_wait_ms
        observation["job_elapsed_ms"] = (time.perf_counter()-job_started)*1000
        observation["ocr_threads"] = requested_threads
        observation["effective_threads"] = dict(self.applied_threads)
        return observation

    def accept_observation(self, observation):
        self.update_thread_hint()
        self.last_observation = observation
        self.diagnostics["observations"] += 1
        changed_border = (observation.get("diagnostics", {}).get("geometry_source") == "borders"
                          and observation.get("diagnostics", {}).get("changed_slots", 0) > 0)
        if (observation.get("transient_uncertain") and not changed_border and self.matches
                and time.monotonic()-observation["timestamp"] < 1.5):
            if self.uncertain_since is None:
                self.uncertain_since = time.monotonic()
            self.expire_uncertain()
            self.set_status(observation.get("error") or "正在核对名称……")
            return
        self.uncertain_since = None
        if observation.get("status") in ("error", "blank_frame", "frame_stale", "scene_changed") or observation.get("frame_stable") is False:
            self.previous_keys.clear()
            self.matches = []
            self.last_good = 0
            self.overlay.clear()
            if observation.get("status") == "scene_changed" and observation.get("layout_source") == "official_shop":
                self.details.setText("当前识别：等待展开商店。")
            self.set_status(observation.get("error") or "当前画面无法确认，等待新画面。")
            return
        if observation["sequence"] == self.last_sequence:
            return
        self.last_sequence = observation["sequence"]
        if time.monotonic() - observation["timestamp"] > 3.5:
            self.previous_keys.clear()
            self.matches = []
            self.last_good = 0
            self.overlay.clear()
            self.set_status("识别结果已过期，正在等待新画面。")
            return
        keys, matches, names = set(), [], []
        pending_reminders = 0
        state = self.store.state
        resolved = []
        for candidate in observation["candidates"]:
            records = self.catalog.resolve_name(candidate["text"])
            if len(records) != 1:
                continue
            record = records[0]
            resolved.append({**candidate, **record})
        source_id = "display_id" if IS_MACOS else "hwnd"
        context = (self.target.get(source_id) if self.target else None,
                   tuple(self.profile.get("roi", [])) if self.profile else ())
        diagnostics = observation.get("diagnostics", {})
        if diagnostics.get("geometry_source") == "borders":
            anchored = anchor_border_candidates(resolved, layout_key=diagnostics.get("layout_key"))
        else:
            anchored = self.marker_anchors.update(resolved, context_key=context)
        for candidate in anchored:
            names.append(candidate["name"])
            key = (candidate["kind"], candidate["id"], candidate["slot_anchor_id"])
            keys.add(key)
            reminder = self.catalog.reminder(state, candidate["kind"], candidate["id"])
            fast_confirmed = (observation.get("frame_stable") is True
                              or candidate.get("independently_confirmed") is True
                              or candidate.get("frame_verified") is True) and candidate["score"] >= .95
            if reminder:
                if fast_confirmed or key in self.previous_keys:
                    matches.append({**candidate, "reminder": reminder})
                else:
                    pending_reminders += 1
        self.previous_keys = keys
        self.matches = matches
        self.last_good = time.monotonic()
        self.details.setText("当前识别：" + ("、".join(names) if names else "未确认名称；不会强制匹配预设。"))
        path = observation.get("diagnostics", {}).get("path", "")
        processing_ms = observation.get("job_elapsed_ms", observation["elapsed_ms"])
        if observation.get("model_preparation_ms", 0) > 0:
            timing = f"模型准备 {processing_ms:.0f} ms"
        elif len(observation.get("recognition_passes", [])) > 1:
            timing = f"刷新复核 {processing_ms:.0f} ms"
        elif path.startswith("unchanged_"):
            timing = "画面未变"
        elif path == "name_band_detection":
            timing = f"名称识别 {processing_ms:.0f} ms"
        else:
            timing = f"定位识别 {processing_ms:.0f} ms"
        if diagnostics.get("geometry_source") == "borders":
            count = diagnostics.get("slot_count", 0)
            changed = diagnostics.get("changed_slots", 0)
            if path.startswith("unchanged_"):
                timing = f"边框 {count} 槽 · 画面未变"
            elif observation.get("model_preparation_ms", 0) > 0:
                timing = f"边框 {count} 槽 · 模型准备 {processing_ms:.0f} ms"
            elif len(observation.get("recognition_passes", [])) > 1:
                timing = f"边框 {count} 槽 · 变化复核 {processing_ms:.0f} ms"
            else:
                timing = f"边框 {count} 槽 · 更新 {changed} 槽 · {processing_ms:.0f} ms"
        elif diagnostics.get("geometry_source") == "fallback":
            timing = "兼容路径 · " + timing
        if diagnostics.get("theme"):
            timing = f"{diagnostics['theme']} · {timing}"
        status = f"扫描中 · {len(names)} 个名称 · {len(matches)} 个提醒"
        if not state["enabled"]:
            status += " · 局内提醒已关闭"
        elif pending_reminders:
            status += f" · {pending_reminders} 个等待复核"
        elif names and not matches:
            status += " · 未匹配提醒名单"
        self.set_status(f"{status} · {timing}")

    def expire_uncertain(self):
        if self.uncertain_since is not None and time.monotonic()-self.uncertain_since >= .35:
            self.matches = []
            self.previous_keys.clear()
            self.last_good = 0
            self.overlay.clear()

    def read_display_context(self):
        geometry = native.get_display_geometry(self.target["display_id"])
        if str(geometry["profile_id"]) != self.target["class_name"]:
            raise native.NativeError("显示器已更换，请刷新后重新选择。")
        context = _display_context(geometry, native.foreground_process_id())
        if context != self.display_context:
            self.display_context = context
            self.invalidate()
            self.last_submit = 0
            self.details.setText("等待所选显示器的商店画面。")
        return context

    def tick(self):
        if self.closing:
            return
        self.expire_uncertain()
        display_context = None
        if IS_MACOS and self.scanning and self.target:
            try:
                display_context = self.read_display_context()
            except Exception as error:
                if self.display_context is not None:
                    self.display_context = None
                    self.invalidate()
                self.overlay.clear()
                self.set_status(f"当前显示器不可用，请刷新显示器：{error}")
        if self.future and self.future.done():
            future, kind, generation = self.future, self.job_kind, self.job_generation
            self.future, self.job_kind = None, None
            try:
                result = future.result()
                if generation == self.generation:
                    if kind == "calibration":
                        if IS_MACOS:
                            self.confirm_mac_capture()
                        roi = open_calibration(result.image_bgr, self.profile.get("roi") if self.profile else None,
                                               self, opacity=self.window_opacities["calibration"] / 100)
                        if roi:
                            self.apply_calibration(roi, [result.image_bgr.shape[1], result.image_bgr.shape[0]])
                            self.set_status("范围已保存，可以开启扫描。")
                        else:
                            self.set_status("已取消框选，原保存范围保留。")
                    elif kind == "capture_probe":
                        self.confirm_mac_capture()
                        width, height = result
                        self.set_status(f"录屏正常 · 已取到 {width} × {height} 像素的画面。")
                    elif kind == "recognition" and self.scanning:
                        if not IS_MACOS or (display_context and display_context[0]
                                            and display_context[0] != os.getpid()
                                            and result.get("display_context") == display_context):
                            if IS_MACOS:
                                self.confirm_mac_capture()
                            self.accept_observation(result)
                        else:
                            self.invalidate()
            except Exception as error:
                logging.exception("Background operation failed")
                if IS_MACOS and isinstance(error, CaptureUnavailable) and error.code == "permission_denied":
                    # The system prompt itself can change foreground focus while the capture is in flight.
                    self.stop_scan()
                    self.calibration_requested = False
                    self.capture_probe_requested = False
                    self.mac_capture_verified = False
                    self.mac_capture_denied = True
                    self.update_display_controls()
                    self.set_status(f"macOS 拒绝了录屏请求。{MAC_CAPTURE_SETTINGS}")
                elif generation == self.generation:
                    if isinstance(error, RecognitionPreparationError):
                        self.stop_scan()
                    self.previous_keys.clear()
                    self.matches = []
                    self.overlay.clear()
                    if kind == "capture_probe":
                        self.set_status(f"检测录屏失败：{error}")
                    else:
                        self.set_status(str(error))
            if IS_MACOS and kind == "capture_probe":
                self.update_display_controls()
            if not self.scanning:
                self.request_capture_stop()
            if self.calibration_requested and not self.closing:
                self.calibration_requested = False
                QTimer.singleShot(0, self.calibrate)
        if IS_MACOS and self.display_switch_pending:
            self.finish_display_switch()
        if IS_MACOS and self.capture_probe_requested and self.future is None:
            self.diagnose_capture()
        if not self.scanning or not self.target:
            self.overlay.clear()
            return
        try:
            if IS_MACOS:
                if display_context is None:
                    return
                pid, rect = display_context[:2]
                if not pid or pid == os.getpid():
                    self.overlay.clear()
                    self.set_status("扫描已开启 · 切回游戏或网页后继续。")
                    return
                front = True
            else:
                valid, reason = native.validate_target(self.target)
                if not valid:
                    self.overlay.clear()
                    self.previous_keys.clear()
                    self.matches = []
                    self.set_status(reason)
                    return
                geometry = native.get_window_geometry(self.target["hwnd"])
                rect = geometry["client_screen_rect"]
            if not profile_matches(self.profile, rect[2]-rect[0], rect[3]-rect[1]):
                self.overlay.clear()
                self.previous_keys.clear()
                self.matches = []
                self.set_status("显示器比例已变化，请重新框选商店。" if IS_MACOS
                                else "窗口比例已变化，请重新框选商店。")
                return
            if not IS_MACOS:
                front = native.foreground_belongs_to_target(self.target, allowed_hwnds=(int(self.winId()),),
                                                            allow_same_process=False)
            fresh = self.last_good and time.monotonic()-self.last_good < 3.5
            if front and fresh and self.store.state["enabled"]:
                self.overlay.present(rect, self.matches)
            else:
                self.overlay.clear()
            if self.future is None and time.monotonic()-self.last_submit >= .12:
                self.submit("recognition", self.recognize)
        except Exception as error:
            self.overlay.clear()
            self.set_status(str(error))

    def closeEvent(self, event):
        self.closing = True
        self.scanning = False
        self.overlay.clear()
        self.timer.stop()
        if self.capture:
            capture = self.capture
            self.executor.submit(lambda: self.stop_capture_worker(capture))
        if self.editor:
            self.editor.hide()
        if self.lookup:
            self.lookup.close()
        if self.opacity_dialog:
            self.opacity_dialog.close()
        if self.tutorial_dialog:
            self.tutorial_dialog.close()
        if self.wing_flight:
            self.wing_flight.close()
        # The queued capture shutdown must run after any in-flight OCR work.
        self.executor.shutdown(wait=False, cancel_futures=False)
        event.accept()
        QApplication.instance().quit()


def main():
    parser = argparse.ArgumentParser(prog=APP_NAME, description=f"{APP_NAME} · 卫戍协议配队提醒")
    parser.add_argument("--data-dir")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if IS_MACOS and args.self_test:
        parser.error("--self-test is available on Windows only")
    data_dir = Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    migration = {"copied": [], "warnings": []}
    if not IS_MACOS and not args.data_dir and not args.self_test:
        for legacy_name in ("Gugugu100", "Gugugu047TitleFix", "Gugugu047Tutorial", "Gugugu047Themes",
                            "Gugugu047Opacity", "Gugugu047Lookup",
                            "Gugugu047Official", "Gugugu047Regulation",
                            "Gugugu047Threads", "Gugugu048", "Gugugu047", "Gugugu045", "Gugugu",
                            "StrongholdVisionAssistantRefresh", "StrongholdVisionAssistantStable",
                            "StrongholdVisionAssistantFast", "StrongholdVisionAssistant"):
            copied = migrate_legacy_data(data_dir, APP_ROOT.parent / legacy_name / "data")
            migration["copied"].extend(copied["copied"])
            migration["warnings"].extend(copied["warnings"])
    logging.basicConfig(filename=str(data_dir / "errors.log"), level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    app = QApplication(sys.argv[:1])
    if not any("YaHei" in name for name in QFontDatabase.families()):
        font = Path(r"C:\Windows\Fonts\msyh.ttc")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(str(RESOURCE_ROOT / ICON_PATH)))
    app.setStyleSheet(STYLE)
    window = Controller(data_dir)
    if migration["warnings"]:
        logging.warning("Previous version data could not be copied: %s", migration["warnings"])
        window.set_status("部分旧配置未自动载入，原文件保留，可手动导入。")
    window.show()
    if args.self_test:
        from qa import run_self_test
        run_self_test(window, app)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.exception("Application startup failed")
        raise
