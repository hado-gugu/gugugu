"""Offscreen controller tests with fake inference and no target windows."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np


class FakeEngine:
    def __init__(self, threads):
        self.threads = threads
        self.closed_on = None
        self.model_info = {"effective_threads": {"det": threads, "rec": threads}}

    def close(self):
        self.closed_on = threading.current_thread().name

    def analyze(self, image, roi, **kwargs):
        return {"status": "no_candidates", "sequence": kwargs["sequence"],
                "timestamp": kwargs["timestamp"], "frame_size": [120, 80],
                "candidates": [], "raw_lines": [], "diagnostics": {}, "elapsed_ms": 1.}


class ThreadControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        from app import Controller
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        with patch("app.native.list_windows", return_value=[]):
            self.controller = Controller(self.temporary.name)
        self.controller.timer.stop()
        self.controller.profile = {"roi": [0, 0, 1, 1], "reference_size": [120, 80]}
        self.controller.performance_settings["ocr_threads"] = 2
        self.controller.requested_threads = 2
        self.controller.engine_threads = 2
        self.controller.applied_threads = {"det": 2, "rec": 2}
        self.controller.engine = FakeEngine(2)
        self.controller.thread_selector.blockSignals(True)
        self.controller.thread_selector.setCurrentIndex(self.controller.thread_selector.findData(2))
        self.controller.thread_selector.blockSignals(False)
        self.addCleanup(self.cleanup_controller)

    def cleanup_controller(self):
        self.controller.scanning = False
        self.controller.timer.stop()
        self.controller.executor.shutdown(wait=True)
        self.controller.overlay.clear()
        self.controller.deleteLater()
        self.application.processEvents()

    def choose(self, count):
        self.controller.thread_selector.setCurrentIndex(self.controller.thread_selector.findData(count))

    def test_live_change_persists_and_does_not_close_an_inflight_engine(self):
        self.controller.scanning = True
        old = self.controller.engine
        pending = Future()
        self.controller.future = pending
        generation = self.controller.generation
        self.choose(4)
        self.assertEqual(self.controller.requested_threads, 4)
        self.assertGreater(self.controller.generation, generation)
        self.assertTrue(self.controller.scanning)
        self.assertIs(self.controller.future, pending)
        self.assertIsNone(old.closed_on)
        data = json.loads(self.controller.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(data["ocr_threads"], 4)
        self.assertIn("结束后", self.controller.thread_status.text())
        pending.set_result(None)

    def test_old_generation_result_is_not_accepted_after_setting_change(self):
        self.controller.scanning = True
        pending = Future()
        self.controller.future, self.controller.job_kind = pending, "recognition"
        self.controller.job_generation = self.controller.generation
        self.choose(4)
        pending.set_result({"old": True})
        with patch.object(self.controller, "accept_observation") as accept:
            self.controller.tick()
        accept.assert_not_called()
        self.assertTrue(self.controller.scanning)

    def test_reconfigure_closes_and_constructs_on_the_same_worker(self):
        old = self.controller.engine
        self.choose(4)
        frame = SimpleNamespace(image_bgr=np.zeros((80, 120, 3), np.uint8),
                                sequence=1, timestamp=time.monotonic())
        created_on = []

        def factory(names, **kwargs):
            created_on.append(threading.current_thread().name)
            self.assertEqual(kwargs["intra_op_num_threads"], 4)
            return FakeEngine(4)

        with patch("app.VisionEngine", side_effect=factory), patch.object(self.controller, "get_frame", return_value=frame):
            result = self.controller.executor.submit(self.controller.recognize).result(timeout=10)
        self.assertTrue(old.closed_on.startswith("visual-recognition"))
        self.assertEqual(created_on, [old.closed_on])
        self.assertEqual(result["ocr_threads"], 4)
        self.assertEqual(result["effective_threads"], {"det": 4, "rec": 4})
        self.assertFalse(self.controller.scanning)

    def test_equivalent_auto_and_manual_choice_reuses_engine(self):
        self.controller.performance_settings["ocr_threads"] = "auto"
        self.controller.requested_threads = self.controller.engine_threads = 4
        self.controller.applied_threads = {"det": 4, "rec": 4}
        self.controller.engine = FakeEngine(4)
        self.controller.thread_selector.blockSignals(True)
        self.controller.thread_selector.setCurrentIndex(self.controller.thread_selector.findData("auto"))
        self.controller.thread_selector.blockSignals(False)
        generation = self.controller.generation
        with patch("app.resolve_ocr_threads", return_value=4):
            self.choose(4)
        self.assertEqual(generation, self.controller.generation)
        self.assertIsNone(self.controller.engine.closed_on)

    def test_current_generation_preparation_error_stops_automatic_retry(self):
        from app import RecognitionPreparationError
        self.controller.scanning = True
        pending = Future()
        self.controller.future, self.controller.job_kind = pending, "recognition"
        self.controller.job_generation = self.controller.generation
        pending.set_exception(RecognitionPreparationError("test init failure"))
        with patch("app.logging.exception"):
            self.controller.tick()
        self.assertFalse(self.controller.scanning)
        self.assertIn("test init failure", self.controller.status.text())

    def test_old_configuration_error_cannot_stop_newer_request(self):
        from app import RecognitionPreparationError
        self.controller.scanning = True
        pending = Future()
        self.controller.future, self.controller.job_kind = pending, "recognition"
        self.controller.job_generation = self.controller.generation
        self.choose(4)
        pending.set_exception(RecognitionPreparationError("obsolete failure"))
        with patch("app.logging.exception"):
            self.controller.tick()
        self.assertTrue(self.controller.scanning)

    def test_requested_threads_do_not_masquerade_as_native_readback(self):
        self.controller.requested_threads = self.controller.engine_threads = 8
        self.controller.applied_threads = {"det": 0, "rec": 0}
        self.controller.update_thread_hint()
        self.assertIn("自动分配", self.controller.thread_status.text())
        self.assertNotIn("实际 8", self.controller.thread_status.text())

    def test_save_failure_keeps_current_request_and_scan_state(self):
        self.controller.scanning = True
        generation = self.controller.generation
        with patch("app.save_settings", side_effect=OSError("read-only fixture")):
            self.choose(4)
        self.assertEqual(self.controller.requested_threads, 2)
        self.assertEqual(self.controller.thread_selector.currentData(), 2)
        self.assertEqual(self.controller.generation, generation)
        self.assertTrue(self.controller.scanning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
