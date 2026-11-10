"""Capture the selected macOS display with ScreenCaptureKit on the OCR worker."""

from __future__ import annotations

import math
import os
import platform
import sys
import threading
import time

import numpy as np

from vision import CapturedFrame, CaptureUnavailable


_REQUEST_TIMEOUT = 2.0
_SOURCE = "macos_screencapturekit_display"


def _request(begin):
    """Wait for an Objective-C completion without dispatching to the GUI thread."""
    complete = threading.Event()
    response = None

    def callback(value, error):
        nonlocal response
        response = value, error
        complete.set()

    begin(callback)
    if not complete.wait(_REQUEST_TIMEOUT):
        raise CaptureUnavailable("capture_timeout", "等待所选显示器的画面超时。")
    value, error = response
    if error is not None or value is None:
        detail = error.localizedDescription() if error is not None else "系统未返回画面。"
        raise CaptureUnavailable("capture_error", f"无法捕获所选显示器：{detail}")
    return value


def _size(rect):
    left, top, right, bottom = rect
    return right - left, bottom - top


def _display_state(geometry):
    return (tuple(geometry["client_screen_rect"]), geometry.get("display_pixel_size"),
            geometry.get("display_rotation"), geometry.get("profile_id"), geometry.get("is_main"))


def _bgr_from_image(quartz, image, pixel_size):
    width, height = pixel_size
    if (quartz.CGImageGetWidth(image), quartz.CGImageGetHeight(image)) != pixel_size:
        raise CaptureUnavailable("geometry_mismatch", "捕获像素尺寸与所选显示器不一致。")

    # Render into an explicit 8-bit sRGB BGRA layout. A CGImage's own provider
    # can have different byte order, row padding, component depth or gamut.
    color_space = quartz.CGColorSpaceCreateWithName(quartz.kCGColorSpaceSRGB)
    bitmap_info = quartz.kCGBitmapByteOrder32Little | quartz.kCGImageAlphaPremultipliedFirst
    context = quartz.CGBitmapContextCreate(None, width, height, 8, width * 4, color_space, bitmap_info)
    if context is None:
        raise CaptureUnavailable("invalid_frame", "无法分配显示器画面的像素缓冲区。")
    quartz.CGContextDrawImage(context, quartz.CGRectMake(0, 0, width, height), image)
    stride = quartz.CGBitmapContextGetBytesPerRow(context)
    data = quartz.CGBitmapContextGetData(context).as_buffer(stride * height)
    bgra = np.ndarray((height, width, 4), dtype=np.uint8, buffer=data, strides=(stride, 4, 1))
    # Copy while the context is alive. bytes also prevents re-enabling writes
    # on a frame shared with both recognition and verification passes.
    return np.frombuffer(bgra[:, :, :3].tobytes(), dtype=np.uint8).reshape(height, width, 3)


class DisplayCapture:
    """One-shot capture with a short reuse interval for immediate callers."""

    def __init__(self, display_id: int, *, minimum_update_interval_ms: int = 80):
        self.display_id = int(display_id)
        self.minimum_update_interval_ms = max(80, int(minimum_update_interval_ms))
        self._lock = threading.Lock()
        self._runtime = None
        self._profile_id = None
        self._filter = None
        self._filter_state = None
        self._frame = None
        self._sequence = 0

    def start(self) -> None:
        with self._lock:
            if self._runtime is not None:
                return
            if sys.platform != "darwin" or platform.machine() != "arm64":
                raise CaptureUnavailable("capture_platform", "Mac 测试版需要 Apple 芯片。")
            version = platform.mac_ver()[0].split(".")
            if tuple(int(part) for part in version[:2]) < (14, 4):
                raise CaptureUnavailable("capture_version", "Mac 测试版需要 macOS 14.4 或更新版本。")
            try:
                import objc
                import Quartz
                import ScreenCaptureKit
                import mac_native
            except ImportError as error:
                raise CaptureUnavailable("dependency_missing", "缺少 Mac 显示器捕获组件。") from error

            with objc.autorelease_pool():
                if not mac_native.has_capture_permission():
                    raise CaptureUnavailable("permission_denied", "请在系统设置中允许咕顾谷录制屏幕，再重新打开软件。")
                geometry = self._geometry(mac_native)
            self._runtime = objc, Quartz, ScreenCaptureKit, mac_native
            self._profile_id = geometry.get("profile_id")

    def _geometry(self, native):
        try:
            geometry = native.get_display_geometry(self.display_id)
        except Exception as error:
            raise CaptureUnavailable("display_unavailable", f"无法读取所选显示器：{error}") from error
        if (not geometry or geometry.get("display_id") != self.display_id or
                (self._profile_id is not None and geometry.get("profile_id") != self._profile_id)):
            raise CaptureUnavailable("display_unavailable", "所选显示器已断开或更换，请重新选择。")
        if min(_size(geometry["client_screen_rect"])) <= 0:
            raise CaptureUnavailable("invalid_geometry", "所选显示器没有有效尺寸。")
        return geometry

    def _content_filter(self, kit, state):
        if self._filter is not None and self._filter_state == state:
            return self._filter
        list_content = kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_
        content = _request(lambda complete: list_content(False, False, complete))
        display = next((item for item in content.displays() if int(item.displayID()) == self.display_id), None)
        if display is None:
            raise CaptureUnavailable("display_unavailable", "系统当前未提供所选显示器的画面。")

        pid = os.getpid()
        own_app = next((app for app in content.applications() if int(app.processID()) == pid), None)
        if own_app is None:
            owners = (window.owningApplication() for window in content.windows())
            own_app = next((app for app in owners if app is not None and int(app.processID()) == pid), None)
        if own_app is None:
            raise CaptureUnavailable("display_filter_unavailable", "系统暂未提供咕顾谷的应用信息，无法从画面排除提醒窗口。")

        capture_filter = kit.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            display, [own_app], [])
        if capture_filter is None:
            raise CaptureUnavailable("display_filter_unavailable", "系统未能创建显示器捕获过滤器。")
        self._filter, self._filter_state = capture_filter, state
        return capture_filter

    def latest_frame(self, max_age_ms: float = 2000) -> CapturedFrame:
        with self._lock:
            if self._runtime is None:
                raise CaptureUnavailable("capture_stopped", "显示器捕获尚未启动。")
            objc, quartz, kit, native = self._runtime
            with objc.autorelease_pool():
                try:
                    return self._latest_frame(quartz, kit, native, float(max_age_ms))
                except CaptureUnavailable:
                    self._frame = self._filter = self._filter_state = None
                    raise
                except Exception as error:
                    self._frame = self._filter = self._filter_state = None
                    raise CaptureUnavailable("capture_error", f"无法捕获所选显示器：{error}") from error

    def _latest_frame(self, quartz, kit, native, max_age_ms):
        geometry = self._geometry(native)
        state = _display_state(geometry)
        foreground_pid = native.foreground_process_id()
        if self._frame is not None:
            age = (time.monotonic() - self._frame.timestamp) * 1000
            if (state == _display_state(self._frame.geometry)
                    and foreground_pid == self._frame.geometry["foreground_pid"]
                    and age <= max_age_ms and age < self.minimum_update_interval_ms):
                return self._frame

        capture_filter = self._content_filter(kit, state)
        content_size = capture_filter.contentRect().size
        logical_size = _size(geometry["client_screen_rect"])
        if (not all(math.isfinite(value) for value in (content_size.width, content_size.height))
                or abs(content_size.width - logical_size[0]) > 1
                or abs(content_size.height - logical_size[1]) > 1):
            raise CaptureUnavailable("display_reconfiguring", "所选显示器正在调整，等待新画面。")
        scale = float(capture_filter.pointPixelScale())
        if not math.isfinite(scale) or not 0 < scale <= 4:
            raise CaptureUnavailable("invalid_geometry", "所选显示器的像素比例无效。")
        pixel_size = round(content_size.width * scale), round(content_size.height * scale)
        width, height = pixel_size
        if min(pixel_size) <= 0 or max(pixel_size) > 16_384 or width * height > 32_000_000:
            raise CaptureUnavailable("invalid_frame", "所选显示器的像素尺寸超出支持范围。")

        config = kit.SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setShowsCursor_(False)
        config.setCapturesAudio_(False)
        microphone = getattr(config, "setCaptureMicrophone_", None)
        if microphone is not None:
            microphone(False)
        # SCScreenshotManager's image callback has no presentation timestamp.
        # Use the earlier request time to conservatively bound frame freshness.
        timestamp = time.monotonic()
        capture = kit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_
        image = _request(lambda complete: capture(capture_filter, config, complete))
        pixels = _bgr_from_image(quartz, image, pixel_size)
        geometry = self._geometry(native)
        if _display_state(geometry) != state:
            raise CaptureUnavailable("display_reconfiguring", "所选显示器的位置或显示模式正在变化，等待新画面。")
        if native.foreground_process_id() != foreground_pid:
            raise CaptureUnavailable("display_reconfiguring", "前台应用已切换，等待新画面。")
        if (time.monotonic() - timestamp) * 1000 > max_age_ms:
            raise CaptureUnavailable("frame_stale", "显示器画面已过期，识别已暂停。")
        geometry = {**geometry, "foreground_pid": foreground_pid,
                    "capture_pixel_size": pixel_size, "capture_point_pixel_scale": scale}
        self._sequence += 1
        self._frame = CapturedFrame(pixels, self._sequence, timestamp, geometry, _SOURCE)
        return self._frame

    def stop(self) -> None:
        with self._lock:
            self._runtime = None
            self._frame = self._filter = self._filter_state = None
