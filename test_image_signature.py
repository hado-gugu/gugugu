"""Hover perturbations must not hide a changed character or a new empty-slot name."""
from pathlib import Path
import unittest

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from image_signature import same_name_structure


def rendered_names(names, *, color=(235, 235, 235), font_size=28):
    image = Image.new("RGB", (1280, 84), (23, 29, 26))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", font_size)
    for index, name in enumerate(names):
        if name:
            draw.text((30 + index * 310, 18), name, font=font, fill=color)
    return np.ascontiguousarray(np.asarray(image)[:, :, ::-1])


def tint(image, gain, offset):
    return np.clip(image.astype(np.float32) * np.array(gain) + np.array(offset), 0, 255).astype(np.uint8)


class NameStructureTests(unittest.TestCase):
    def setUp(self):
        self.original = rendered_names(["深靛", "野鬃", "送葬人", "刺玫"])

    def test_uniform_dimming_and_tint_preserve_structure_and_strict_cache(self):
        hovered = tint(self.original, [.65, .65, .65], [18, 13, 9])
        self.assertIs(same_name_structure(self.original, hovered), True)
        self.assertIs(same_name_structure(self.original, hovered, strict=True), True)

    def test_one_hovered_card_is_not_ignored_but_its_tint_is_tolerated(self):
        hovered = self.original.copy()
        hovered[:, 310:620] = tint(hovered[:, 310:620], [.78, .88, .96], [11, 5, 1])
        self.assertIs(same_name_structure(self.original, hovered), True)
        changed = rendered_names(["深靛", "野繁", "送葬人", "刺玫"])
        changed[:, 310:620] = tint(changed[:, 310:620], [.78, .88, .96], [11, 5, 1])
        self.assertIs(same_name_structure(self.original, changed), False)
        self.assertIsNot(same_name_structure(self.original, changed, strict=True), True)

    def test_long_card_outline_does_not_become_a_changed_name(self):
        hovered = self.original.copy()
        cv2.rectangle(hovered, (5, 2), (306, 81), (90, 205, 160), 2)
        self.assertIs(same_name_structure(self.original, hovered), True)

    def test_single_character_change_in_a_long_row_is_detected(self):
        changed = rendered_names(["深蓝", "野鬃", "送葬人", "刺玫"])
        self.assertIs(same_name_structure(self.original, changed), False)
        self.assertIsNot(same_name_structure(self.original, changed, strict=True), True)

    def test_similar_single_character_is_never_a_strict_cache_hit(self):
        before = rendered_names(["未", "野鬃", "送葬人", "刺玫"])
        after = rendered_names(["末", "野鬃", "送葬人", "刺玫"])
        self.assertIsNot(same_name_structure(before, after, strict=True), True)
        self.assertIsNot(same_name_structure(before, after), True)

    def test_short_to_long_name_is_detected(self):
        changed = rendered_names(["深靛", "野鬃", "圣约送葬人", "刺玫"])
        self.assertIs(same_name_structure(self.original, changed), False)

    def test_new_name_in_an_initially_empty_slot_is_detected(self):
        before = rendered_names(["深靛", None, "送葬人", "刺玫"])
        self.assertIs(same_name_structure(before, self.original), False)

    def test_disappearing_dim_gray_name_is_detected(self):
        before = rendered_names(["深靛", "野鬃", "送葬人", "刺玫"], color=(65, 65, 65))
        after = rendered_names(["深靛", None, "送葬人", "刺玫"], color=(65, 65, 65))
        self.assertIs(same_name_structure(before, after), False)

    def test_translation_cannot_enable_strict_cache_reuse(self):
        shifted = np.roll(self.original, 1, axis=1)
        self.assertIsNot(same_name_structure(self.original, shifted, strict=True), True)
        self.assertIsNot(same_name_structure(self.original, shifted), False)

    def test_real_web_name_band_with_synthetic_hover(self):
        source = Path(__file__).parent / "samples" / "normal.png"
        if not source.exists():
            self.skipTest("Source screenshot not installed with the application.")
        image = cv2.imdecode(np.frombuffer(source.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        band = np.ascontiguousarray(image[486:569, 361:1613])
        hovered = tint(band, [.72, .72, .72], [16, 10, 6])
        self.assertIsNot(same_name_structure(band, hovered), False)
        self.assertIs(same_name_structure(band, hovered, strict=True), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
