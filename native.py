"""Small Win32 boundary for the screen-only assistant.

No input injection or game-process memory access. Geometry is physical pixels,
rectangles use exclusive right/bottom edges. Mutating helpers accept only HWNDs
owned by this assistant process. PrintWindow is an explicit worker-thread fallback;
the normal capture path belongs to vision.py (Windows Graphics Capture).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes as ct
from ctypes import wintypes as wt
import os
import time
from typing import Any, Iterable


class NativeError(RuntimeError):
    """A native target/capture cannot be used safely with its current geometry."""


Rect = tuple[int, int, int, int]
IS_WINDOWS = os.name == "nt"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
WDA_NONE = 0
WDA_EXCLUDEFROMCAPTURE = 0x11
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GA_ROOTOWNER = 3
PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2


def rect_size(rect: Iterable[int]) -> tuple[int, int]:
    left, top, right, bottom = map(int, rect)
    return right - left, bottom - top


def translate_rect(rect: Iterable[int], dx: int, dy: int) -> Rect:
    left, top, right, bottom = map(int, rect)
    return left + int(dx), top + int(dy), right + int(dx), bottom + int(dy)


def intersect_rect(first: Iterable[int], second: Iterable[int]) -> Rect | None:
    a, b = tuple(first), tuple(second)
    result = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return result if result[2] > result[0] and result[3] > result[1] else None


def client_crop_rect(capture_size: Iterable[int], geometry: dict[str, Any]) -> Rect:
    """Locate the client area only for an exact, known capture geometry.

    WGC builds can return client, DWM visible-frame, or full-window dimensions.
    A mismatch is rejected instead of guessing a scale that misplaces reminders.
    """
    size = tuple(map(int, capture_size))
    client = tuple(geometry["client_screen_rect"])
    client_size = rect_size(client)
    if size == client_size and min(size) > 0:
        return 0, 0, size[0], size[1]
    candidates = [geometry.get("capture_screen_rect"), geometry.get("frame_rect"), geometry.get("window_rect")]
    for screen_rect in candidates:
        if screen_rect is None or rect_size(screen_rect) != size:
            continue
        crop = translate_rect(client, -screen_rect[0], -screen_rect[1])
        if (crop[0] >= 0 and crop[1] >= 0 and crop[2] <= size[0] and crop[3] <= size[1]
                and rect_size(crop) == client_size and min(client_size) > 0):
            return crop
    raise NativeError(f"geometry_mismatch: capture={size}, client={client_size}")


def client_rect_to_screen(rect: Iterable[int], geometry: dict[str, Any]) -> Rect:
    """Convert a client-pixel ROI to physical desktop pixels for the overlay."""
    rect = tuple(map(int, rect))
    width, height = rect_size(geometry["client_screen_rect"])
    if not (0 <= rect[0] < rect[2] <= width and 0 <= rect[1] < rect[3] <= height):
        raise NativeError("client_roi_out_of_bounds")
    origin = geometry["client_screen_rect"]
    return translate_rect(rect, origin[0], origin[1])


if IS_WINDOWS:
    user32 = ct.WinDLL("user32", use_last_error=True)
    kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    gdi32 = ct.WinDLL("gdi32", use_last_error=True)
    dwmapi = ct.WinDLL("dwmapi", use_last_error=True)
    HWND = wt.HWND
    HANDLE = wt.HANDLE
    HDC = wt.HDC
    LPARAM = ct.c_ssize_t
    LONG_PTR = ct.c_ssize_t
    WNDENUMPROC = ct.WINFUNCTYPE(wt.BOOL, HWND, LPARAM)

    def _bind(dll, name, restype, argtypes):
        function = getattr(dll, name)
        function.restype, function.argtypes = restype, argtypes
        return function

    _IsWindow = _bind(user32, "IsWindow", wt.BOOL, [HWND])
    _IsWindowVisible = _bind(user32, "IsWindowVisible", wt.BOOL, [HWND])
    _IsIconic = _bind(user32, "IsIconic", wt.BOOL, [HWND])
    _EnumWindows = _bind(user32, "EnumWindows", wt.BOOL, [WNDENUMPROC, LPARAM])
    _GetWindowThreadProcessId = _bind(user32, "GetWindowThreadProcessId", wt.DWORD, [HWND, ct.POINTER(wt.DWORD)])
    _GetWindowTextLength = _bind(user32, "GetWindowTextLengthW", ct.c_int, [HWND])
    _GetWindowText = _bind(user32, "GetWindowTextW", ct.c_int, [HWND, wt.LPWSTR, ct.c_int])
    _GetClassName = _bind(user32, "GetClassNameW", ct.c_int, [HWND, wt.LPWSTR, ct.c_int])
    _GetWindowRect = _bind(user32, "GetWindowRect", wt.BOOL, [HWND, ct.POINTER(wt.RECT)])
    _GetClientRect = _bind(user32, "GetClientRect", wt.BOOL, [HWND, ct.POINTER(wt.RECT)])
    _ClientToScreen = _bind(user32, "ClientToScreen", wt.BOOL, [HWND, ct.POINTER(wt.POINT)])
    _GetForegroundWindow = _bind(user32, "GetForegroundWindow", HWND, [])
    _GetAncestor = _bind(user32, "GetAncestor", HWND, [HWND, wt.UINT])
    _GetDpiForWindow = _bind(user32, "GetDpiForWindow", wt.UINT, [HWND]) if hasattr(user32, "GetDpiForWindow") else None
    _SetThreadDpiContext = _bind(user32, "SetThreadDpiAwarenessContext", HANDLE, [HANDLE]) if hasattr(user32, "SetThreadDpiAwarenessContext") else None
    _SetProcessDpiContext = _bind(user32, "SetProcessDpiAwarenessContext", wt.BOOL, [HANDLE]) if hasattr(user32, "SetProcessDpiAwarenessContext") else None
    _SetWindowDisplayAffinity = _bind(user32, "SetWindowDisplayAffinity", wt.BOOL, [HWND, wt.DWORD])
    _GetWindowDisplayAffinity = _bind(user32, "GetWindowDisplayAffinity", wt.BOOL, [HWND, ct.POINTER(wt.DWORD)])
    _GetWindowLongPtr = _bind(user32, "GetWindowLongPtrW" if ct.sizeof(ct.c_void_p) == 8 else "GetWindowLongW", LONG_PTR, [HWND, ct.c_int])
    _SetWindowLongPtr = _bind(user32, "SetWindowLongPtrW" if ct.sizeof(ct.c_void_p) == 8 else "SetWindowLongW", LONG_PTR, [HWND, ct.c_int, LONG_PTR])
    _SetWindowPos = _bind(user32, "SetWindowPos", wt.BOOL, [HWND, HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT])
    _DwmGetWindowAttribute = _bind(dwmapi, "DwmGetWindowAttribute", ct.c_long, [HWND, wt.DWORD, ct.c_void_p, wt.DWORD])
    _OpenProcess = _bind(kernel32, "OpenProcess", HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD])
    _CloseHandle = _bind(kernel32, "CloseHandle", wt.BOOL, [HANDLE])
    _QueryFullProcessImageName = _bind(kernel32, "QueryFullProcessImageNameW", wt.BOOL, [HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD)])
    _GetProcessTimes = _bind(kernel32, "GetProcessTimes", wt.BOOL, [HANDLE, ct.POINTER(wt.FILETIME), ct.POINTER(wt.FILETIME), ct.POINTER(wt.FILETIME), ct.POINTER(wt.FILETIME)])
    _GetDC = _bind(user32, "GetDC", HDC, [HWND])
    _ReleaseDC = _bind(user32, "ReleaseDC", ct.c_int, [HWND, HDC])
    _PrintWindow = _bind(user32, "PrintWindow", wt.BOOL, [HWND, HDC, wt.UINT])
    _CreateCompatibleDC = _bind(gdi32, "CreateCompatibleDC", HDC, [HDC])
    _DeleteDC = _bind(gdi32, "DeleteDC", wt.BOOL, [HDC])
    _SelectObject = _bind(gdi32, "SelectObject", HANDLE, [HDC, HANDLE])
    _DeleteObject = _bind(gdi32, "DeleteObject", wt.BOOL, [HANDLE])

    class BITMAPINFOHEADER(ct.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ct.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    _CreateDIBSection = _bind(gdi32, "CreateDIBSection", HANDLE,
                             [HDC, ct.POINTER(BITMAPINFO), wt.UINT, ct.POINTER(ct.c_void_p), HANDLE, wt.DWORD])


def _require_windows():
    if not IS_WINDOWS:
        raise NativeError("Windows is required for native window operations")


def _error(operation: str) -> NativeError:
    code = ct.get_last_error()
    return NativeError(f"{operation} failed (WinError {code}: {ct.FormatError(code).strip()})")


def _hwnd(value: int) -> int:
    _require_windows()
    value = int(value)
    if value <= 0 or not _IsWindow(value):
        raise NativeError("target_window_closed")
    return value


def _pid(hwnd: int) -> int:
    value = wt.DWORD()
    if not _GetWindowThreadProcessId(hwnd, ct.byref(value)):
        raise _error("GetWindowThreadProcessId")
    return int(value.value)


def _require_own_window(hwnd: int) -> int:
    hwnd = _hwnd(hwnd)
    if _pid(hwnd) != os.getpid():
        raise PermissionError("Native window changes are restricted to this assistant's own windows")
    return hwnd


@contextmanager
def physical_dpi_context():
    """Temporarily opt this thread into physical per-monitor-v2 coordinates."""
    _require_windows()
    previous = _SetThreadDpiContext(HANDLE(-4)) if _SetThreadDpiContext else None
    try:
        yield
    finally:
        if previous and _SetThreadDpiContext:
            _SetThreadDpiContext(previous)


def enable_per_monitor_dpi_awareness() -> bool:
    """Optional startup call before QApplication; geometry also uses thread context."""
    _require_windows()
    return bool(_SetProcessDpiContext and _SetProcessDpiContext(HANDLE(-4)))


def enable_dpi_awareness() -> None:
    """Best-effort process startup alias; Qt may have already selected PMv2."""
    enable_per_monitor_dpi_awareness()


def current_foreground_hwnd() -> int:
    _require_windows()
    return int(_GetForegroundWindow() or 0)


def _title(hwnd: int) -> str:
    length = _GetWindowTextLength(hwnd)
    buffer = ct.create_unicode_buffer(max(length + 1, 2))
    _GetWindowText(hwnd, buffer, len(buffer))
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ct.create_unicode_buffer(256)
    _GetClassName(hwnd, buffer, len(buffer))
    return buffer.value


def process_identity(pid: int) -> dict[str, Any]:
    """Query only executable identity and creation time, never process memory."""
    _require_windows()
    process = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not process:
        raise _error("OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)")
    try:
        length = wt.DWORD(32768)
        executable = ct.create_unicode_buffer(length.value)
        if not _QueryFullProcessImageName(process, 0, executable, ct.byref(length)):
            raise _error("QueryFullProcessImageNameW")
        creation, exit_time, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if not _GetProcessTimes(process, ct.byref(creation), ct.byref(exit_time), ct.byref(kernel), ct.byref(user)):
            raise _error("GetProcessTimes")
        created = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return {"pid": int(pid), "exe": executable.value, "process_start_time": created}
    finally:
        _CloseHandle(process)


def window_identity(hwnd: int) -> dict[str, Any]:
    hwnd = _hwnd(hwnd)
    thread_pid = _pid(hwnd)
    identity = {"hwnd": hwnd, **process_identity(thread_pid), "class_name": _class_name(hwnd)}
    # Reject a race where the handle changed owner during the identity query.
    if not _IsWindow(hwnd) or _pid(hwnd) != thread_pid:
        raise NativeError("target_identity_changed")
    return identity


def get_window_geometry(hwnd: int, *, include_title: bool = False) -> dict[str, Any]:
    """Read capture geometry without sending a title request to the UI thread.

    GetWindowText sends WM_GETTEXT for windows in this process. Calling it from
    a WGC callback can deadlock when the UI simultaneously waits for capture to
    stop. Title lookup is explicit metadata work; the hot path returns "".
    """
    hwnd = _hwnd(hwnd)
    with physical_dpi_context():
        window, client = wt.RECT(), wt.RECT()
        if not _GetWindowRect(hwnd, ct.byref(window)):
            raise _error("GetWindowRect")
        if not _GetClientRect(hwnd, ct.byref(client)):
            raise _error("GetClientRect")
        first, last = wt.POINT(client.left, client.top), wt.POINT(client.right, client.bottom)
        if not _ClientToScreen(hwnd, ct.byref(first)) or not _ClientToScreen(hwnd, ct.byref(last)):
            raise _error("ClientToScreen")
        window_rect = (window.left, window.top, window.right, window.bottom)
        client_rect = (first.x, first.y, last.x, last.y)
        frame = wt.RECT()
        frame_ok = _DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ct.byref(frame), ct.sizeof(frame)) == 0
        frame_rect = (frame.left, frame.top, frame.right, frame.bottom) if frame_ok else window_rect
        cloaked = wt.DWORD()
        cloak_ok = _DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ct.byref(cloaked), ct.sizeof(cloaked)) == 0
        return {"hwnd": hwnd, "pid": _pid(hwnd), "title": _title(hwnd) if include_title else "",
                "visible": bool(_IsWindowVisible(hwnd)), "minimized": bool(_IsIconic(hwnd)),
                "cloaked": bool(cloaked.value) if cloak_ok else False,
                "dpi": int(_GetDpiForWindow(hwnd) or 96) if _GetDpiForWindow else 96,
                "client_screen_rect": client_rect, "client_rect": client_rect,
                "client_size": rect_size(client_rect), "window_rect": window_rect,
                "frame_rect": frame_rect, "capture_screen_rect": frame_rect,
                "frame_bounds_available": frame_ok,
                "client_offset_in_window": (first.x - window.left, first.y - window.top),
                "client_offset_in_frame": (first.x - frame_rect[0], first.y - frame_rect[1])}


def get_window_info(hwnd: int) -> dict[str, Any]:
    return {**get_window_geometry(hwnd, include_title=True), **window_identity(hwnd)}


def list_windows(exclude_own_process: bool = True) -> list[dict[str, Any]]:
    """Visible, titled top-level windows; no target is selected automatically."""
    _require_windows()
    result: list[dict[str, Any]] = []
    identities: dict[int, dict[str, Any] | None] = {}

    @WNDENUMPROC
    def callback(hwnd, _):
        try:
            if not _IsWindowVisible(hwnd) or not _title(hwnd).strip():
                return True
            pid = _pid(hwnd)
            if exclude_own_process and pid == os.getpid():
                return True
            geometry = get_window_geometry(hwnd, include_title=True)
            if geometry["cloaked"] or min(geometry["client_size"]) <= 0:
                return True
            if pid not in identities:
                try:
                    identities[pid] = process_identity(pid)
                except NativeError:
                    identities[pid] = None
            process = identities[pid]
            row = {**geometry, "exe": None, "process_start_time": None,
                   "class_name": _class_name(hwnd), "identity_available": process is not None}
            if process:
                row.update(process)
            result.append(row)
        except (NativeError, OSError):
            # A window can disappear naturally while EnumWindows runs.
            pass
        return True

    ct.set_last_error(0)
    if not _EnumWindows(callback, 0):
        if ct.get_last_error() == 0:
            raise NativeError("interactive_desktop_unavailable: EnumWindows returned 0 without a Win32 error")
        raise _error("EnumWindows")
    return sorted(result, key=lambda row: (row["title"].casefold(), row["hwnd"]))


def validate_target(identity: dict[str, Any], require_visible: bool = True) -> tuple[bool, str]:
    try:
        current = window_identity(int(identity["hwnd"]))
        for key in ("pid", "process_start_time"):
            if current[key] != identity.get(key):
                return False, "target_identity_changed"
        if not identity.get("exe") or os.path.normcase(current["exe"]) != os.path.normcase(identity["exe"]):
            return False, "target_identity_changed"
        if identity.get("class_name") and current["class_name"] != identity["class_name"]:
            return False, "target_identity_changed"
        geometry = get_window_geometry(current["hwnd"])
        if require_visible:
            if not geometry["visible"] or geometry["cloaked"]:
                return False, "target_hidden"
            if geometry["minimized"]:
                return False, "target_minimized"
            if min(geometry["client_size"]) <= 0:
                return False, "target_empty_client"
        return True, "ok"
    except (NativeError, OSError, KeyError, TypeError, ValueError) as error:
        return False, str(error)


def foreground_belongs_to_target(identity: dict[str, Any], allowed_hwnds: Iterable[int] = (),
                                 allow_same_process: bool = True) -> bool:
    """Allow explicit assistant controls without focusing or activating anything."""
    if not validate_target(identity, require_visible=True)[0]:
        return False
    foreground = current_foreground_hwnd()
    if not foreground:
        return False
    if foreground == int(identity["hwnd"]) or foreground in {int(value) for value in allowed_hwnds}:
        return True
    foreground_root = _GetAncestor(foreground, GA_ROOTOWNER)
    if foreground_root and foreground_root == _GetAncestor(identity["hwnd"], GA_ROOTOWNER):
        return True
    try:
        return bool(allow_same_process and _pid(foreground) == int(identity["pid"]))
    except NativeError:
        return False


def get_overlay_style(hwnd: int) -> int:
    return int(_GetWindowLongPtr(_hwnd(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF


def set_capture_exclusion(hwnd: int, excluded: bool = True) -> dict[str, Any]:
    hwnd = _require_own_window(hwnd)
    requested = WDA_EXCLUDEFROMCAPTURE if excluded else WDA_NONE
    ct.set_last_error(0)
    success = bool(_SetWindowDisplayAffinity(hwnd, requested))
    error = ct.get_last_error() if not success else 0
    observed = wt.DWORD()
    readback = bool(_GetWindowDisplayAffinity(hwnd, ct.byref(observed)))
    return {"success": success, "requested": requested,
            "observed": int(observed.value) if readback else None, "winerror": error}


def configure_overlay(hwnd: int, click_through: bool = True,
                      exclude_capture: bool = True) -> dict[str, Any]:
    """Add OS-level whole-window pass-through; Qt still supplies per-pixel alpha.

    Deliberately never calls SetLayeredWindowAttributes: it can invalidate Qt's
    subsequent per-pixel UpdateLayeredWindow rendering.
    """
    hwnd = _require_own_window(hwnd)
    previous = get_overlay_style(hwnd)
    style = (previous | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    style = style | WS_EX_TRANSPARENT if click_through else style & ~WS_EX_TRANSPARENT
    ct.set_last_error(0)
    old = _SetWindowLongPtr(hwnd, GWL_EXSTYLE, style)
    if old == 0 and ct.get_last_error():
        raise _error("SetWindowLongPtrW")
    if not _SetWindowPos(hwnd, None, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED):
        raise _error("SetWindowPos(style refresh)")
    observed = get_overlay_style(hwnd)
    expected = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        expected |= WS_EX_TRANSPARENT
    if observed & expected != expected:
        raise NativeError("overlay_style_verification_failed")
    affinity = set_capture_exclusion(hwnd, exclude_capture)
    return {"style": observed, "click_through": bool(observed & WS_EX_TRANSPARENT),
            "layered": bool(observed & WS_EX_LAYERED), "no_activate": bool(observed & WS_EX_NOACTIVATE),
            "capture_excluded": bool(affinity["success"] and affinity["observed"] == WDA_EXCLUDEFROMCAPTURE),
            "capture_exclusion": affinity}


def place_overlay(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    """Position this process's overlay in physical desktop pixels without focus."""
    hwnd = _require_own_window(hwnd)
    if width <= 0 or height <= 0:
        raise ValueError("Overlay width and height must be positive")
    with physical_dpi_context():
        if not _SetWindowPos(hwnd, HWND(-1), int(x), int(y), int(width), int(height),
                             SWP_NOACTIVATE | SWP_NOOWNERZORDER):
            raise _error("SetWindowPos(overlay)")


@dataclass(frozen=True)
class CaptureResult:
    image: Any
    method: str
    geometry: dict[str, Any]
    captured_at: float
    suspect_blank: bool
    occlusion_possible: bool = False


def capture_window_printwindow(hwnd: int, expected_identity: dict[str, Any] | None = None,
                              render_full_content: bool = True) -> CaptureResult:
    """Explicit client capture fallback. PrintWindow may block: use a worker.

    Hardware-accelerated emulators can return black or stale frames even when the
    native call reports success. Caller must validate scene/freshness separately.
    """
    from PIL import Image

    hwnd = _hwnd(hwnd)
    if expected_identity is not None:
        valid, reason = validate_target(expected_identity)
        if not valid or hwnd != int(expected_identity["hwnd"]):
            raise NativeError(reason if not valid else "target_identity_changed")
    geometry = get_window_geometry(hwnd)
    if not geometry["visible"] or geometry["minimized"] or geometry["cloaked"]:
        raise NativeError("target_not_visible")
    width, height = geometry["client_size"]
    if width <= 0 or height <= 0 or width * height > 100_000_000:
        raise NativeError("invalid_capture_size")
    hdc = memory_dc = bitmap = previous = None
    try:
        with physical_dpi_context():
            hdc = _GetDC(hwnd)
            if not hdc:
                raise _error("GetDC")
            memory_dc = _CreateCompatibleDC(hdc)
            if not memory_dc:
                raise _error("CreateCompatibleDC")
            info = BITMAPINFO()
            info.bmiHeader.biSize = ct.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down BGRA, no vertical inversion
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            bits = ct.c_void_p()
            bitmap = _CreateDIBSection(hdc, ct.byref(info), 0, ct.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise _error("CreateDIBSection")
            previous = _SelectObject(memory_dc, bitmap)
            if not previous or previous == ct.c_void_p(-1).value:
                raise _error("SelectObject")
            ct.memset(bits, 0, width * height * 4)
            flags = PW_CLIENTONLY | (PW_RENDERFULLCONTENT if render_full_content else 0)
            if not _PrintWindow(hwnd, memory_dc, flags):
                raise _error("PrintWindow")
            pixels = ct.string_at(bits, width * height * 4)
        image = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX", width * 4, 1)
        current = get_window_geometry(hwnd)
        if current["client_screen_rect"] != geometry["client_screen_rect"]:
            raise NativeError("target_geometry_changed_during_capture")
        if expected_identity is not None and not validate_target(expected_identity)[0]:
            raise NativeError("target_identity_changed_during_capture")
        extrema = image.getextrema()
        suspect_blank = max(value[1] for value in extrema) <= 2 or all(value[1] - value[0] <= 1 for value in extrema)
        return CaptureResult(image, "PrintWindow-client", geometry, time.monotonic(), suspect_blank)
    finally:
        if memory_dc and previous and previous != ct.c_void_p(-1).value:
            _SelectObject(memory_dc, previous)
        if bitmap:
            _DeleteObject(bitmap)
        if memory_dc:
            _DeleteDC(memory_dc)
        if hdc:
            _ReleaseDC(hwnd, hdc)


def capture_window(hwnd: int, expected_identity: dict[str, Any] | None = None,
                   allow_screen_fallback: bool = False) -> CaptureResult:
    """PrintWindow first; screen-pixel fallback only when explicitly requested."""
    try:
        result = capture_window_printwindow(hwnd, expected_identity)
        if not result.suspect_blank or not allow_screen_fallback:
            return result
    except NativeError:
        if not allow_screen_fallback:
            raise
    from PIL import ImageGrab

    if expected_identity is not None:
        valid, reason = validate_target(expected_identity)
        if not valid:
            raise NativeError(reason)
    geometry = get_window_geometry(hwnd)
    if not geometry["visible"] or geometry["minimized"] or geometry["cloaked"]:
        raise NativeError("target_not_visible")
    with physical_dpi_context():
        image = ImageGrab.grab(bbox=geometry["client_screen_rect"], all_screens=True).convert("RGB")
    if image.size != tuple(geometry["client_size"]):
        raise NativeError("screen_capture_geometry_mismatch")
    if get_window_geometry(hwnd)["client_screen_rect"] != geometry["client_screen_rect"]:
        raise NativeError("target_geometry_changed_during_capture")
    extrema = image.getextrema()
    blank = max(value[1] for value in extrema) <= 2 or all(value[1] - value[0] <= 1 for value in extrema)
    return CaptureResult(image, "screen-client-fallback", geometry, time.monotonic(), blank, True)


__all__ = ["NativeError", "CaptureResult", "list_windows", "get_window_info", "get_window_geometry",
           "window_identity", "process_identity", "validate_target", "foreground_belongs_to_target",
           "physical_dpi_context", "enable_per_monitor_dpi_awareness", "enable_dpi_awareness",
           "current_foreground_hwnd", "configure_overlay",
           "set_capture_exclusion", "get_overlay_style", "place_overlay", "capture_window",
           "capture_window_printwindow", "client_crop_rect", "client_rect_to_screen",
           "rect_size", "translate_rect", "intersect_rect"]
