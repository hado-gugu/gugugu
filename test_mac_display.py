"""Exercise the macOS display path with fake framework calls and no live capture."""

from contextlib import ExitStack, nullcontext
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

import mac_capture
import mac_native
from vision import CaptureUnavailable


def display(display_id=17, *, main=True):
    profile = f"fixture-display-{display_id}"
    rect = (0, 0, 4, 3) if main else (-4, 0, 0, 3)
    return {"display_id": display_id, "title": f"Fixture display {display_id}",
            "is_main": main, "profile_id": profile, "exe": "macos-display",
            "class_name": profile, "client_screen_rect": rect, "client_size": (4, 3),
            "display_pixel_size": (8, 6), "display_rotation": 0.0}


class DisplayMetadataTests(unittest.TestCase):
    def test_primary_first_and_disconnected_displays_skipped(self):
        quartz = Mock(kCGErrorSuccess=0)
        quartz.CGGetActiveDisplayList.return_value = (0, (18, 17, 99), 3)
        quartz.CGMainDisplayID.return_value = 17
        quartz.CGDisplayIsActive.side_effect = lambda display_id: display_id != 99
        quartz.CGDisplayBounds.side_effect = lambda display_id: ((0 if display_id == 17 else -4, 0), (4, 3))
        quartz.CGDisplayCopyDisplayMode.return_value = object()
        quartz.CGDisplayModeGetPixelWidth.return_value = 8
        quartz.CGDisplayModeGetPixelHeight.return_value = 6
        quartz.CGDisplayRotation.return_value = 0
        quartz.CGDisplaySerialNumber.return_value = 0
        with patch.object(mac_native, "Quartz", quartz), patch.object(mac_native, "AppKit", Mock()), \
                patch.object(mac_native, "objc", SimpleNamespace(autorelease_pool=nullcontext)):
            rows = mac_native.list_displays()

        self.assertEqual([row["display_id"] for row in rows], [17, 18])
        self.assertTrue(rows[0]["is_main"])
        self.assertEqual(rows[1]["client_screen_rect"], (-4, 0, 0, 3))
        self.assertEqual(rows[1]["display_pixel_size"], (8, 6))
        self.assertNotEqual(rows[0]["profile_id"], rows[1]["profile_id"])


class DisplayCaptureTests(unittest.TestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(mac_capture.sys, "platform", "darwin"))
        patches.enter_context(patch.object(mac_capture.platform, "machine", return_value="arm64"))
        patches.enter_context(patch.object(mac_capture.platform, "mac_ver", return_value=("14.4", (), "")))
        self.permission = patches.enter_context(patch.object(mac_native, "has_capture_permission", return_value=True))
        self.geometry = patches.enter_context(patch.object(mac_native, "get_display_geometry", return_value=display()))
        self.foreground_pid = os.getpid() + 1000
        self.foreground = patches.enter_context(patch.object(mac_native, "foreground_process_id", return_value=self.foreground_pid))
        self.pixels = np.zeros((6, 8, 3), dtype=np.uint8)
        self.to_bgr = patches.enter_context(patch.object(mac_capture, "_bgr_from_image", return_value=self.pixels))
        self.kit = Mock()
        self.image = object()
        self.chosen_display = SimpleNamespace(displayID=lambda: 17)
        other_display = SimpleNamespace(displayID=lambda: 18)
        self.own_app = SimpleNamespace(processID=lambda: os.getpid())
        self.content = SimpleNamespace(displays=lambda: [other_display, self.chosen_display],
                                       applications=lambda: [self.own_app], windows=lambda: [])
        self.kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_.side_effect = (
            lambda _desktop, _onscreen, done: done(self.content, None)
        )
        self.make_filter = self.kit.SCContentFilter.alloc.return_value.initWithDisplay_excludingApplications_exceptingWindows_
        self.make_filter.return_value.contentRect.return_value.size = SimpleNamespace(width=4, height=3)
        self.make_filter.return_value.pointPixelScale.return_value = 2.0
        self.config = self.kit.SCStreamConfiguration.alloc.return_value.init.return_value
        self.take_picture = self.kit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_
        self.take_picture.side_effect = lambda _filter, _config, done: done(self.image, None)
        patches.enter_context(patch.dict(sys.modules, {"ScreenCaptureKit": self.kit, "Quartz": Mock(),
                                                      "objc": SimpleNamespace(autorelease_pool=nullcontext)}))

    def test_selected_display_frame_uses_pixel_scale_and_no_audio(self):
        self.geometry.return_value = display(main=False)
        capture = mac_capture.DisplayCapture(17)
        capture.start()
        self.addCleanup(capture.stop)

        frame = capture.latest_frame()

        self.make_filter.assert_called_once_with(self.chosen_display, [self.own_app], [])
        self.config.setWidth_.assert_called_once_with(8)
        self.config.setHeight_.assert_called_once_with(6)
        self.config.setShowsCursor_.assert_called_once_with(False)
        self.config.setCapturesAudio_.assert_called_once_with(False)
        self.config.setCaptureMicrophone_.assert_called_once_with(False)
        self.to_bgr.assert_called_once_with(sys.modules["Quartz"], self.image, (8, 6))
        self.assertIs(frame.image_bgr, self.pixels)
        self.assertEqual(frame.geometry["client_screen_rect"], (-4, 0, 0, 3))
        self.assertEqual(frame.geometry["capture_pixel_size"], (8, 6))
        self.assertEqual(frame.geometry["foreground_pid"], self.foreground_pid)

    def test_missing_permission_stops_before_capture(self):
        self.permission.return_value = False
        with self.assertRaises(CaptureUnavailable) as failure:
            mac_capture.DisplayCapture(17).start()

        self.assertEqual(failure.exception.code, "permission_denied")
        self.take_picture.assert_not_called()

    def test_foreground_change_discards_the_frame(self):
        capture = mac_capture.DisplayCapture(17)
        capture.start()
        self.addCleanup(capture.stop)

        def change_focus(_filter, _config, done):
            self.foreground.return_value += 1
            done(self.image, None)

        self.take_picture.side_effect = change_focus
        with self.assertRaises(CaptureUnavailable):
            capture.latest_frame()
        self.take_picture.assert_called_once()

    def test_capture_requires_own_app_to_be_excluded(self):
        self.content.applications = lambda: []
        capture = mac_capture.DisplayCapture(17)
        capture.start()
        self.addCleanup(capture.stop)

        with self.assertRaises(CaptureUnavailable) as failure:
            capture.latest_frame()
        self.assertEqual(failure.exception.code, "display_filter_unavailable")
        self.take_picture.assert_not_called()

    def test_disconnected_display_stops_before_capture(self):
        capture = mac_capture.DisplayCapture(17)
        capture.start()
        self.addCleanup(capture.stop)
        self.geometry.side_effect = mac_native.NativeError("display_unavailable")

        with self.assertRaises(CaptureUnavailable):
            capture.latest_frame()
        self.take_picture.assert_not_called()


class MacControllerStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def test_controller_selects_main_display_without_starting_scan(self):
        import app

        native = Mock()
        native.has_capture_permission.return_value = True
        native.list_displays.return_value = [display(18, main=False), display()]
        native.get_display_geometry.return_value = display()
        native.list_windows.side_effect = AssertionError("macOS startup asked for game windows")
        capture_factory = Mock()

        with tempfile.TemporaryDirectory() as folder, patch.object(app, "IS_MACOS", True), \
                patch.object(app, "native", native), patch.object(app, "DisplayCapture", capture_factory, create=True):
            controller = app.Controller(Path(folder))
            try:
                self.assertEqual(controller.target["display_id"], 17)
                self.assertFalse(controller.scanning)
                native.list_windows.assert_not_called()
                native.request_capture_permission.assert_not_called()
                capture_factory.return_value.start.assert_not_called()
            finally:
                controller.timer.stop()
                controller.executor.shutdown(wait=True)
                controller.overlay.deleteLater()
                controller.deleteLater()


if __name__ == "__main__":
    unittest.main()
