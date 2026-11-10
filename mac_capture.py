"""Capture the selected macOS window with ScreenCaptureKit on the OCR worker."""

from __future__ import annotations

import math
import platform
import sys
import threading
import time

import numpy as np

from vision import CapturedFrame, CaptureUnavailable


_REQUEST_TIMEOUT = 2.0
_SOURCE = "macos_screencapturekit"


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
        raise CaptureUnavailable("capture_timeout", "等待所选窗口的画面超时。")
    value, error = response
    if error is not None or value is None:
        detail = error.localizedDescription() if error is not None else "系统未返回画面。"
        raise CaptureUnavailable("capture_error", f"无法捕获所选窗口：{detail}")
    return value


def _size(rect):
    left, top, right, bottom = rect
    return right - left, bottom - top


def _bgr_from_image(quartz, image, pixel_size):
    width, height = pixel_size
    if (quartz.CGImageGetWidth(image), quartz.CGImageGetHeight(image)) != pixel_size:
        raise CaptureUnavailable("geometry_mismatch", "捕获像素尺寸与所选窗口不一致。")

    # Render into an explicit 8-bit sRGB BGRA layout. A CGImage's own provider
    # can have different byte order, row padding, component depth or gamut.
    color_space = quartz.CGColorSpaceCreateWithName(quartz.kCGColorSpaceSRGB)
    bitmap_info = quartz.kCGBitmapByteOrder32Little | quartz.kCGImageAlphaPremultipliedFirst
    context = quartz.CGBitmapContextCreate(None, width, height, 8, width * 4, color_space, bitmap_info)
    if context is None:
        raise CaptureUnavailable("invalid_frame", "无法分配窗口画面的像素缓冲区。")
    quartz.CGContextDrawImage(context, quartz.CGRectMake(0, 0, width, height), image)
    stride = quartz.CGBitmapContextGetBytesPerRow(context)
    data = quartz.CGBitmapContextGetData(context).as_buffer(stride * height)
    bgra = np.ndarray((height, width, 4), dtype=np.uint8, buffer=data, strides=(stride, 4, 1))
    # Copy while the context is alive. bytes also prevents re-enabling writes
    # on a frame shared with both recognition and verification passes.
    return np.frombuffer(bgra[:, :, :3].tobytes(), dtype=np.uint8).reshape(height, width, 3)


class WindowCapture:
    """One-shot capture with a short reuse interval for immediate callers."""

    def __init__(self, hwnd: int, *, minimum_update_interval_ms: int = 80, identity: dict | None = None):
        self.hwnd = int(hwnd)  # CGWindowID, shared interface with Windows capture.
        self.minimum_update_interval_ms = max(80, int(minimum_update_interval_ms))
        self._lock = threading.Lock()
        self._runtime = None
        self._identity = dict(identity) if identity is not None else None
        self._filter = None
        self._filter_rect = None
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
                raise CaptureUnavailable("dependency_missing", "缺少 Mac 窗口捕获组件。") from error

            with objc.autorelease_pool():
                if not mac_native.has_capture_permission():
                    raise CaptureUnavailable("permission_denied", "请在系统设置中允许咕顾谷录制屏幕，再重新打开软件。")
                identity = self._identity
                if identity is None:
                    try:
                        identity = mac_native.window_identity(self.hwnd)
                    except Exception as error:
                        raise CaptureUnavailable("identity_unavailable", f"无法确认绑定窗口身份：{error}") from error
                if identity.get("hwnd") != self.hwnd:
                    raise CaptureUnavailable("target_invalid", "绑定窗口的身份与所选窗口不一致。")
                self._geometry(mac_native, identity)
            self._runtime = objc, Quartz, ScreenCaptureKit, mac_native
            self._identity = identity

    def _geometry(self, native, identity):
        try:
            valid, reason = native.validate_target(identity)
            if not valid:
                raise CaptureUnavailable("target_invalid", f"绑定窗口当前不可用：{reason}")
            geometry = native.get_window_geometry(self.hwnd)
        except CaptureUnavailable:
            raise
        except Exception as error:
            raise CaptureUnavailable("window_unavailable", f"无法读取绑定窗口：{error}") from error
        if not geometry or geometry.get("pid") != identity.get("pid"):
            raise CaptureUnavailable("window_replaced", "绑定的窗口已关闭或被替换，请重新选择。")
        if geometry.get("minimized"):
            raise CaptureUnavailable("window_minimized", "窗口已最小化，识别已暂停。")
        if not geometry.get("visible") or geometry.get("cloaked"):
            raise CaptureUnavailable("window_hidden", "绑定的窗口当前不可见，识别已暂停。")
        if min(_size(geometry["client_screen_rect"])) <= 0:
            raise CaptureUnavailable("invalid_geometry", "所选窗口没有有效尺寸。")
        return geometry

    def _content_filter(self, kit, rect):
        if self._filter is not None and self._filter_rect == rect:
            return self._filter
        list_windows = kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_
        content = _request(lambda complete: list_windows(True, True, complete))
        for window in content.windows():
            if int(window.windowID()) != self.hwnd:
                continue
            owner = window.owningApplication()
            if owner is not None and int(owner.processID()) == self._identity["pid"] and window.isOnScreen():
                capture_filter = kit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
                if capture_filter is not None:
                    self._filter, self._filter_rect = capture_filter, rect
                    return capture_filter
            break
        raise CaptureUnavailable("window_hidden", "系统当前未提供所选窗口的画面。")

    def latest_frame(self, max_age_ms: float = 2000) -> CapturedFrame:
        with self._lock:
            if self._runtime is None:
                raise CaptureUnavailable("capture_stopped", "窗口捕获尚未启动。")
            objc, quartz, kit, native = self._runtime
            with objc.autorelease_pool():
                try:
                    return self._latest_frame(quartz, kit, native, float(max_age_ms))
                except CaptureUnavailable:
                    self._frame = self._filter = self._filter_rect = None
                    raise
                except Exception as error:
                    self._frame = self._filter = self._filter_rect = None
                    raise CaptureUnavailable("capture_error", f"无法捕获所选窗口：{error}") from error

    def _latest_frame(self, quartz, kit, native, max_age_ms):
        geometry = self._geometry(native, self._identity)
        rect = tuple(geometry["client_screen_rect"])
        if self._frame is not None:
            age = (time.monotonic() - self._frame.timestamp) * 1000
            if (rect == tuple(self._frame.geometry["client_screen_rect"])
                    and age <= max_age_ms and age < self.minimum_update_interval_ms):
                return self._frame

        capture_filter = self._content_filter(kit, rect)
        content_size = capture_filter.contentRect().size
        logical_size = _size(rect)
        if (abs(content_size.width - logical_size[0]) > 1
                or abs(content_size.height - logical_size[1]) > 1):
            raise CaptureUnavailable("window_resizing", "所选窗口尺寸正在变化，等待新画面。")
        scale = float(capture_filter.pointPixelScale())
        if not math.isfinite(scale) or not 0 < scale <= 4:
            raise CaptureUnavailable("invalid_geometry", "所选窗口的像素比例无效。")
        pixel_size = round(content_size.width * scale), round(content_size.height * scale)
        width, height = pixel_size
        if min(pixel_size) <= 0 or max(pixel_size) > 16_384 or width * height > 32_000_000:
            raise CaptureUnavailable("invalid_frame", "所选窗口的像素尺寸超出支持范围。")

        config = kit.SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setShowsCursor_(False)
        config.setCapturesAudio_(False)
        config.setIgnoreShadowsSingleWindow_(True)
        config.setIncludeChildWindows_(False)
        # SCScreenshotManager's image callback has no presentation timestamp.
        # Use the earlier request time to conservatively bound frame freshness.
        timestamp = time.monotonic()
        capture = kit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_
        image = _request(lambda complete: capture(capture_filter, config, complete))
        pixels = _bgr_from_image(quartz, image, pixel_size)
        geometry = self._geometry(native, self._identity)
        if tuple(geometry["client_screen_rect"]) != rect:
            raise CaptureUnavailable("window_resizing", "所选窗口的位置或尺寸正在变化，等待新画面。")
        if (time.monotonic() - timestamp) * 1000 > max_age_ms:
            raise CaptureUnavailable("frame_stale", "窗口画面已过期，识别已暂停。")
        geometry = {**geometry, "capture_pixel_size": pixel_size, "capture_point_pixel_scale": scale}
        self._sequence += 1
        self._frame = CapturedFrame(pixels, self._sequence, timestamp, geometry, _SOURCE)
        return self._frame

    def stop(self) -> None:
        with self._lock:
            self._runtime = None
            self._frame = self._filter = self._filter_rect = None
