"""Independent transition regressions for faster, cached name-band processing.

The observation fixtures here are synthesized API inputs, not screenshots or an
OCR accuracy evaluation. They exercise the public controller boundary without
binding, capturing, or interacting with any external window. Pixel/layout tests
below isolate the OCR boundary while exercising real VisionEngine state.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).parent


def candidate(text: str, slot: int = 0, *, row_shift: float = 0.0) -> dict:
    """Distinct positions, including duplicate names in separate shop slots."""
    left = 0.20 + slot * 0.15
    return {"text": text, "raw_text": text, "score": .99,
            "box_norm": [left, .76 + row_shift, left + .12, .80 + row_shift]}


class ControllerTransitionRegressions(unittest.TestCase):
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
        self.controller.store.import_json((ROOT / "samples/web-demo.json").read_text(encoding="utf-8"))
        for name in ("幽灵鲨", "归溟幽灵鲨"):
            record = self.controller.catalog.resolve_name(name)
            if len(record) != 1:
                raise AssertionError("Regression fixture needs the actual catalogue name: " + name)
            self.controller.store.toggle_target(record[0]["id"], True)
        self.controller.scanning = True
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.controller.timer.stop()
        self.controller.overlay.clear()
        self.controller.executor.shutdown(wait=False, cancel_futures=True)
        self.controller.deleteLater()
        self.application.processEvents()

    def observe(self, sequence: int, rows: list[dict], **extra):
        observation = {"status": "ready" if rows else "no_candidates",
                       "sequence": sequence, "timestamp": time.monotonic(),
                       "candidates": rows, "elapsed_ms": 7.0}
        observation.update(extra)
        self.controller.accept_observation(observation)

    def names(self):
        return [row["name"] for row in self.controller.matches]

    def test_reused_capture_sequence_is_not_a_second_independent_confirmation(self):
        self.observe(41, [candidate("野鬃")])
        self.observe(41, [candidate("野鬃")])
        self.assertEqual(self.names(), [], "Reusing cached pixels must not manufacture confirmation")
        self.observe(42, [candidate("野鬃")])
        self.assertEqual(self.names(), ["野鬃"])

    def test_short_to_long_name_clears_old_label_before_confirming_new_label(self):
        self.observe(1, [candidate("幽灵鲨")])
        self.observe(2, [candidate("幽灵鲨")])
        self.assertEqual(self.names(), ["幽灵鲨"])
        self.observe(3, [candidate("归溟幽灵鲨")])
        self.assertEqual(self.names(), [], "The new long name must not inherit the old short name's stability")
        self.observe(4, [candidate("归溟幽灵鲨")])
        self.assertEqual(self.names(), ["归溟幽灵鲨"])

    def test_initially_empty_slot_can_become_occupied(self):
        self.observe(1, [])
        self.observe(2, [candidate("野鬃", 2)])
        self.assertEqual(self.names(), [])
        self.observe(3, [candidate("野鬃", 2)])
        self.assertEqual(self.names(), ["野鬃"])

    def test_same_operator_in_two_slots_keeps_two_independent_reminders(self):
        both = [candidate("野鬃", 0), candidate("野鬃", 2)]
        self.observe(1, both)
        self.observe(2, both)
        self.assertEqual(self.names(), ["野鬃", "野鬃"])
        self.assertNotEqual(self.controller.matches[0]["box_norm"], self.controller.matches[1]["box_norm"])
        self.observe(3, [candidate("野鬃", 2)])
        self.assertEqual(len(self.controller.matches), 1, "A disappeared duplicate must be removed immediately")

    def test_disappearance_clears_and_reappearance_requires_fresh_confirmation(self):
        self.observe(1, [candidate("野鬃")])
        self.observe(2, [candidate("野鬃")])
        self.assertEqual(self.names(), ["野鬃"])
        self.observe(3, [])
        self.assertEqual(self.names(), [])
        self.observe(4, [candidate("野鬃")])
        self.assertEqual(self.names(), [])
        self.observe(5, [candidate("野鬃")])
        self.assertEqual(self.names(), ["野鬃"])

    def test_layout_reflow_never_reuses_old_marker_coordinates(self):
        self.observe(1, [candidate("野鬃")])
        self.observe(2, [candidate("野鬃")])
        self.observe(3, [candidate("野鬃", row_shift=-.12)])
        self.assertEqual(self.names(), [])
        self.observe(4, [candidate("野鬃", row_shift=-.12)])
        self.assertEqual(self.names(), ["野鬃"])
        self.assertLess(self.controller.matches[0]["box_norm"][1], .70)

    def test_new_unmatched_text_does_not_keep_previous_target(self):
        self.observe(1, [candidate("深靛")])
        self.observe(2, [candidate("深靛")])
        self.assertEqual(self.names(), ["深靛"])
        self.observe(3, [candidate("深蓝")])
        self.assertEqual(self.names(), [])

    def test_verified_distinct_live_frame_allows_high_confidence_first_pass(self):
        self.observe(1, [candidate("野鬃")], frame_stable=True)
        self.assertEqual(self.names(), ["野鬃"])

    def test_verified_pixels_do_not_bypass_low_confidence_two_frame_confirmation(self):
        row = {**candidate("野鬃"), "score": .90}
        self.observe(1, [row], frame_stable=True)
        self.assertEqual(self.names(), [])
        self.observe(1, [row], frame_stable=True)
        self.assertEqual(self.names(), [], "Same sequence cannot confirm a low-confidence cached label")
        self.observe(2, [row], frame_stable=True)
        self.assertEqual(self.names(), ["野鬃"])

    def test_failed_live_frame_stability_clears_previous_reminders(self):
        self.observe(1, [candidate("野鬃")])
        self.observe(2, [candidate("野鬃")])
        self.assertEqual(self.names(), ["野鬃"])
        self.observe(3, [candidate("野鬃")], frame_stable=False, status="scene_changed")
        self.assertEqual(self.names(), [])
        self.assertFalse(self.controller.previous_keys)
        self.assertEqual(self.controller.last_good, 0)

    def test_live_frame_verification_never_bypasses_expired_timestamp(self):
        self.observe(1, [candidate("野鬃")], frame_stable=True, timestamp=time.monotonic() - 12)
        self.assertEqual(self.names(), [])


class SynthesizedShop:
    """Original synthetic card layout, not a composited or real-game screenshot.

    Chinese labels use real catalogue/official names, but geometry and decoration
    are test-created. The mock OCR oracle below deliberately returns no text if
    a requested crop cuts off any part of a name, so a stale tight word crop fails.
    """
    width, height = 1280, 720
    roi = [.22, .48, .99, .99]

    def __init__(self, names, *, y_shift=0, frozen=False):
        image = Image.new("RGB", (self.width, self.height), (12, 20, 16))
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
        self.lines = []
        for index, name in enumerate(names):
            left, top = 300 + index * 225, 385 + y_shift
            color = (33, 62, 78) if frozen else (25, 42, 31)
            draw.rectangle((left, top, left + 205, top + 275), fill=color, outline=(96, 156, 120), width=2)
            # Test art/noise ensures no text is not mistaken for a blank capture.
            draw.rectangle((left + 25, top + 25, left + 145, top + 130), fill=(65, 83, 72))
            if not name:
                continue
            x, y = left + 12, top + 200
            draw.text((x, y), name, font=font, fill=(241, 247, 243), stroke_width=0)
            bounds = draw.textbbox((x, y), name, font=font)
            self.lines.append({"text": name, "box": list(bounds)})
        self.image = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])


class CropCoverageOracle:
    """Deterministic mocked OCR boundary, not a recognition algorithm."""
    def __init__(self):
        self.scene = None
        self.calls = []

    def __call__(self, image_bgr, *, offset_px=(0, 0), frame_size=(1280, 720), min_height_px=0.0):
        x0, y0 = offset_px
        x1, y1 = x0 + image_bgr.shape[1], y0 + image_bgr.shape[0]
        self.calls.append([x0, y0, x1, y1])
        width, height = frame_size
        result, clipped = [], False
        for line in self.scene.lines:
            left, top, right, bottom = line["box"]
            overlaps = left < x1 and right > x0 and top < y1 and bottom > y0
            contains = x0 <= left and y0 <= top and right <= x1 and bottom <= y1
            if overlaps and not contains:
                clipped = True
            if contains:
                result.append({"text": line["text"], "raw_text": line["text"], "score": .99,
                               "box_px": [left, top, right, bottom],
                               "box_norm": [left / width, top / height, right / width, bottom / height]})
        return result, {"det_ms": 1.0, "rec_ms": 1.0,
                        "recognized_lines": len(result), "edge_clipped": clipped}


class NameBandTransitionRegressions(unittest.TestCase):
    """Exercise the real analyze/layout cache while mocking only OCR inference."""
    @classmethod
    def setUpClass(cls):
        from vision import VisionEngine
        if not hasattr(VisionEngine, "reset_layout"):
            raise RuntimeError("Fast VisionEngine API is not ready; do not treat this suite as passed")
        cls.engine = VisionEngine(["送葬人", "圣约送葬人", "野鬃", "刺玫", "深靛"])

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.engine.reset_layout()
        self.sequence = 0
        self.oracle = CropCoverageOracle()
        self.patcher = patch.object(self.engine, "_ocr_lines", side_effect=self.oracle)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def observe(self, scene, *, same_sequence=False):
        self.oracle.scene = scene
        if not same_sequence:
            self.sequence += 1
        return self.engine.analyze(scene.image, scene.roi, sequence=self.sequence,
                                   timestamp=time.monotonic())

    @staticmethod
    def names(observation):
        return [row["text"] for row in observation["candidates"]]

    def test_new_long_name_is_not_cut_to_old_word_width(self):
        first = self.observe(SynthesizedShop(["送葬人", "刺玫", "深靛"]))
        self.assertIn("送葬人", self.names(first))
        second = self.observe(SynthesizedShop(["圣约送葬人", "刺玫", "深靛"]))
        self.assertIn("圣约送葬人", self.names(second))
        self.assertNotIn("送葬人", self.names(second))
        for band in second["stability_regions_norm"]:
            self.assertAlmostEqual(band[0], SynthesizedShop.roi[0], places=2)
            self.assertAlmostEqual(band[2], SynthesizedShop.roi[2], places=2)

    def test_previously_unoccupied_column_is_not_left_out_of_cached_band(self):
        first = self.observe(SynthesizedShop(["野鬃", None, "深靛"]))
        self.assertEqual(self.names(first), ["野鬃", "深靛"])
        second = self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"]))
        self.assertEqual(self.names(second), ["野鬃", "刺玫", "深靛"])

    def test_duplicates_remain_distinct_after_cached_layout(self):
        scene = SynthesizedShop(["野鬃", "野鬃", "深靛"])
        self.observe(scene)
        result = self.observe(scene)
        duplicates = [row for row in result["candidates"] if row["text"] == "野鬃"]
        self.assertEqual(len(duplicates), 2)
        self.assertNotEqual(duplicates[0]["box_norm"], duplicates[1]["box_norm"])

    def test_frozen_tint_changes_reinspect_pixels_without_retaining_wrong_name(self):
        self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"]))
        calls_before = len(self.oracle.calls)
        frozen = self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"], frozen=True))
        self.assertGreater(len(self.oracle.calls), calls_before)
        self.assertEqual(self.names(frozen), ["野鬃", "刺玫", "深靛"])

    def test_disappeared_name_is_absent_on_next_analyze(self):
        self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"]))
        result = self.observe(SynthesizedShop([None, "刺玫", "深靛"]))
        self.assertEqual(self.names(result), ["刺玫", "深靛"])

    def test_row_reflow_triggers_full_shop_relocalization(self):
        self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"]))
        calls_before = len(self.oracle.calls)
        shifted = SynthesizedShop(["野鬃", "刺玫", "深靛"], y_shift=-70)
        temporarily_empty = self.observe(shifted)
        self.assertEqual(self.names(temporarily_empty), [])
        # A refresh animation gets a short grace period; a genuine row reflow
        # must still be found by the bounded full-shop probe after that period.
        with patch("vision.time.monotonic", return_value=time.monotonic() + .7):
            result = self.observe(shifted)
        self.assertEqual(self.names(result), ["野鬃", "刺玫", "深靛"])
        new_calls = self.oracle.calls[calls_before:]
        self.assertTrue(any(box[3] - box[1] > 200 for box in new_calls), "Lost row must fall back to the full ROI")
        self.assertLess(result["candidates"][0]["box_norm"][1], .75)

    def test_static_pixel_cache_keeps_source_sequence_without_running_ocr_again(self):
        scene = SynthesizedShop(["野鬃", "刺玫", "深靛"])
        first = self.observe(scene)
        calls_before = len(self.oracle.calls)
        second = self.observe(scene, same_sequence=True)
        self.assertEqual(second["sequence"], first["sequence"])
        self.assertEqual(self.names(second), self.names(first))
        self.assertEqual(len(self.oracle.calls), calls_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
