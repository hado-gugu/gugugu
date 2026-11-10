"""Synthetic frames only: no screenshot, OCR, window or network is accessed."""
import unittest

import cv2
import numpy as np

from shop_geometry import _best_group, detect_shop_grid, validate_shop_grid


class ShopGeometryTests(unittest.TestCase):
    roi = [0., 0., 1., 1.]

    def scene(self, count=5, *, empty=(), hover=None, extra_left=False, panels=True):
        image = np.full((340, 1320, 3), (15, 24, 20), np.uint8)
        lefts = [270 + index * 155 + (14 if index == count - 1 else 0) for index in range(count)]
        if extra_left:
            lefts.insert(0, 115)
        boxes = []
        for index, left in enumerate(lefts):
            top, width, height = 60, 140, 240
            if index == hover:
                left -= 4
                top -= 10
                width += 8
                height += 12
            right, bottom = left + width, top + height
            cv2.rectangle(image, (left, top), (right - 1, bottom - 1), (90, 125, 100), 1)
            if index not in empty:
                cv2.rectangle(image, (left + 12, top + 20), (right - 13, top + 165), (60, 90, 70), 1)
                cv2.rectangle(image, (left + 30, top + 40), (right - 31, top + 125), (130, 145, 135), 2)
            boxes.append([left, top, right, bottom])
        if panels:
            # Upgrade is a different width; funds is not a full card frame.
            cv2.rectangle(image, (55, 60), (164, 299), (90, 125, 100), 1)
            cv2.rectangle(image, (75, 125), (144, 195), (140, 150, 140), 2)
            cv2.line(image, (1130, 60), (1130, 299), (90, 125, 100), 1)
            cv2.rectangle(image, (1160, 140), (1240, 210), (100, 170, 190), 2)
        return image, boxes

    def test_outer_cards_exclude_avatar_upgrade_and_funds(self):
        image, boxes = self.scene()
        grid = detect_shop_grid(image, self.roi)
        self.assertIsNotNone(grid)
        self.assertEqual(len(grid["slots"]), 5)
        for slot, expected in zip(grid["slots"], boxes):
            actual = np.array(slot["box_norm"]) * [1320, 340, 1320, 340]
            np.testing.assert_allclose(actual, expected, atol=3)
            self.assertEqual(slot["name_roi_norm"], slot["box_norm"])
            self.assertEqual(slot["anchor_norm"], slot["box_norm"][:2])

    def test_empty_card_frames_are_not_removed(self):
        image, _ = self.scene(empty=(0, 2, 4))
        grid = detect_shop_grid(image, self.roi)
        self.assertIsNotNone(grid)
        self.assertEqual(len(grid["slots"]), 5)
        self.assertTrue(validate_shop_grid(image, grid, self.roi))

    def test_multiple_purchased_dashed_frames_keep_full_grid(self):
        solid, boxes = self.scene()
        previous = detect_shop_grid(solid, self.roi)
        dashed = solid.copy()
        for index in (0, 2, 4):
            left, top, right, bottom = boxes[index]
            cv2.rectangle(dashed, (left - 2, top - 2), (right + 1, bottom + 1), (15, 24, 20), -1)
            for x in range(left, right, 6):
                cv2.line(dashed, (x, top), (min(x + 2, right - 1), top), (90, 125, 100), 1)
                cv2.line(dashed, (x, bottom - 1), (min(x + 2, right - 1), bottom - 1), (90, 125, 100), 1)
            for y in range(top, bottom, 6):
                cv2.line(dashed, (left, y), (left, min(y + 2, bottom - 1)), (90, 125, 100), 1)
                cv2.line(dashed, (right - 1, y), (right - 1, min(y + 2, bottom - 1)), (90, 125, 100), 1)
        self.assertTrue(validate_shop_grid(dashed, previous, self.roi))
        current = detect_shop_grid(dashed, self.roi)
        self.assertIsNotNone(current)
        self.assertEqual(len(current["slots"]), 5)

    def test_three_card_shop_can_tolerate_one_local_hover(self):
        image, _ = self.scene(3)
        grid = detect_shop_grid(image, self.roi)
        self.assertIsNotNone(grid)
        hovered, _ = self.scene(3, hover=1)
        self.assertTrue(validate_shop_grid(hovered, grid, self.roi))
        current = detect_shop_grid(hovered, self.roi, previous=grid)
        self.assertEqual(current["layout_key"], grid["layout_key"])
        self.assertEqual(current["slots"], grid["slots"])
        self.assertEqual(current["diagnostics"]["path"], "validated_previous_frames")

    def test_new_level_expansion_changes_layout(self):
        image, _ = self.scene(4)
        grid = detect_shop_grid(image, self.roi)
        self.assertIsNotNone(grid)
        expanded, _ = self.scene(4, extra_left=True)
        self.assertFalse(validate_shop_grid(expanded, grid, self.roi))
        current = detect_shop_grid(expanded, self.roi, previous=grid)
        self.assertIsNotNone(current)
        self.assertEqual(len(current["slots"]), 5)
        self.assertNotEqual(current["layout_key"], grid["layout_key"])

    def test_resize_requires_new_detection_and_layout_key(self):
        image, _ = self.scene()
        grid = detect_shop_grid(image, self.roi)
        scaled = cv2.resize(image, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_NEAREST)
        self.assertFalse(validate_shop_grid(scaled, grid, self.roi))
        new = detect_shop_grid(scaled, self.roi, previous=grid)
        self.assertIsNotNone(new)
        self.assertEqual(len(new["slots"]), 5)
        self.assertNotEqual(new["layout_key"], grid["layout_key"])

    def test_no_or_insufficient_frame_evidence_returns_none(self):
        self.assertIsNone(detect_shop_grid(np.full((340, 1320, 3), 25, np.uint8), self.roi))
        image, _ = self.scene(2, panels=False)
        self.assertIsNone(detect_shop_grid(image, self.roi))

    def test_invalid_roi_and_moved_whole_row_do_not_validate(self):
        image, _ = self.scene()
        self.assertIsNone(detect_shop_grid(image, [0, 0, float("nan"), 1]))
        grid = detect_shop_grid(image, self.roi)
        moved = cv2.warpAffine(image, np.float32([[1, 0, -25], [0, 1, 0]]), (1320, 340))
        self.assertFalse(validate_shop_grid(moved, grid, self.roi))
        self.assertFalse(validate_shop_grid(image, grid, [.02, 0, 1, 1]))

    def test_fainter_outer_card_frames_win_over_stronger_nested_portraits(self):
        outer = [{"box": [270 + index * 155, 60, 410 + index * 155, 300], "quality": .70}
                 for index in range(3)]
        inner = [{"box": [280 + index * 155, 75, 400 + index * 155, 275], "quality": .95}
                 for index in range(3)]
        chosen = _best_group(outer + inner)
        self.assertIsNotNone(chosen)
        self.assertEqual([row["box"] for row in chosen], [row["box"] for row in outer])


if __name__ == "__main__":
    unittest.main()
