"""Pure temporary-file and mocked-constructor tests; no model inference or UI."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import performance_settings as settings
from vision import VisionEngine


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "data" / "settings.json"

    def test_missing_settings_preserves_047_two_threads_without_creating_file(self):
        self.assertEqual(settings.load_settings(self.path), ({"ocr_threads": 2}, []))
        self.assertFalse(self.path.exists())

    def test_auto_uses_logical_cpu_boundaries_and_unknown_cpu(self):
        for count, expected in ((1, 1), (2, 1), (3, 1), (4, 2), (7, 2), (8, 4), (32, 4), (0, 1), (True, 1)):
            with self.subTest(cpu=count):
                self.assertEqual(settings.resolve_ocr_threads("auto", count), expected)
        with patch.object(settings.os, "cpu_count", return_value=None):
            self.assertEqual(settings.resolve_ocr_threads("auto"), 1)
        with patch.object(settings.os, "cpu_count", return_value=16):
            self.assertEqual(settings.resolve_ocr_threads("auto"), 4)

    def test_manual_override_is_not_downgraded(self):
        for choice in (1, 2, 4, 8):
            self.assertEqual(settings.resolve_ocr_threads(choice, 1), choice)

    def test_invalid_choices_are_rejected_including_bool_float_and_numeric_string(self):
        for choice in (True, False, 0, 3, -1, 16, 2.0, "2", "AUTO", None):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                settings.validate_thread_choice(choice)

    def test_bad_json_defaults_and_warns_without_overwriting(self):
        self.path.parent.mkdir()
        for content in ("{broken", "[]", "null", '{"ocr_threads":NaN}'):
            self.path.write_text(content, encoding="utf-8")
            payload, warnings = settings.load_settings(self.path)
            self.assertEqual(payload["ocr_threads"], 2)
            self.assertTrue(warnings)
            self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_bad_choice_falls_back_but_retains_unknown_fields(self):
        self.path.parent.mkdir()
        self.path.write_text('{"ocr_threads":true,"panel":{"collapsed":true},"future":7}', encoding="utf-8")
        payload, warnings = settings.load_settings(self.path)
        self.assertEqual(payload, {"ocr_threads": 2, "panel": {"collapsed": True}, "future": 7})
        self.assertTrue(warnings)

    def test_atomic_save_preserves_unknown_fields_and_other_data_files(self):
        self.path.parent.mkdir()
        self.path.write_text('{"ocr_threads":1,"future":{"x":[1,2]}}', encoding="utf-8")
        planner = self.path.with_name("presets.json")
        calibration = self.path.with_name("calibration.json")
        planner.write_bytes(b"original planner")
        calibration.write_bytes(b"original calibration")
        saved = settings.save_settings(self.path, {"ocr_threads": 8, "window": "side"})
        self.assertEqual(saved["future"], {"x": [1, 2]})
        self.assertEqual(saved["ocr_threads"], 8)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), saved)
        self.assertEqual(planner.read_bytes(), b"original planner")
        self.assertEqual(calibration.read_bytes(), b"original calibration")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_replace_failure_preserves_original_and_cleans_temporary(self):
        settings.save_settings(self.path, {"ocr_threads": 2})
        before = self.path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaisesRegex(OSError, "synthetic"):
                settings.save_settings(self.path, {"ocr_threads": 4})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path for path in self.path.parent.iterdir() if path.name.endswith(".tmp")], [])

    def test_invalid_save_does_not_change_existing_file(self):
        settings.save_settings(self.path, {"ocr_threads": 4})
        before = self.path.read_bytes()
        for payload in ({"ocr_threads": True}, {"ocr_threads": 3}, {"other": float("nan")}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                settings.save_settings(self.path, payload)
            self.assertEqual(self.path.read_bytes(), before)


class MockOCR:
    parameters = []

    def __init__(self, *, params):
        self.params = dict(params)
        type(self).parameters.append(self.params)
        options = SimpleNamespace(intra_op_num_threads=params["EngineConfig.onnxruntime.intra_op_num_threads"],
                                  inter_op_num_threads=1, execution_mode="ORT_SEQUENTIAL")
        native = SimpleNamespace(get_providers=lambda: ["synthetic-no-inference"],
                                 get_session_options=lambda: options)
        self.text_rec = SimpleNamespace(session=SimpleNamespace(session=native))
        self.text_det = SimpleNamespace(session=SimpleNamespace(session=native))

    def _load_rec_model(self):
        self.rec_threads = self.params["EngineConfig.onnxruntime.intra_op_num_threads"]

    def _load_det_model(self):
        self.det_threads = self.params["EngineConfig.onnxruntime.intra_op_num_threads"]


class EngineThreadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.models = Path(self.temporary.name)
        for kind in ("rec", "det"):
            (self.models / f"PP-OCRv6_{kind}_small.onnx").write_bytes(b"synthetic-model-placeholder")

    def test_requested_threads_reach_both_model_loads_and_inter_stays_one(self):
        with patch("rapidocr.RapidOCR", MockOCR):
            for threads in (1, 2, 4, 8):
                with self.subTest(threads=threads):
                    engine = VisionEngine([], self.models, intra_op_num_threads=threads)
                    self.assertEqual(engine.engine.rec_threads, threads)
                    self.assertEqual(engine.engine.det_threads, threads)
                    self.assertEqual(engine.parameters["EngineConfig.onnxruntime.inter_op_num_threads"], 1)
                    self.assertNotIn("EngineConfig.onnxruntime.execution_mode", engine.parameters)
                    for component in ("text_det", "text_rec"):
                        self.assertEqual(engine.model_info["session_options"][component]["intra_op_num_threads"], threads)
                    self.assertEqual(engine.model_info["requested_intra_op_num_threads"], threads)
                    self.assertEqual(engine.model_info["requested_intra_op_threads"], threads)
                    self.assertEqual(engine.model_info["effective_threads"], {"det": threads, "rec": threads})
                    engine.close()

    def test_omitted_parameter_preserves_two_threads(self):
        with patch("rapidocr.RapidOCR", MockOCR):
            engine = VisionEngine([], self.models)
            self.assertEqual(engine.engine.rec_threads, 2)
            self.assertEqual(engine.engine.det_threads, 2)
            engine.close()

    def test_invalid_threads_fail_before_factory_or_model_access(self):
        with patch("rapidocr.RapidOCR", side_effect=AssertionError("Must not construct native OCR")) as factory:
            for value in (True, False, 0, 3, -1, 16, 2.0, "2", None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    VisionEngine([], self.models / "does-not-exist", intra_op_num_threads=value)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
