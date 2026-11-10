"""Pure geometry checks; optional Win32 checks only read existing windows."""
import os
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import Mock, patch

import native


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.geometry = {"client_screen_rect": (-990, 130, -190, 730),
                         "capture_screen_rect": (-998, 100, -182, 738),
                         "window_rect": (-1006, 92, -174, 746)}

    def test_rects_preserve_negative_desktop_origin(self):
        self.assertEqual(native.rect_size((-990, 130, -190, 730)), (800, 600))
        self.assertEqual(native.translate_rect((0, 0, 30, 20), -990, 130), (-990, 130, -960, 150))

    def test_intersection(self):
        self.assertEqual(native.intersect_rect((0, 0, 20, 20), (10, -1, 30, 10)), (10, 0, 20, 10))
        self.assertIsNone(native.intersect_rect((0, 0, 10, 10), (10, 0, 20, 10)))

    def test_wgc_already_client(self):
        self.assertEqual(native.client_crop_rect((800, 600), self.geometry), (0, 0, 800, 600))

    def test_wgc_visible_frame(self):
        self.assertEqual(native.client_crop_rect((816, 638), self.geometry), (8, 30, 808, 630))

    def test_wgc_full_window(self):
        self.assertEqual(native.client_crop_rect((832, 654), self.geometry), (16, 38, 816, 638))

    def test_unknown_capture_size_is_rejected(self):
        with self.assertRaisesRegex(native.NativeError, "geometry_mismatch"):
            native.client_crop_rect((1200, 900), self.geometry)

    def test_impossible_client_crop_is_rejected(self):
        invalid = {"client_screen_rect": (20, 20, 820, 620),
                   "window_rect": (30, 30, 846, 668)}
        with self.assertRaises(native.NativeError):
            native.client_crop_rect((816, 638), invalid)

    def test_overlay_roi_exact_translation(self):
        self.assertEqual(native.client_rect_to_screen((0, 0, 800, 600), self.geometry), (-990, 130, -190, 730))
        self.assertEqual(native.client_rect_to_screen((100, 400, 200, 500), self.geometry), (-890, 530, -790, 630))

    def test_overlay_roi_outside_client_is_rejected(self):
        for rect in [(-1, 0, 20, 20), (0, 0, 801, 20), (0, 0, 0, 20)]:
            with self.assertRaises(native.NativeError):
                native.client_rect_to_screen(rect, self.geometry)


@unittest.skipUnless(native.IS_WINDOWS, "Win32 geometry bindings exist only on Windows")
class CaptureGeometryTitleTests(unittest.TestCase):
    def setUp(self):
        # Exercise the actual geometry helper without a desktop or real HWND.
        # A title lookup must be absent even for a same-process capture target.
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        replacements = {
            "_hwnd": lambda hwnd: int(hwnd),
            "physical_dpi_context": nullcontext,
            "_GetWindowRect": self.window_rect,
            "_GetClientRect": self.client_rect,
            "_ClientToScreen": self.client_to_screen,
            "_DwmGetWindowAttribute": lambda *args: 1,
            "_GetDpiForWindow": lambda hwnd: 144,
            "_IsWindowVisible": lambda hwnd: True,
            "_IsIconic": lambda hwnd: False,
            "_pid": lambda hwnd: os.getpid(),
        }
        for name, replacement in replacements.items():
            self.patches.enter_context(patch.object(native, name, replacement))
        self.title = Mock(side_effect=AssertionError("capture tried to read a window title"))
        self.patches.enter_context(patch.object(native, "_title", self.title))

    @staticmethod
    def window_rect(hwnd, pointer):
        pointer._obj.left, pointer._obj.top = 100, 100
        pointer._obj.right, pointer._obj.bottom = 930, 740
        return True

    @staticmethod
    def client_rect(hwnd, pointer):
        pointer._obj.left, pointer._obj.top = 0, 0
        pointer._obj.right, pointer._obj.bottom = 800, 600
        return True

    @staticmethod
    def client_to_screen(hwnd, pointer):
        pointer._obj.x += 110
        pointer._obj.y += 130
        return True

    def test_capture_geometry_never_reads_title_by_default(self):
        geometry = native.get_window_geometry(42)
        self.assertEqual(geometry["client_screen_rect"], (110, 130, 910, 730))
        self.assertEqual(geometry["client_size"], (800, 600))
        self.assertEqual(geometry["title"], "")
        self.title.assert_not_called()

    def test_explicit_metadata_geometry_reads_title(self):
        self.title.side_effect = None
        self.title.return_value = "Owned test fixture"
        geometry = native.get_window_geometry(42, include_title=True)
        self.assertEqual(geometry["title"], "Owned test fixture")
        self.title.assert_called_once_with(42)

    def test_window_info_explicitly_requests_title(self):
        self.title.side_effect = None
        self.title.return_value = "Owned test fixture"
        with patch.object(native, "window_identity", return_value={"hwnd": 42, "pid": os.getpid()}):
            info = native.get_window_info(42)
        self.assertEqual(info["title"], "Owned test fixture")
        self.title.assert_called_once_with(42)

    def test_window_inventory_still_returns_readable_titles(self):
        self.title.side_effect = None
        self.title.return_value = "Owned test fixture"
        identity = {"pid": os.getpid(), "exe": "fixture.exe", "process_start_time": 1}
        with patch.object(native, "_EnumWindows", side_effect=lambda callback, parameter: bool(callback(42, parameter))), \
             patch.object(native, "_class_name", return_value="FixtureClass"), \
             patch.object(native, "process_identity", return_value=identity):
            windows = native.list_windows(exclude_own_process=False)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["title"], "Owned test fixture")
        self.assertEqual(windows[0]["client_size"], (800, 600))


@unittest.skipUnless(native.IS_WINDOWS and os.environ.get("STRONGHOLD_NATIVE_SMOKE") == "1",
                     "Read-only real Win32 checks are opt-in")
class ReadOnlyWin32Tests(unittest.TestCase):
    def test_query_own_process_identity(self):
        identity = native.process_identity(os.getpid())
        self.assertEqual(identity["pid"], os.getpid())
        self.assertGreater(identity["process_start_time"], 0)
        self.assertTrue(identity["exe"].lower().endswith(".exe"))

    def test_visible_window_inventory_and_geometry(self):
        try:
            windows = native.list_windows()
        except native.NativeError as error:
            if "interactive_desktop_unavailable" in str(error):
                self.skipTest(str(error))
            raise
        self.assertTrue(all(row["pid"] != os.getpid() for row in windows))
        usable = [row for row in windows if row["identity_available"] and not row["minimized"]]
        if not usable:
            self.skipTest("No accessible external window is available")
        selected = usable[0]
        current = native.get_window_info(selected["hwnd"])
        self.assertEqual(current["pid"], selected["pid"])
        self.assertTrue(native.validate_target(current)[0])
        width, height = current["client_size"]
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        wrong = {**current, "process_start_time": current["process_start_time"] + 1}
        self.assertEqual(native.validate_target(wrong), (False, "target_identity_changed"))
        with self.assertRaises(PermissionError):
            native.configure_overlay(selected["hwnd"])


if __name__ == "__main__":
    unittest.main()
