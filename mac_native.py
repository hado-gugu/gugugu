"""Quartz display metadata and AppKit overlay support for macOS."""

from __future__ import annotations

import math
from typing import Any

try:
    import AppKit
    import objc
    import Quartz
except ImportError:
    AppKit = objc = Quartz = None


class NativeError(RuntimeError):
    """A macOS display operation could not be completed safely."""


def _require_frameworks() -> None:
    if AppKit is None or objc is None or Quartz is None:
        raise NativeError("缺少 macOS 显示器支持组件，请按安装说明安装 PyObjC。")


def has_capture_permission() -> bool:
    """Check Screen Recording permission without requesting it."""
    preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    return bool(preflight and preflight())


def request_capture_permission() -> bool:
    """Request Screen Recording permission from an explicit UI action."""
    request = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
    return has_capture_permission() or bool(request and request())


def _profile_id(display_id: int) -> str:
    # Some virtual displays don't report a serial number. Keep separate
    # per-display profiles in that case without guessing a hardware identity.
    serial = int(Quartz.CGDisplaySerialNumber(display_id))
    if serial:
        vendor = int(Quartz.CGDisplayVendorNumber(display_id))
        model = int(Quartz.CGDisplayModelNumber(display_id))
        return f"serial-{vendor:08x}-{model:08x}-{serial:08x}"
    return f"display-{display_id:08x}"


def _display_geometry(display_id: int, main_id: int) -> dict[str, Any]:
    if not 0 < display_id <= 0xFFFFFFFF or not Quartz.CGDisplayIsActive(display_id):
        raise NativeError("所选显示器当前不可用，请刷新显示器列表。")
    (x, y), (width, height) = Quartz.CGDisplayBounds(display_id)
    if not all(math.isfinite(value) for value in (x, y, width, height)) or min(width, height) <= 0:
        raise NativeError("所选显示器当前没有有效画面，请刷新显示器列表。")

    left, top = math.floor(x), math.floor(y)
    right, bottom = math.ceil(x + width), math.ceil(y + height)
    point_size = right - left, bottom - top
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)
    if mode is None:
        raise NativeError("无法读取所选显示器的当前模式。")
    pixels = int(Quartz.CGDisplayModeGetPixelWidth(mode)), int(Quartz.CGDisplayModeGetPixelHeight(mode))
    rotation = float(Quartz.CGDisplayRotation(display_id))
    if min(pixels) <= 0 or not math.isfinite(rotation):
        raise NativeError("所选显示器当前没有有效的显示模式。")

    is_main = display_id == main_id
    label = "主显示器" if is_main else f"显示器 {display_id}"
    profile_id = _profile_id(display_id)
    return {"display_id": display_id, "title": f"{label} · {point_size[0]} × {point_size[1]}",
            "is_main": is_main, "exe": "macos-display", "class_name": profile_id, "profile_id": profile_id,
            "client_screen_rect": (left, top, right, bottom), "client_size": point_size,
            "display_pixel_size": pixels, "display_rotation": rotation,
            "coordinate_space": "quartz_global_points"}


def get_display_geometry(display_id: int) -> dict[str, Any]:
    """Return global logical points and the current backing display mode."""
    _require_frameworks()
    with objc.autorelease_pool():
        return _display_geometry(int(display_id), int(Quartz.CGMainDisplayID()))


def list_displays() -> list[dict[str, Any]]:
    """Return active displays, with the primary display first."""
    _require_frameworks()
    with objc.autorelease_pool():
        error, display_ids, count = Quartz.CGGetActiveDisplayList(64, None, None)
        if error != Quartz.kCGErrorSuccess:
            raise NativeError(f"无法列出当前显示器（{error}）。")
        main_id = int(Quartz.CGMainDisplayID())
        displays = []
        for display_id in (display_ids or ())[:count]:
            try:
                displays.append(_display_geometry(int(display_id), main_id))
            except NativeError:
                # Displays can disconnect while the list is being prepared.
                continue
    return sorted(displays, key=lambda display: (not display["is_main"], display["display_id"]))


def foreground_process_id() -> int:
    """Return the current foreground app's PID, or zero when unavailable."""
    if AppKit is None or objc is None:
        return 0
    with objc.autorelease_pool():
        application = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return max(0, int(application.processIdentifier())) if application is not None else 0


def _own_window(view_id: int):
    """Resolve an owned Qt NSView pointer without dereferencing caller pointers."""
    view_id = int(view_id)
    for window in AppKit.NSApplication.sharedApplication().windows():
        content = window.contentView()
        pending = [content] if content is not None else []
        while pending:
            view = pending.pop()
            if objc.pyobjc_id(view) == view_id:
                return window
            pending.extend(view.subviews())
    return None


def configure_overlay(view_id: int, click_through: bool = True) -> dict[str, Any]:
    """Apply AppKit click-through to a window owned by this assistant."""
    _require_frameworks()
    with objc.autorelease_pool():
        window = _own_window(view_id)
        if window is None:
            raise NativeError("overlay_window_unavailable")
        window.setIgnoresMouseEvents_(bool(click_through))
        return {"click_through": bool(window.ignoresMouseEvents())}
