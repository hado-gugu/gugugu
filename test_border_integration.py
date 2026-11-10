"""Pure frame/anchor boundary checks; no models, windows or user data."""
import copy
from types import SimpleNamespace
import unittest

import numpy as np

from marker_stability import anchor_border_candidates
from responsiveness import confirm_independent_names, filter_current_candidates, name_verification_regions


def candidate(name="测试干员", slot=0, layout="grid-a"):
    left = .1 + slot * .22
    return {"text": name, "score": .99, "box_norm": [left + .02, .65, left + .14, .70],
            "border_slot_id": slot, "border_layout_key": layout,
            "slot_roi_norm": [left, .3, left + .2, .95],
            "name_roi_norm": [left, .55, left + .2, .93],
            "border_anchor_norm": [left + .01, .60]}


def observation(sequence, candidates):
    return {"sequence": sequence, "timestamp": float(sequence), "frame_size": [1000, 600],
            "roi_norm": [0, .2, 1, 1], "candidates": candidates}


class BorderBoundaryTests(unittest.TestCase):
    def test_marker_stays_on_border_anchor_when_ocr_box_moves(self):
        first = candidate()
        moved = copy.deepcopy(first)
        moved["box_norm"] = [.13, .76, .28, .81]
        a = anchor_border_candidates([first], layout_key="grid-a")[0]
        b = anchor_border_candidates([moved], layout_key="grid-a")[0]
        self.assertEqual(a["anchor_norm"], b["anchor_norm"])
        self.assertEqual(a["slot_anchor_id"], b["slot_anchor_id"])
        self.assertNotIn("slot_anchor_id", first)

    def test_duplicate_names_in_different_slots_keep_separate_tokens(self):
        rows = anchor_border_candidates([candidate(slot=0), candidate(slot=2)], layout_key="grid-a")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["slot_anchor_id"], rows[1]["slot_anchor_id"])

    def test_conflicting_identities_in_one_slot_are_not_stacked(self):
        self.assertEqual(anchor_border_candidates([candidate("干员"), candidate("装备")], layout_key="grid-a"), [])

    def test_layout_change_cannot_reuse_confirmation_or_anchor_token(self):
        old, new = candidate(), candidate(layout="grid-b")
        a = anchor_border_candidates([old], layout_key="grid-a")[0]
        b = anchor_border_candidates([new], layout_key="grid-b")[0]
        self.assertNotEqual(a["slot_anchor_id"], b["slot_anchor_id"])
        self.assertFalse(confirm_independent_names(observation(1, [old]), observation(2, [new]))[0].get("independently_confirmed"))
        self.assertEqual(anchor_border_candidates([old], layout_key="grid-b"), [])
        self.assertEqual(anchor_border_candidates([old], layout_key=None), [])

    def test_same_border_slot_confirms_despite_ocr_title_height_jitter(self):
        old, current = candidate(), candidate()
        current["box_norm"] = [.12, .75, .25, .80]
        result = confirm_independent_names(observation(1, [old]), observation(2, [current]))
        self.assertTrue(result[0].get("independently_confirmed"))
        self.assertFalse(confirm_independent_names(observation(2, [current]), observation(2, [current]))[0].get("independently_confirmed"))

    def test_fallback_observation_does_not_confirm_border_identity(self):
        old, new = candidate(), candidate()
        for field in ("border_slot_id", "border_layout_key"):
            old.pop(field)
        self.assertFalse(confirm_independent_names(observation(1, [old]), observation(2, [new]))[0].get("independently_confirmed"))

    def test_single_remaining_card_verification_excludes_funds(self):
        row = candidate()
        result = observation(2, [row])
        self.assertEqual(name_verification_regions(result), [row["name_roi_norm"]])
        before = np.zeros((600, 1000, 3), np.uint8)
        after = before.copy()
        after[350:550, 850:950] = 240
        a = SimpleNamespace(image_bgr=before, sequence=1, timestamp=1.)
        b = SimpleNamespace(image_bgr=after, sequence=2, timestamp=2.)
        self.assertEqual(len(filter_current_candidates(result, a, b)), 1)

    def test_new_title_at_other_height_in_same_slot_is_not_ignored(self):
        row = candidate()
        before = np.zeros((600, 1000, 3), np.uint8)
        after = before.copy()
        after[510:530, 120:200] = 240
        a = SimpleNamespace(image_bgr=before, sequence=1, timestamp=1.)
        b = SimpleNamespace(image_bgr=after, sequence=2, timestamp=2.)
        self.assertEqual(filter_current_candidates(observation(2, [row]), a, b), [])

    def test_name_mapped_to_another_card_is_not_anchored_here(self):
        row = candidate()
        row["box_norm"] = [.40, .65, .52, .70]
        self.assertEqual(anchor_border_candidates([row], layout_key="grid-a"), [])

    def test_invalid_border_name_region_cannot_use_name_pitch_fallback(self):
        row = candidate()
        row["name_roi_norm"] = [.1, .55, .2, .93]
        self.assertEqual(name_verification_regions(observation(1, [row])), [])
        self.assertEqual(anchor_border_candidates([row], layout_key="grid-a"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
