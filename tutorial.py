"""Illustrated quick-start guide for the companion."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from branding import APP_NAME


_STEPS = (
    (
        "1 绑定窗口",
        "选择游戏所在的窗口",
        "bind.png",
        "在窗口下拉框选中明日方舟，再点绑定所选窗口。",
        "进入卫戍协议对局并展开商店。回到咕顾谷，点「刷新」，选中「明日方舟」，再点「绑定所选窗口」。",
        "卫戍协议终端、乌萨斯齐心团和可露希尔可以自动定位。提示需要框选时，打开第 4 页。",
    ),
    (
        "2 配置提醒",
        "选好要提醒的干员和装备",
        "preset.png",
        "配队预设窗口中的新建、导入配队、局内提醒及三个分类。",
        "打开「配队预设」。点「＋ 新建」并选择「目标干员」和「目标装备」。已有配队文件时，点「导入配队」，选择 JSON 文件后点「追加导入」。",
        "勾选「局内提醒」，再回到主控的「当前配队预设」确认选中了这一套。",
    ),
    (
        "3 开启扫描",
        "切回游戏看卡片提醒",
        "scan.png",
        "开启扫描后，爱心标出目标，拇指标出过渡干员。",
        "点主控的「开启扫描」，再切回游戏。爱心标记目标干员和装备，拇指标记过渡干员。商店收起时，提醒会隐藏。",
        "过渡干员在配队的「全局过渡」中设置。识别到名称却没看到标记时，检查当前配队和「局内提醒」。",
    ),
    (
        "4 手动框选",
        "需要校准时，框住整排卡片",
        "calibrate.png",
        "框选包含升级区和完整卡片边缘，并给左边留出新槽位空间。",
        "保持商店展开，在主控点「框选 / 重新校准商店」。框住所有卡片边缘，包含升级区并向左留出新槽位的空间。点「保存范围」，再开启扫描。",
        "适用于提示重新框选、旧范围截掉卡槽，或首次设置网页商店。",
    ),
)

_MAC_STEPS = (
    (
        "1 选择显示器",
        "在要识别的显示器上打开游戏",
        None,
        "",
        "在游戏或网页中进入卫戍协议对局，展开商店。咕顾谷默认使用主显示器。"
        "如果商店在另一块屏幕，从显示器下拉框切换。连接新显示器后点「刷新显示器」。",
        "首次使用需要授权屏幕录制。授权后完全退出并重新打开咕顾谷，再点「检测录屏」确认能取到画面。"
        "检测只读取一帧，用于确认捕获状态。",
    ),
    (
        "2 框选商店",
        "框住整排卡片",
        None,
        "",
        "保持商店在所选显示器上可见，点「框选 / 重新校准商店」。在显示器画面中框住所有卡片边缘，"
        "包含升级区并向左留出新槽位的空间，然后点「保存范围」。",
        "每块显示器分别保存范围。商店在同一显示器内移位、改变大小或切换全屏后，请重新框选。",
    ),
    ("3 配置提醒", *_STEPS[1][1:]),
    (
        "4 开启扫描",
        "回到游戏看卡片提醒",
        None,
        "",
        "点主控的「开启扫描」，再回到游戏或网页。爱心标出目标干员和装备，拇指标出过渡干员。",
        "切换前台应用时会先清除旧标记，再按当前画面识别。请让商店保持可见。",
    ),
)


def _label(text, name=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    return label


class _Illustration(QLabel):
    def __init__(self, pixmap, description):
        super().__init__()
        self._original = pixmap
        self.setAccessibleName(description)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(208)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        ratio = self.devicePixelRatioF()
        image = self._original.scaled(event.size() * ratio, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
        image.setDevicePixelRatio(ratio)
        self.setPixmap(image)


class TutorialDialog(QDialog):
    def __init__(self, parent, resource_root, *, macos=False):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(f"{APP_NAME} · 使用教程")
        self.setWindowIcon(parent.windowIcon())
        self.resize(624, 552)
        self.setMinimumSize(440, 355)
        self.setStyleSheet("""
            QLabel#tutorialHeading { color:#59f4ca; font-size:23px; font-weight:700; }
            QLabel#tutorialStep { font-size:18px; font-weight:700; }
            QLabel#tutorialIntro, QLabel#tutorialCount { color:#a8beb1; }
            QLabel#tutorialTip { background:#182820; color:#bcdfcf; border:1px solid #355344;
                                 border-radius:6px; padding:10px; }
            QScrollArea#tutorialPage { border:none; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        layout.addWidget(_label("快速上手", "tutorialHeading"))
        intro = ("先选择显示器并框选商店，再配置并开启提醒。" if macos
                 else "按前 3 步开启提醒。需要校准商店时，查看第 4 页。")
        layout.addWidget(_label(intro, "tutorialIntro"))

        self.pages = QTabWidget()
        self.pages.setUsesScrollButtons(True)
        for tab, title, filename, description, body, tip in (_MAC_STEPS if macos else _STEPS):
            page = QWidget()
            content = QVBoxLayout(page)
            content.setContentsMargins(12, 12, 12, 12)
            content.setSpacing(10)
            content.addWidget(_label(title, "tutorialStep"))
            if filename:
                pixmap = QPixmap(str(resource_root / "assets" / "tutorial" / filename))
                if not pixmap.isNull():
                    content.addWidget(_Illustration(pixmap, description))
            content.addWidget(_label(body))
            content.addWidget(_label(tip, "tutorialTip"))
            content.addStretch()
            scroll = QScrollArea()
            scroll.setObjectName("tutorialPage")
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.pages.addTab(scroll, tab)
        layout.addWidget(self.pages, 1)

        navigation = QHBoxLayout()
        self.counter = _label("", "tutorialCount")
        navigation.addWidget(self.counter)
        navigation.addStretch()
        self.previous = QPushButton("上一步")
        self.previous.clicked.connect(lambda: self.pages.setCurrentIndex(self.pages.currentIndex() - 1))
        navigation.addWidget(self.previous)
        self.next = QPushButton("下一步")
        self.next.setObjectName("primary")
        self.next.clicked.connect(self._advance)
        navigation.addWidget(self.next)
        close = QPushButton("关闭教程")
        close.clicked.connect(self.reject)
        navigation.addWidget(close)
        layout.addLayout(navigation)
        self.pages.currentChanged.connect(self._update_navigation)
        self._update_navigation(self.pages.currentIndex())

    def _update_navigation(self, index):
        self.counter.setText(f"{index + 1} / {self.pages.count()}")
        self.previous.setEnabled(index > 0)
        self.next.setText("完成" if index == self.pages.count() - 1 else "下一步")

    def _advance(self):
        index = self.pages.currentIndex()
        if index == self.pages.count() - 1:
            self.accept()
        else:
            self.pages.setCurrentIndex(index + 1)
