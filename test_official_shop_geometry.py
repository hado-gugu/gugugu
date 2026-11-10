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
from vision import VisionEngine


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
        closure = replace(geometry._CLOSURE, expand=geometry._CLOSURE.fold)
        alternate = replace(closure, key="alternate", name="模拟第二主题")
        self.themes = (closure, alternate)
        self.active = "closure"
        self.panel = "open"
        self.right_empty = False
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
        names = ("fold", "expand", "card-trim", "card-footer", "empty-mark")
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


if __name__ == "__main__":
    unittest.main()
