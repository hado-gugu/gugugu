"""Regression scenarios: hover, independent purchases, and genuine refreshes.

OCR responses below are explicit test inputs, not an OCR accuracy benchmark.
The actual controller/anchor/independent-frame confirmation paths are exercised.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from responsiveness import confirm_independent_names, filter_current_candidates


def row(name, slot, *, shift=0, score=.99):
    return {"text": name, "score": score,
            "box_norm": [.25+slot*.15+shift, .76+shift, .34+slot*.15+shift, .81+shift]}


def observation(sequence, rows, **extra):
    return {"status": "ready" if rows else "no_candidates", "sequence": sequence,
            "timestamp": time.monotonic()+sequence*.0001, "frame_size": [1200, 720],
            "candidates": copy.deepcopy(rows), "diagnostics": {}, "elapsed_ms": 20,
            **extra}


class IndependentNameTests(unittest.TestCase):
    def test_purchase_confirms_only_names_still_in_newer_frame(self):
        old = observation(1, [row("野鬃", 0), row("刺玫", 1), row("深靛", 2)])
        new = observation(2, [row("刺玫", 1), row("深靛", 2)])
        result = confirm_independent_names(old, new)
        self.assertEqual([r["text"] for r in result], ["刺玫", "深靛"])
        self.assertTrue(all(r.get("independently_confirmed") for r in result))

    def test_same_capture_and_low_confidence_cannot_manufacture_confirmation(self):
        old = observation(1, [row("野鬃", 0)])
        self.assertFalse(confirm_independent_names(old, old)[0].get("independently_confirmed"))
        new = observation(2, [row("野鬃", 0, score=.90)])
        self.assertFalse(confirm_independent_names(old, new)[0].get("independently_confirmed"))

    def test_duplicate_names_cannot_confirm_a_new_slot(self):
        old = observation(1, [row("野鬃", 0)])
        new = observation(2, [row("野鬃", 0), row("野鬃", 2)])
        result = confirm_independent_names(old, new)
        self.assertTrue(result[0].get("independently_confirmed"))
        self.assertFalse(result[1].get("independently_confirmed"))

    def test_different_name_or_moved_row_is_not_unchanged(self):
        old = observation(1, [row("野鬃", 0)])
        for new in (row("刺玫", 0), row("野鬃", 0, shift=-.12)):
            self.assertFalse(confirm_independent_names(old, observation(2, [new]))[0].get("independently_confirmed"))

    def test_verification_covers_new_suffix_beyond_old_short_name(self):
        from test_image_signature import rendered_names
        before = rendered_names(["阿", "野鬃", "送葬人", "刺玫"])
        after = rendered_names(["阿米娅", "野鬃", "送葬人", "刺玫"])
        source = SimpleNamespace(image_bgr=before, sequence=1, timestamp=1.)
        latest = SimpleNamespace(image_bgr=after, sequence=2, timestamp=2.)
        old = {"text": "阿", "score": .99, "box_norm": [30/1280, .25, 58/1280, .70]}
        result = filter_current_candidates({"candidates": [old], "roi_norm": [0, 0, 1, 1]}, source, latest)
        self.assertEqual(result, [], "The old short crop must not hide an appended name suffix")


class StableControllerTests(unittest.TestCase):
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
        self.controller.store.import_json((Path(__file__).parent/"samples/web-demo.json").read_text(encoding="utf-8"))
        self.controller.scanning = True
        self.addCleanup(self.cleanup_controller)

    def cleanup_controller(self):
        self.controller.executor.shutdown(wait=True)
        self.controller.overlay.clear()
        self.controller.deleteLater()
        self.application.processEvents()

    def test_ocr_box_jitter_does_not_move_marker_or_lose_confirmation(self):
        self.controller.accept_observation(observation(1, [row("野鬃", 0)], frame_stable=True))
        original = self.controller.matches[0]["anchor_norm"]
        for sequence, shift in enumerate((.001, -.0012, .0006, -.0005), 2):
            self.controller.accept_observation(observation(sequence, [row("野鬃", 0, shift=shift)]))
            self.assertEqual(self.controller.matches[0]["anchor_norm"], original)

    def test_cross_frame_purchase_keeps_other_reminders_without_global_clear(self):
        first = [row("野鬃", 0), row("折桠", 1), row("深靛", 2)]
        self.controller.accept_observation(observation(1, first, frame_stable=True))
        old_anchors = {r["name"]: r["anchor_norm"] for r in self.controller.matches}
        a, b = observation(2, first), observation(3, first[1:])
        image = np.zeros((720, 1200, 3), np.uint8)
        frames = [SimpleNamespace(image_bgr=image, sequence=o["sequence"], timestamp=o["timestamp"])
                  for o in (a, b)]
        self.controller.profile = {"roi": [.2, .4, 1, 1], "reference_size": [1200, 720]}
        self.controller.capture = SimpleNamespace(latest_frame=lambda **kw: frames[1])
        self.controller.engine = SimpleNamespace(analyze=Mock(side_effect=[a, b]))
        self.controller.engine_threads = self.controller.requested_threads
        with patch.object(self.controller, "get_frame", return_value=frames[0]), patch("app.name_frames_stable", return_value=False):
            newest = self.controller.recognize()
        self.controller.accept_observation(newest)
        self.assertEqual([r["name"] for r in self.controller.matches], ["折桠", "深靛"])
        self.assertTrue(all(r["anchor_norm"] == old_anchors[r["name"]] for r in self.controller.matches))
        self.assertEqual(newest["sequence"], 3)
        self.assertEqual(self.controller.engine.analyze.call_count, 2)

    def test_second_purchase_during_ocr_drops_only_that_newly_changed_slot(self):
        from test_fast_scenarios import SynthesizedShop, CropCoverageOracle
        scenes = [SynthesizedShop(names) for names in
                  (["野鬃", "折桠", "深靛"], [None, "折桠", "深靛"], [None, None, "深靛"])]
        reads, frames = [], []
        oracle = CropCoverageOracle()
        for sequence, scene in enumerate(scenes, 1):
            oracle.scene = scene
            candidates, _ = oracle(scene.image)
            result = observation(sequence, candidates, frame_size=[1280, 720],
                                 roi_norm=scene.roi, stability_regions_norm=[[.22, .78, .99, .90]])
            reads.append(result)
            frames.append(SimpleNamespace(image_bgr=scene.image, sequence=sequence, timestamp=result["timestamp"]))
        self.controller.accept_observation({**reads[0], "frame_stable": True})
        self.controller.profile = {"roi": scenes[0].roi, "reference_size": [1280, 720]}
        self.controller.engine = SimpleNamespace(analyze=Mock(side_effect=reads[:2]))
        self.controller.engine_threads = self.controller.requested_threads
        self.controller.capture = SimpleNamespace(latest_frame=Mock(side_effect=frames[1:]))
        with patch.object(self.controller, "get_frame", return_value=frames[0]):
            newest = self.controller.recognize()
        self.controller.accept_observation(newest)
        self.assertEqual([r["name"] for r in self.controller.matches], ["深靛"])
        self.assertEqual(self.controller.engine.analyze.call_count, 2, "Verification must not queue a third OCR")

    def test_confirmed_new_non_target_replaces_old_target_immediately(self):
        self.controller.accept_observation(observation(1, [row("野鬃", 0)], frame_stable=True))
        self.controller.accept_observation(observation(2, [row("送葬人", 0)], frame_stable=True))
        self.assertEqual(self.controller.matches, [])

    def test_temporary_unreadable_result_has_a_fixed_non_renewing_deadline(self):
        self.controller.accept_observation(observation(1, [row("野鬃", 0)], frame_stable=True))
        uncertain = observation(2, [], transient_uncertain=True)
        self.controller.accept_observation(uncertain)
        self.assertEqual(len(self.controller.matches), 1)
        started = self.controller.uncertain_since
        self.controller.accept_observation(observation(3, [], transient_uncertain=True))
        self.assertEqual(self.controller.uncertain_since, started)
        with patch("app.time.monotonic", return_value=started+.36):
            self.controller.expire_uncertain()
        self.assertEqual(self.controller.matches, [])

    def test_unavailable_capture_or_stop_never_gets_a_grace_period(self):
        self.controller.accept_observation(observation(1, [row("野鬃", 0)], frame_stable=True))
        self.controller.accept_observation(observation(2, [], status="frame_stale"))
        self.assertEqual(self.controller.matches, [])
        self.controller.accept_observation(observation(3, [row("野鬃", 0)], frame_stable=True))
        self.controller.stop_scan()
        self.assertEqual(self.controller.matches, [])


if __name__ == "__main__":
    unittest.main()
