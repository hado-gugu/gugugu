import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from calibration import validate_roi, profile_matches, save_profile, load_profiles, profile_key


class CalibrationTests(unittest.TestCase):
    def test_relative_roi_survives_scale_and_negative_desktop_origin(self):
        profile = {"roi": [.2, .5, .9, .99], "reference_size": [1600, 900]}
        self.assertTrue(profile_matches(profile, 1200, 675))
        self.assertFalse(profile_matches(profile, 1200, 900))

    def test_invalid_roi_never_saves(self):
        for roi in ([0, 0, 1.1, 1], [.5, .2, .4, .7], [0, 0, float("nan"), 1], [0, 0, .001, .001]):
            with self.assertRaises(ValueError):
                validate_roi(roi)

    def test_profile_roundtrip_and_failed_save_preserves_old(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calibration.json"
            identity = {"exe": "C:/App/browser.exe", "class_name": "BrowserWindow"}
            expected = save_profile(path, identity, [.2, .5, .9, .99], [1600, 900])
            self.assertEqual(expected, load_profiles(path)["profiles"][profile_key(identity)])
            old = path.read_bytes()
            with patch("calibration.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    save_profile(path, identity, [.1, .3, .8, .9], [1200, 675])
            self.assertEqual(old, path.read_bytes())


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def test_mac_hold_can_only_continue_a_shown_reminder_and_expires_on_tick(self):
        from app import Controller

        with tempfile.TemporaryDirectory() as folder, patch("app.native.list_windows", return_value=[]):
            widget = Controller(folder)
            widget.timer.stop()
            self.addCleanup(widget.deleteLater)
            self.addCleanup(widget.executor.shutdown, wait=True)
            widget.store.import_json((Path(__file__).parent / "samples/web-demo.json").read_text(encoding="utf-8"))
            now = time.monotonic()
            candidate = {"text": "野鬃", "score": .99, "box_norm": [.4, .8, .5, .9],
                         "slot_roi_norm": [.38, .6, .52, .93], "name_roi_norm": [.4, .8, .5, .9],
                         "border_slot_id": "one", "border_layout_key": "shop", "border_anchor_norm": [.4, .8]}
            held = {**candidate, "title_hold_until": now + .65}

            def accept(sequence, row):
                widget.accept_observation({"sequence": sequence, "timestamp": now, "candidates": [row],
                                           "frame_stable": True, "elapsed_ms": 20,
                                           "diagnostics": {"geometry_source": "borders", "layout_key": "shop"}})

            with patch("app.IS_MACOS", True), patch("app.time.monotonic", return_value=now) as clock:
                accept(1, held)
                self.assertEqual(widget.matches, [])
                self.assertFalse(widget.previous_keys)
                accept(2, candidate)
                self.assertEqual(len(widget.matches), 1)
                accept(3, held)
                self.assertEqual(len(widget.matches), 1)
                self.assertFalse(widget.previous_keys)
                clock.return_value = now + .66
                widget.tick()
                self.assertEqual(widget.matches, [])

    def test_old_observations_clear_after_preset_and_stop(self):
        from app import Controller
        with tempfile.TemporaryDirectory() as folder, patch("app.native.list_windows", return_value=[]):
            widget = Controller(folder)
            widget.store.import_json((Path(__file__).parent / "samples/web-demo.json").read_text(encoding="utf-8"))
            widget.scanning = True
            candidate = {"text": "野鬃", "score": .99, "box_norm": [.4, .8, .5, .9]}
            widget.accept_observation({"sequence": 1, "timestamp": time.monotonic(), "candidates": [candidate], "elapsed_ms": 20})
            self.assertEqual(widget.matches, [])
            widget.accept_observation({"sequence": 2, "timestamp": time.monotonic(), "candidates": [candidate], "elapsed_ms": 20})
            self.assertEqual(len(widget.matches), 1)
            widget.accept_observation({"sequence": 3, "timestamp": time.monotonic()-10,
                                       "candidates": [candidate], "elapsed_ms": 20})
            self.assertEqual(widget.matches, [])
            self.assertFalse(widget.previous_keys)
            self.assertEqual(widget.last_good, 0)
            widget.on_planner_changed()
            self.assertEqual(widget.matches, [])
            widget.stop_scan()
            self.assertEqual(widget.matches, [])
            widget.timer.stop()
            widget.executor.shutdown(wait=False)
            widget.deleteLater()

    def test_capture_shutdown_does_not_block_ui_thread(self):
        from app import Controller

        class SlowCapture:
            def stop(self):
                time.sleep(.18)

        with tempfile.TemporaryDirectory() as folder, patch("app.native.list_windows", return_value=[]):
            widget = Controller(folder)
            widget.capture = SlowCapture()
            widget.capture_running = True
            started = time.perf_counter()
            widget.stop_scan()
            self.assertLess(time.perf_counter()-started, .08)
            self.assertIsNotNone(widget.future)
            widget.future.result(timeout=2)
            self.assertFalse(widget.capture_running)
            widget.timer.stop()
            widget.executor.shutdown(wait=True)
            widget.deleteLater()

    def test_close_during_ocr_preserves_queued_capture_shutdown(self):
        from app import Controller
        release = threading.Event()
        entered = threading.Event()
        stopped = threading.Event()

        class Capture:
            def stop(self):
                stopped.set()

        def active_ocr():
            entered.set()
            release.wait(2)

        with tempfile.TemporaryDirectory() as folder, patch("app.native.list_windows", return_value=[]):
            widget = Controller(folder)
            widget.capture = Capture()
            widget.capture_running = True
            widget.future = widget.executor.submit(active_ocr)
            self.assertTrue(entered.wait(1))
            try:
                started = time.perf_counter()
                with patch("app.QApplication.instance", return_value=Mock()):
                    widget.closeEvent(Mock())
                self.assertLess(time.perf_counter()-started, .08)
                self.assertFalse(stopped.is_set())
            finally:
                release.set()
                widget.executor.shutdown(wait=True)
                widget.deleteLater()
            self.assertTrue(stopped.is_set())
            self.assertFalse(widget.capture_running)


if __name__ == "__main__":
    unittest.main()
