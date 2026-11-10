"""Synthetic landmark and title-cache tests, without loading OCR models."""

from dataclasses import replace
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

import official_shop_geometry as geometry
import official_vision
from border_vision import BorderVisionEngine
from official_vision import OfficialShopEngine
from vision import CapturedFrame, VisionEngine, _rect_pixels


ROI = (.4, .75, .87, 1.)


def _fake_base_init(engine, *args, **kwargs):
    engine._lock = Lock()
    engine._closed = False
    engine._layout_epoch = 0
    engine.max_frame_age_ms = 5_000
    engine.min_score = .8
    engine._names = {name: name for name in ("松果", "断崖")}
    engine.engine = SimpleNamespace(text_rec=Mock(
        side_effect=lambda request: SimpleNamespace(
            txts=["松果"] * len(request.img), scores=[.99] * len(request.img)
        )
    ))


class OfficialShopTests(unittest.TestCase):
    def setUp(self):
        closure = replace(geometry._CLOSURE, expand=geometry._CLOSURE.fold,
                          covered=geometry._CLOSURE.empty)
        alternate = replace(closure, key="alternate", name="模拟第二主题")
        self.themes = (closure, alternate)
        self.active = "closure"
        self.panel = "open"
        self.right_empty = False
        self.right_covered = False
        self.frame = np.zeros((800, 1280, 3), dtype=np.uint8)
        for target in (
            patch.object(geometry, "_scaled_templates", side_effect=self._templates),
            patch.object(geometry, "_match", side_effect=self._match),
            patch.object(official_vision, "locate_official_shop", side_effect=self._locate),
            patch.object(VisionEngine, "__init__", _fake_base_init),
        ):
            target.start()
            self.addCleanup(target.stop)

    @staticmethod
    def _templates(theme, scale):
        names = ("fold", "expand", "card-trim", "card-footer", "empty-mark", "card-covered")
        return {name: (theme.key, name) for name in names}

    def _match(self, image, template, x, y, tolerance):
        theme_key, name = template
        if theme_key != self.active:
            return 0., x, y
        if name == "fold":
            matched = self.panel == "open"
        elif name == "expand":
            matched = self.panel == "closed"
        elif name == "empty-mark":
            matched = self.right_empty and x > .75 * image.shape[1]
        elif name == "card-covered":
            matched = self.right_covered and x > .75 * image.shape[1]
        else:
            matched = True
        return (.98 if matched else 0.), x, y

    def _locate(self, frame, roi):
        return geometry.locate_official_shop(frame, roi, themes=self.themes)

    def test_matching_layout_uses_client_coordinates_and_scale(self):
        small, recognized = self._locate(self.frame, ROI)
        full, _ = self._locate(np.zeros((1600, 2560, 3), dtype=np.uint8), ROI)

        self.assertTrue(recognized)
        self.assertEqual(small["diagnostics"]["scale"], .5)
        self.assertEqual(full["diagnostics"]["scale"], 1.)
        self.assertEqual(len(small["slots"]), 4)
        self.assertNotEqual(small["layout_key"], full["layout_key"])
        for small_slot, full_slot in zip(small["slots"], full["slots"]):
            np.testing.assert_allclose(small_slot["title_box_norm"], full_slot["title_box_norm"])

    def test_closed_landmark_blocks_manual_fallback_but_unknown_may_use_it(self):
        engine = OfficialShopEngine(allow_legacy=True)
        fallback = {"diagnostics": {}, "elapsed_ms": 0.}
        with patch.object(BorderVisionEngine, "analyze", return_value=fallback) as legacy:
            self.panel = "closed"
            self.assertEqual(self._locate(self.frame, ROI), (None, True))
            self.assertEqual(engine.analyze(self.frame, ROI)["candidates"], [])
            legacy.assert_not_called()
            engine.engine.text_rec.assert_not_called()

            self.panel = "unknown"
            self.assertEqual(self._locate(self.frame, ROI), (None, False))
            self.assertEqual(engine.analyze(self.frame, ROI)["candidates"], [])
            legacy.assert_not_called()
            result = OfficialShopEngine(allow_legacy=True).analyze(self.frame, ROI)
            self.assertTrue(result["diagnostics"]["official_theme_fallback"])
            legacy.assert_called_once()

    def test_visible_open_landmark_with_insufficient_slots_blocks_legacy(self):
        engine = OfficialShopEngine(allow_legacy=True)
        clipped_roi = (.6, *ROI[1:])
        self.assertEqual(self._locate(self.frame, clipped_roi), (None, True))
        with patch.object(BorderVisionEngine, "analyze") as legacy:
            self.assertEqual(engine.analyze(self.frame, clipped_roi)["candidates"], [])
            legacy.assert_not_called()
            engine.engine.text_rec.assert_not_called()

    def test_theme_switch_and_unknown_scene_do_not_reuse_cached_titles(self):
        engine = OfficialShopEngine(allow_legacy=True)
        first = engine.analyze(self.frame, ROI)
        unchanged = engine.analyze(self.frame, ROI)
        self.assertEqual(first["status"], "ready", first)
        self.assertTrue(unchanged["ocr_reused"])
        self.assertEqual(engine.engine.text_rec.call_count, 1)

        self.active = "alternate"
        second = engine.analyze(self.frame, ROI)
        self.assertEqual(second["status"], "ready", second)
        self.assertNotEqual(first["diagnostics"]["layout_key"], second["diagnostics"]["layout_key"])
        self.assertNotEqual(first["candidates"][0]["border_slot_id"], second["candidates"][0]["border_slot_id"])
        self.assertEqual(engine.engine.text_rec.call_count, 2)

        self.panel = "unknown"
        with patch.object(BorderVisionEngine, "analyze") as legacy:
            self.assertEqual(engine.analyze(self.frame, ROI)["candidates"], [])
            legacy.assert_not_called()
        self.panel = "open"
        self.assertEqual(engine.analyze(self.frame, ROI)["status"], "ready")
        self.assertEqual(engine.engine.text_rec.call_count, 3)

    def test_positive_empty_mark_wins_over_visible_trim_and_clears_title(self):
        engine = OfficialShopEngine()
        before = engine.analyze(self.frame, ROI)
        self.assertEqual(before["status"], "ready", before)
        self.right_empty = True
        grid, _ = self._locate(self.frame, ROI)
        self.assertTrue(grid["slots"][-1]["empty"])
        after = engine.analyze(self.frame, ROI)
        self.assertEqual(after["diagnostics"]["layout_key"], before["diagnostics"]["layout_key"])
        self.assertEqual([row["border_slot_id"] for row in after["candidates"]],
                         [row["border_slot_id"] for row in before["candidates"][:-1]])
        self.assertEqual(engine.engine.text_rec.call_count, 1)

    def _title_frame(self, background=30, ink=190, *, changed_stroke=False):
        frame = self.frame.copy()
        grid, _ = self._locate(frame, ROI)
        left, top, right, bottom = _rect_pixels(grid["slots"][-1]["title_box_norm"], 1280, 800)
        crop = frame[top:bottom, left:right]
        crop[:] = background
        crop[5:14, 12:15] = ink
        crop[5:8, 15:24] = ink
        crop[11:14, 15:23] = ink
        if changed_stroke:
            crop[11:14, 15:23] = background
        return frame

    @staticmethod
    def _stabilizing_engine(*reads):
        engine = OfficialShopEngine(stabilize_titles=True)
        engine.engine.text_rec.side_effect = [
            SimpleNamespace(txts=texts, scores=[.99] * len(texts)) for texts in reads
        ]
        return engine

    def _scan(self, engine, frame, sequence, timestamp):
        with patch.object(official_vision.time, "monotonic", return_value=timestamp):
            result = engine.analyze(frame, ROI, sequence=sequence, timestamp=timestamp)
        self.assertIsNone(result["error"], result)
        return result

    def test_unchanged_miss_retries_only_its_slot_once_after_delay(self):
        pixels = self._title_frame()
        for read in ("断崖", "未匹配"):
            with self.subTest(retry_text=read):
                engine = self._stabilizing_engine(["松果"] * 3 + ["未匹配"], [read])
                first = self._scan(engine, pixels, 10, 100.)
                self.assertEqual(len(first["candidates"]), 3)
                self._scan(engine, pixels, 11, 100.21)
                self._scan(engine, pixels, 10, 100.30)
                self.assertEqual(engine.engine.text_rec.call_count, 1)

                retry = self._scan(engine, pixels, 12, 100.30)
                later = self._scan(engine, pixels, 13, 101.)
                expected = ["松果"] * 3 + ([read] if read == "断崖" else [])
                self.assertEqual([row["text"] for row in retry["candidates"]], expected)
                self.assertEqual([row["text"] for row in later["candidates"]], expected)
                self.assertEqual(engine.engine.text_rec.call_count, 2)
                original = engine.engine.text_rec.call_args_list[0].args[0].img[-1]
                padded, = engine.engine.text_rec.call_args_list[1].args[0].img
                self.assertEqual(padded.shape, (original.shape[0] + 6, original.shape[1] + 10, 3))
                np.testing.assert_array_equal(padded[3:-3, 5:-5], original)

    def test_tinted_title_miss_holds_only_that_slot_and_never_extends_expiry(self):
        engine = self._stabilizing_engine(["松果"] * 3 + ["断崖"], ["未匹配"], ["未匹配"], ["未匹配"])
        self._scan(engine, self._title_frame(), 1, 100.)
        first_tint = self._scan(engine, self._title_frame(60, 140), 2, 100.1)
        later_pixels = self._title_frame(80, 160)
        later_tint = self._scan(engine, later_pixels, 3, 100.4)

        for result in (first_tint, later_tint):
            self.assertEqual([row["text"] for row in result["candidates"]], ["松果"] * 3 + ["断崖"])
            held, = [row for row in result["candidates"] if "title_hold_until" in row]
            self.assertAlmostEqual(held["title_hold_until"], 100.75)
            self.assertEqual(result["raw_lines"][-1]["raw_text"], "未匹配")
        expired = self._scan(engine, later_pixels, 4, 100.8)
        self.assertEqual([row["text"] for row in expired["candidates"]], ["松果"] * 3)

    def test_changed_title_or_conflicting_name_does_not_keep_old_identity(self):
        for recognized, changed in (("松果", False), ("未匹配", True)):
            with self.subTest(recognized=recognized, changed_stroke=changed):
                engine = self._stabilizing_engine(["松果"] * 3 + ["断崖"], [recognized])
                self._scan(engine, self._title_frame(), 1, 100.)
                current = self._scan(engine, self._title_frame(60, 140, changed_stroke=changed), 2, 100.1)
                self.assertEqual([row["text"] for row in current["candidates"]],
                                 ["松果"] * (3 if changed else 4))
                self.assertFalse(any("title_hold_until" in row for row in current["candidates"]))

    def test_later_frame_rechecks_held_slot_proof_occupancy_layout_and_expiry(self):
        engine = self._stabilizing_engine(["松果"] * 3 + ["断崖"], ["未匹配"])
        original, tinted = self._title_frame(), self._title_frame(60, 140)
        self._scan(engine, original, 1, 100.)
        held = self._scan(engine, tinted, 2, 100.1)

        def titles(pixels, timestamp=100.2):
            frame = CapturedFrame(pixels, 3, timestamp, {})
            return [row["text"] for row in engine.recheck_held_titles(held, frame)]

        self.assertEqual(titles(tinted), ["松果"] * 3 + ["断崖"])
        self.assertEqual(titles(self._title_frame(60, 140, changed_stroke=True)), ["松果"] * 3)
        self.assertEqual(titles(tinted, timestamp=100.8), ["松果"] * 3)
        for attribute, value in (("right_empty", True), ("right_covered", True), ("active", "alternate")):
            with self.subTest(attribute=attribute), patch.object(self, attribute, value):
                self.assertEqual(titles(tinted), ["松果"] * 3)
        self.assertEqual(engine.engine.text_rec.call_count, 2)

    def test_empty_or_switched_layout_cannot_resurrect_held_title(self):
        original, tinted = self._title_frame(), self._title_frame(60, 140)
        for attribute, value, reread in (("right_empty", True, ["未匹配"]),
                                         ("active", "alternate", ["松果"] * 3 + ["未匹配"])):
            with self.subTest(attribute=attribute):
                reads = [["松果"] * 3 + ["断崖"], ["未匹配"]]
                if attribute == "active":
                    reads.append(reread)
                reads.append(reread)
                engine = self._stabilizing_engine(*reads)
                self._scan(engine, original, 1, 100.)
                before = self._scan(engine, tinted, 2, 100.1)
                self.assertIn("title_hold_until", before["candidates"][-1])
                with patch.object(self, attribute, value):
                    changed = self._scan(engine, tinted, 3, 100.2)
                restored = self._scan(engine, tinted, 4, 100.3)
                for result in (changed, restored):
                    self.assertEqual([row["text"] for row in result["candidates"]], ["松果"] * 3)


if __name__ == "__main__":
    unittest.main()
