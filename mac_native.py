"""Quartz window metadata and AppKit focus for the screen-only assistant.

Window rectangles are global logical points with an exclusive right and bottom.
Quartz reports the whole window. The capture backend maps those points to its
own image pixels, while Qt uses the points directly to position the overlay.
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable

try:
    import AppKit
    import objc
    import Quartz
except ImportError:
    AppKit = objc = Quartz = None


class NativeError(RuntimeError):
    """A macOS window operation could not be completed safely."""


def _require_frameworks() -> None:
    if AppKit is None or objc is None or Quartz is None:
        raise NativeError("缺少 macOS 窗口支持组件，请按安装说明安装 PyObjC。")


def has_capture_permission() -> bool:
    """Check Screen Recording permission without requesting it."""
    preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    return bool(preflight and preflight())


def request_capture_permission() -> bool:
    """Request Screen Recording permission from an explicit UI action."""
    request = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
    return has_capture_permission() or bool(request and request())


def _window_rows(onscreen: bool):
    option = Quartz.kCGWindowListOptionOnScreenOnly if onscreen else Quartz.kCGWindowListOptionAll
    rows = Quartz.CGWindowListCopyWindowInfo(option | Quartz.kCGWindowListExcludeDesktopElements,
                                           Quartz.kCGNullWindowID)
    if rows is None:
        raise NativeError("interactive_desktop_unavailable")
    return rows


def _window_id(row) -> int:
    return int(row[Quartz.kCGWindowNumber])


def _owner_pid(row) -> int:
    return int(row[Quartz.kCGWindowOwnerPID])


def _find_row(hwnd: int, rows):
    return next((row for row in rows if _window_id(row) == hwnd), None)


def _window_row(hwnd: int, onscreen_rows=None):
    hwnd = int(hwnd)
    if not 0 < hwnd <= 0xFFFFFFFF:
        raise NativeError("target_closed")
    if onscreen_rows is None:
        onscreen_rows = _window_rows(onscreen=True)
    row = _find_row(hwnd, onscreen_rows)
    if row is not None:
        return row, True
    row = _find_row(hwnd, _window_rows(onscreen=False))
    if row is None:
        raise NativeError("target_closed")
    return row, bool(row.get(Quartz.kCGWindowIsOnscreen, False))


def _title(row) -> str:
    title = str(row.get(Quartz.kCGWindowName) or "").strip()
    if title:
        return title
    owner = str(row.get(Quartz.kCGWindowOwnerName) or "应用").strip()
    return f"{owner} · 窗口 {_window_id(row)}"


def _visible(row, onscreen: bool) -> bool:
    return onscreen and float(row[Quartz.kCGWindowAlpha]) > 0


def _normal(row) -> bool:
    return int(row[Quartz.kCGWindowLayer]) == 0


def _window_rect(row) -> tuple[int, int, int, int]:
    ok, rect = Quartz.CGRectMakeWithDictionaryRepresentation(row[Quartz.kCGWindowBounds], None)
    if not ok:
        raise NativeError("target_invalid_bounds")
    (x, y), (width, height) = rect
    if not all(math.isfinite(value) for value in (x, y, width, height)) or min(width, height) < 0:
        raise NativeError("target_invalid_bounds")
    left, top = math.floor(x), math.floor(y)
    right = math.ceil(x + width) if width else left
    bottom = math.ceil(y + height) if height else top
    return left, top, right, bottom


def _geometry(row, onscreen: bool, include_title: bool) -> dict[str, Any]:
    rect = _window_rect(row)
    left, top, right, bottom = rect
    return {"hwnd": _window_id(row), "pid": _owner_pid(row), "title": _title(row) if include_title else "",
            "visible": _visible(row, onscreen), "minimized": False, "cloaked": not onscreen,
            "client_screen_rect": rect, "client_rect": rect, "client_size": (right - left, bottom - top),
            "window_rect": rect, "frame_rect": rect, "capture_screen_rect": rect,
            "frame_bounds_available": True, "client_offset_in_window": (0, 0),
            "client_offset_in_frame": (0, 0), "coordinate_space": "quartz_global_points"}


def _process_identity(pid: int) -> dict[str, Any]:
    application = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if application is None or application.isTerminated():
        raise NativeError("target_identity_unavailable")
    bundle = str(application.bundleIdentifier() or "")
    executable = application.executableURL()
    exe = str(executable.path() or "") if executable is not None else ""
    launch = application.launchDate()
    if launch is None or not (exe or bundle):
        # Without a launch time, a reused PID cannot be safely distinguished.
        raise NativeError("target_identity_unavailable")
    return {"pid": pid, "exe": exe or bundle, "process_start_time": float(launch.timeIntervalSince1970()),
            "class_name": bundle, "bundle_identifier": bundle}


def window_identity(hwnd: int) -> dict[str, Any]:
    _require_frameworks()
    with objc.autorelease_pool():
        row, _ = _window_row(hwnd)
        pid = _owner_pid(row)
        identity = {"hwnd": _window_id(row), **_process_identity(pid)}
        latest, _ = _window_row(hwnd)
        if _owner_pid(latest) != pid:
            raise NativeError("target_identity_changed")
        return identity


def get_window_geometry(hwnd: int, *, include_title: bool = False) -> dict[str, Any]:
    _require_frameworks()
    with objc.autorelease_pool():
        row, onscreen = _window_row(hwnd)
        return _geometry(row, onscreen, include_title)


def get_window_info(hwnd: int) -> dict[str, Any]:
    _require_frameworks()
    with objc.autorelease_pool():
        row, onscreen = _window_row(hwnd)
        return {**_geometry(row, onscreen, True), **_process_identity(_owner_pid(row))}


def list_windows(exclude_own_process: bool = True) -> list[dict[str, Any]]:
    """Return visible normal windows even when Quartz withholds their titles."""
    _require_frameworks()
    windows = []
    identities = {}
    with objc.autorelease_pool():
        for row in _window_rows(onscreen=True):
            pid = _owner_pid(row)
            if (exclude_own_process and pid == os.getpid()) or not _normal(row) or not _visible(row, True):
                continue
            try:
                window = _geometry(row, True, True)
            except NativeError:
                continue
            if min(window["client_size"]) <= 0:
                continue
            if pid not in identities:
                try:
                    identities[pid] = _process_identity(pid)
                except NativeError:
                    identities[pid] = None
            process = identities[pid]
            if process is None:
                window.update(exe=None, process_start_time=None, class_name="", identity_available=False)
            else:
                window.update(process, identity_available=True)
            windows.append(window)
    return sorted(windows, key=lambda row: (row["title"].casefold(), row["hwnd"]))


def _validate_row(identity, row, onscreen: bool, require_visible: bool) -> tuple[bool, str]:
    pid = _owner_pid(row)
    if pid != identity.get("pid"):
        return False, "target_identity_changed"
    current = _process_identity(pid)
    if any(current[key] != identity.get(key) for key in ("process_start_time", "exe", "class_name")):
        return False, "target_identity_changed"
    if require_visible:
        if not _visible(row, onscreen) or not _normal(row):
            return False, "target_hidden"
        left, top, right, bottom = _window_rect(row)
        if min(right - left, bottom - top) <= 0:
            return False, "target_empty_client"
    return True, "ok"


def validate_target(identity: dict[str, Any], require_visible: bool = True) -> tuple[bool, str]:
    try:
        _require_frameworks()
        with objc.autorelease_pool():
            row, onscreen = _window_row(identity["hwnd"])
            return _validate_row(identity, row, onscreen, require_visible)
    except (NativeError, OSError, KeyError, TypeError, ValueError, OverflowError) as error:
        return False, str(error)


def foreground_belongs_to_target(identity: dict[str, Any], allowed_hwnds: Iterable[int] = (),
                                 allow_same_process: bool = False) -> bool:
    """Use the frontmost app and its topmost normal window in Quartz order."""
    try:
        _require_frameworks()
        with objc.autorelease_pool():
            rows = _window_rows(onscreen=True)
            hwnd = int(identity["hwnd"])
            target = _find_row(hwnd, rows)
            if target is None or not _validate_row(identity, target, True, True)[0]:
                return False
            application = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if application is None:
                return False
            pid = int(application.processIdentifier())
            if pid == os.getpid():
                # The assistant's Qt tool windows need not be at normal level.
                key_window = AppKit.NSApplication.sharedApplication().keyWindow()
                foreground = int(key_window.windowNumber()) if key_window is not None else 0
            else:
                topmost = next((row for row in rows if _owner_pid(row) == pid
                                and _normal(row) and _visible(row, True)), None)
                foreground = _window_id(topmost) if topmost is not None else 0
            return foreground > 0 and (foreground == hwnd or foreground in allowed_hwnds
                                       or (allow_same_process and pid == identity["pid"]))
    except (NativeError, OSError, KeyError, TypeError, ValueError, OverflowError):
        return False


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


def own_window_number(view_id: int) -> int:
    """Convert a top-level Qt winId() into a Quartz window ID on the UI thread."""
    _require_frameworks()
    with objc.autorelease_pool():
        window = _own_window(view_id)
        return max(0, int(window.windowNumber())) if window is not None else 0


def configure_overlay(view_id: int, click_through: bool = True) -> dict[str, Any]:
    """Apply AppKit click-through to a window owned by this assistant."""
    _require_frameworks()
    with objc.autorelease_pool():
        window = _own_window(view_id)
        if window is None:
            raise NativeError("overlay_window_unavailable")
        window.setIgnoresMouseEvents_(bool(click_through))
        return {"click_through": bool(window.ignoresMouseEvents())}
