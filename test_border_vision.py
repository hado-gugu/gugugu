"""Pure synthetic regression tests; no OCR model, screenshots, or windows.

The oracle reads deliberately colored synthetic tags. It tests routing/cache and
coordinate behavior only, and makes no claim about real-game OCR accuracy.
"""
import copy
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

import border_vision
from border_vision import BorderVisionEngine
from vision import VisionEngine


NAMES = ["松果", "断崖", "忍冬", "哈洛德", "坚守盾牌", "盟约之币", "送葬人", "送葬人测试后缀"]
WIDTH, HEIGHT = 1160, 440
ROI = [0.0, 0.0, 1.0, 1.0]


def grid(key="layout-a", shift=0):
    slots = []
    for index in range(5):
        box = [(40 + 220 * index + shift) / WIDTH, 80 / HEIGHT,
               (230 + 220 * index + shift) / WIDTH, 350 / HEIGHT]
        slots.append({"id": f"{key}:{index}", "box_norm": box,
                      "name_roi_norm": box[:], "anchor_norm": box[:2]})
    return {"slots": slots, "layout_key": key, "frame_size": [WIDTH, HEIGHT],
            "roi_norm": ROI[:], "confidence": .99, "diagnostics": {}}


def scene(names=None, ys=None, shift=0):
    names = names or ["松果", "断崖", "忍冬", "哈洛德", "坚守盾牌"]
    ys = ys or [255, 255, 255, 215, 302]
    image = np.full((HEIGHT, WIDTH, 3), 170, dtype=np.uint8)
    for index, name in enumerate(names):
        left, top, right, bottom = 40 + index * 220 + shift, 80, 230 + index * 220 + shift, 350
        image[top:bottom, left:left + 2] = 40
        image[top:bottom, right - 2:right] = 40
        image[top:top + 2, left:right] = 40
        image[bottom - 2:bottom, left:right] = 40
        if not name:
            continue
        x, y = left + 12, ys[index]
        for char_index, char in enumerate(name):
            cell = x + char_index * 17
            for bit in range(5):
                if (ord(char) >> bit) & 1:
                    image[y + 2 + bit * 3:y + 4 + bit * 3, cell + 2:cell + 13] = 30
            image[y + 2:y + 20, cell + 2:cell + 4] = 30
            image[y + 18:y + 20, cell + 2:cell + 13] = 30
        image[y, x] = [70 + NAMES.index(name), 17, 211]
    return image


class Oracle:
    def __init__(self):
        self.calls = []
        self.omit = set()
        self.extra = []

    def __call__(self, image, *, offset_px, frame_size, min_height_px=0):
        self.calls.append({"shape": image.shape, "offset": offset_px, "frame_size": frame_size})
        rows = []
        for index, name in enumerate(NAMES):
            if name in self.omit:
                continue
            ys, xs = np.where((image[:, :, 0] == 70 + index) & (image[:, :, 1] == 17) & (image[:, :, 2] == 211))
            for x, y in zip(xs, ys):
                box = [x + offset_px[0], y + offset_px[1],
                       x + offset_px[0] + 17 * len(name), y + offset_px[1] + 22]
                rows.append(VisionEngine._line(name, .99, box, *frame_size))
        rows.extend(copy.deepcopy(self.extra))
        return rows, {"det_ms": 2., "rec_ms": 3., "recognized_lines": len(rows),
                      "detected_lines": len(rows), "filtered_small_lines": 0, "edge_clipped": False}


def synthetic_base_init(self, *args, **kwargs):
    # Explicitly skip RapidOCR initialization. Any accidental real inference
    # fails because this engine does not contain a native backend.
    self._lock = threading.Lock()
    self._closed = False
    self.engine = None
    self.min_score = .8
    self.max_frame_age_ms = 5000
    self.smart = True
    self.full_refresh_seconds = 10.
    self.empty_band_grace_seconds = .6
    self._layout = self._last_full = None
    self._layout_epoch = 0
    from vision import normalize_name
    self._names = {normalize_name(name): name for name in NAMES}


class BorderVisionTests(unittest.TestCase):
    def setUp(self):
        self.current_grid = grid()
        self.valid = True
        self.init_patch = patch.object(VisionEngine, "__init__", synthetic_base_init)
        self.detect_patch = patch.object(border_vision, "detect_shop_grid", side_effect=lambda *a, **k: copy.deepcopy(self.current_grid))
        self.validate_patch = patch.object(border_vision, "validate_shop_grid", side_effect=lambda *a, **k: self.valid)
        for item in (self.init_patch, self.detect_patch, self.validate_patch):
            item.start()
            self.addCleanup(item.stop)
        self.engine = BorderVisionEngine(NAMES)
        self.oracle = Oracle()
        self.engine._ocr_lines = self.oracle

    def analyze(self, image):
        result = self.engine.analyze(image, ROI, sequence=len(self.oracle.calls) + 1)
        self.assertIsNone(result["error"], result)
        return result

    @staticmethod
    def by_slot(result):
        return {row["border_slot_id"]: row for row in result["candidates"]}

    def test_first_scan_handles_high_operator_and_low_equipment(self):
        result = self.analyze(scene())
        self.assertEqual([row["text"] for row in result["candidates"]], NAMES[:5])
        self.assertEqual(len(self.oracle.calls), 1)
        self.assertEqual(result["diagnostics"]["path"], "border_initial_detection")
        for candidate, slot in zip(result["candidates"], self.current_grid["slots"]):
            box = slot["box_norm"]
            self.assertEqual(candidate["slot_roi_norm"], box)
            self.assertEqual(candidate["border_layout_key"], "layout-a")
            self.assertAlmostEqual(candidate["border_anchor_norm"][0], box[0] + .04 * (box[2] - box[0]))
            self.assertAlmostEqual(candidate["border_anchor_norm"][1], box[1] + .60 * (box[3] - box[1]))
            self.assertTrue(self.engine._contains(candidate["name_roi_norm"], candidate["box_norm"]))

    def test_purchase_clears_only_changed_slot_without_old_name_recovery(self):
        first = self.analyze(scene())
        second = self.analyze(scene(["松果", None, "忍冬", "哈洛德", "坚守盾牌"]))
        self.assertEqual([row["text"] for row in second["candidates"]], ["松果", "忍冬", "哈洛德", "坚守盾牌"])
        self.assertEqual(second["diagnostics"]["changed_slots"], 1)
        self.assertEqual(second["diagnostics"]["reused_slots"], 4)
        a, b = self.by_slot(first), self.by_slot(second)
        for slot_id in b:
            self.assertEqual(a[slot_id]["slot_revision"], b[slot_id]["slot_revision"])

    def test_frozen_store_refills_only_empty_slot(self):
        first = self.analyze(scene(["松果", None, "忍冬", "哈洛德", "坚守盾牌"]))
        before = len(self.oracle.calls)
        second = self.analyze(scene(["松果", "盟约之币", "忍冬", "哈洛德", "坚守盾牌"]))
        self.assertEqual(second["diagnostics"]["changed_slots"], 1)
        self.assertEqual(len(self.oracle.calls) - before, 1)
        self.assertEqual(self.by_slot(second)["layout-a:1"]["text"], "盟约之币")
        for slot_id, row in self.by_slot(first).items():
            self.assertEqual(row["slot_revision"], self.by_slot(second)[slot_id]["slot_revision"])

    def test_whole_refresh_uses_one_mosaic_and_maps_every_box_to_client(self):
        self.analyze(scene())
        before = len(self.oracle.calls)
        result = self.analyze(scene(["盟约之币", "忍冬", "哈洛德", "松果", "断崖"]))
        self.assertEqual(len(self.oracle.calls) - before, 1)
        self.assertEqual(result["diagnostics"]["path"], "border_batch_detection")
        self.assertEqual(len(result["candidates"]), 5)
        for index, candidate in enumerate(result["candidates"]):
            self.assertTrue(self.engine._contains(self.current_grid["slots"][index]["box_norm"], candidate["box_norm"]))
            self.assertAlmostEqual(candidate["box_px"][0], 52 + 220 * index)
            self.assertAlmostEqual(candidate["box_norm"][0], candidate["box_px"][0] / WIDTH)

    def test_equipment_can_be_first_and_operator_can_be_last(self):
        result = self.analyze(scene(["坚守盾牌", "断崖", "忍冬", "哈洛德", "松果"]))
        self.assertEqual(result["candidates"][0]["text"], "坚守盾牌")
        self.assertEqual(result["candidates"][-1]["text"], "松果")

    def test_short_prefix_to_long_name_checks_whole_slot_width(self):
        self.analyze(scene(["送葬人", "断崖", "忍冬", "哈洛德", "坚守盾牌"]))
        result = self.analyze(scene(["送葬人测试后缀", "断崖", "忍冬", "哈洛德", "坚守盾牌"]))
        self.assertEqual(self.by_slot(result)["layout-a:0"]["text"], "送葬人测试后缀")
        self.assertEqual(result["diagnostics"]["changed_slots"], 1)

    def test_new_title_above_nominal_band_is_searched_and_retained(self):
        self.analyze(scene())
        result = self.analyze(scene(["盟约之币", "断崖", "忍冬", "哈洛德", "坚守盾牌"], ys=[182, 255, 255, 215, 302]))
        candidate = self.by_slot(result)["layout-a:0"]
        self.assertEqual(candidate["text"], "盟约之币")
        self.assertLess(candidate["name_roi_norm"][1], candidate["box_norm"][1])
        before = len(self.oracle.calls)
        self.analyze(scene(["盟约之币", "断崖", "忍冬", "哈洛德", "坚守盾牌"], ys=[182, 255, 255, 215, 302]))
        self.assertEqual(before, len(self.oracle.calls))

    def test_duplicate_names_remain_two_different_border_slots(self):
        result = self.analyze(scene(["松果", "松果", "忍冬", "哈洛德", "坚守盾牌"]))
        matches = [row for row in result["candidates"] if row["text"] == "松果"]
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0]["border_slot_id"], matches[1]["border_slot_id"])
        self.assertNotEqual(matches[0]["border_anchor_norm"], matches[1]["border_anchor_norm"])

    def test_layout_change_full_scan_has_no_cached_old_identities(self):
        self.analyze(scene())
        self.current_grid = grid("layout-b", shift=10)
        self.valid = False
        result = self.analyze(scene([None, None, "忍冬", None, None], shift=10))
        self.assertEqual([row["text"] for row in result["candidates"]], ["忍冬"])
        self.assertEqual(result["candidates"][0]["border_slot_id"], "layout-b:2")
        self.assertEqual(result["diagnostics"]["path"], "border_initial_detection")

    def test_periodic_ocr_miss_does_not_restore_previous_equipment(self):
        image = scene()
        self.analyze(image)
        self.engine._border_layout["verified_at"] = time.monotonic() - 11
        self.engine._border_layout["changed_at"] = time.monotonic() - 2
        self.oracle.omit.add("坚守盾牌")
        result = self.analyze(image)
        self.assertEqual(result["diagnostics"]["path"], "border_periodic_detection")
        self.assertNotIn("坚守盾牌", [row["text"] for row in result["candidates"]])

    def test_geometry_failure_falls_back_without_border_fields_or_cache(self):
        self.analyze(scene())
        self.valid = False
        self.current_grid = None
        self.oracle.omit = set(NAMES)
        result = self.analyze(scene())
        self.assertEqual(result["diagnostics"]["geometry_source"], "fallback")
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(self.engine._border_layout)
        self.assertGreaterEqual(result["elapsed_ms"], result["diagnostics"]["geometry_ms"])

    def test_geometry_exception_falls_back_instead_of_replaying_names(self):
        self.analyze(scene())
        self.oracle.omit = set(NAMES)
        with patch.object(border_vision, "validate_shop_grid", side_effect=RuntimeError("synthetic geometry fault")):
            result = self.analyze(scene())
        self.assertEqual(result["diagnostics"]["geometry_source"], "fallback")
        self.assertEqual(result["diagnostics"]["geometry_reason"], "border_geometry_error")
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(self.engine._border_layout)

    def test_photometric_hover_reuses_slots_without_global_ocr(self):
        image = scene()
        first = self.analyze(image)
        before = len(self.oracle.calls)
        tinted = np.round(image.astype(np.float32) * .72 + 15).astype(np.uint8)
        result = self.analyze(tinted)
        self.assertEqual(result["diagnostics"]["path"], "unchanged_border_slots")
        self.assertEqual(len(self.oracle.calls), before)
        self.assertEqual([row["text"] for row in result["candidates"]], [row["text"] for row in first["candidates"]])

    def test_failed_geometry_is_rate_limited_but_context_change_retries(self):
        image = scene()
        with patch.object(border_vision, "detect_shop_grid", return_value=None) as detector:
            first = self.analyze(image)
            second = self.analyze(image)
            self.assertEqual(detector.call_count, 1)
            self.assertEqual(first["diagnostics"]["geometry_source"], "fallback")
            self.assertEqual(second["diagnostics"]["geometry_reason"], "border_retry_backoff")
            self.assertFalse(any("border_slot_id" in row for row in second["candidates"]))
            changed = self.engine.analyze(image, [0., .01, 1., 1.])
            self.assertIsNone(changed["error"])
            self.assertEqual(detector.call_count, 2)
            self.engine._border_geometry_retry["retry_at"] = time.monotonic() - .01
            self.engine.analyze(image, [0., .01, 1., 1.])
            self.assertEqual(detector.call_count, 3)

    def test_conflicting_same_size_names_in_one_slot_are_unknown(self):
        row = VisionEngine._line("断崖", .99, [52, 285, 86, 307], WIDTH, HEIGHT)
        self.oracle.extra = [row]
        result = self.analyze(scene())
        self.assertNotIn("layout-a:0", self.by_slot(result))

    def test_mosaic_gap_or_cross_tile_boxes_are_rejected_before_mapping(self):
        image = scene()
        regions = [self.current_grid["slots"][i]["box_norm"] for i in (0, 2)]
        # Tile1 y=12..282; gap=282..322; tile2 begins322.
        self.oracle.extra = [VisionEngine._line("盟约之币", .99, [15, 275, 83, 330], 214, 604),
                             VisionEngine._line("断崖", .99, [15, 290, 49, 312], 214, 604)]
        groups, _ = self.engine._read_regions(image, regions, (WIDTH, HEIGHT))
        self.assertEqual([[row["text"] for row in group] for group in groups], [["松果"], ["忍冬"]])

    def test_reset_during_ocr_does_not_repopulate_semantic_cache(self):
        oracle = self.oracle
        def reset_during_read(*args, **kwargs):
            rows = oracle(*args, **kwargs)
            self.engine.reset_layout()
            return rows
        self.engine._ocr_lines = reset_during_read
        result = self.engine.analyze(scene(), ROI)
        self.assertEqual(result["status"], "frame_stale")
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(self.engine._border_layout)



if __name__ == "__main__":
    unittest.main()
