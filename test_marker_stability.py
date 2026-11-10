"""Independent spatial-marker behavior checks; no OCR, UI, capture, or game I/O."""
import copy
import math
import unittest

from marker_stability import SlotAnchorTracker


def row(left=.25, top=.75, width=.055, height=.025, *, identity="operator-a", name="野鬃"):
    return {"box_norm": [left, top, left + width, top + height],
            "kind": "operator", "id": identity, "name": name, "reminder": "target"}


class StableSlotAnchors(unittest.TestCase):
    def test_first_reference_is_the_name_top_left(self):
        tracker = SlotAnchorTracker()
        result = tracker.update([row()])[0]
        self.assertEqual(result["anchor_norm"], [.25, .75])
        self.assertTrue(result["slot_anchor_id"])

    def test_one_pixel_ocr_jitter_does_not_move_anchor_or_token(self):
        tracker = SlotAnchorTracker()
        original = tracker.update([row()])[0]
        for index in range(60):
            shift_x = (index % 3 - 1) / 1920
            shift_y = (index % 3 - 1) / 1080
            current = tracker.update([row(.25 + shift_x, .75 + shift_y)])[0]
            self.assertEqual(current["anchor_norm"], original["anchor_norm"])
            self.assertEqual(current["slot_anchor_id"], original["slot_anchor_id"])

    def test_identity_change_keeps_slot_anchor_but_returns_only_current_semantics(self):
        tracker = SlotAnchorTracker()
        original = tracker.update([row(identity="short", name="送葬人")])[0]
        changed = tracker.update([row(width=.13, identity="long", name="圣约送葬人")])[0]
        self.assertEqual(changed["slot_anchor_id"], original["slot_anchor_id"])
        self.assertEqual(changed["anchor_norm"], original["anchor_norm"])
        self.assertEqual(changed["id"], "long")
        self.assertEqual(changed["name"], "圣约送葬人")

    def test_left_center_and_right_aligned_name_growth_keeps_same_slot(self):
        for left, width in ((.30, .14), (.255, .14), (.21, .14)):
            with self.subTest(alignment=(left, width)):
                tracker = SlotAnchorTracker()
                original = tracker.update([row(.30, width=.05)])[0]
                changed = tracker.update([row(left, width=width)])[0]
                self.assertEqual(changed["slot_anchor_id"], original["slot_anchor_id"])
                self.assertEqual(changed["anchor_norm"], original["anchor_norm"])

    def test_same_identity_in_two_cards_stays_two_slots_even_if_input_order_changes(self):
        tracker = SlotAnchorTracker()
        first = tracker.update([row(.23), row(.61)])
        reversed_rows = tracker.update([row(.6105), row(.2295)])
        self.assertEqual(len({item["slot_anchor_id"] for item in first}), 2)
        self.assertEqual(reversed_rows[0]["slot_anchor_id"], first[1]["slot_anchor_id"])
        self.assertEqual(reversed_rows[1]["slot_anchor_id"], first[0]["slot_anchor_id"])

    def test_empty_frame_replays_nothing_but_can_reuse_geometry_when_card_returns(self):
        tracker = SlotAnchorTracker()
        first = tracker.update([row()])[0]
        self.assertEqual(tracker.update([]), [])
        new = tracker.update([row(identity="new-operator")])[0]
        self.assertEqual(new["slot_anchor_id"], first["slot_anchor_id"])
        self.assertEqual(new["id"], "new-operator")

    def test_proportional_resize_preserves_normalized_anchors(self):
        tracker = SlotAnchorTracker()
        physical = [600, 780, 720, 810]
        initial = [physical[0] / 1920, physical[1] / 1080, physical[2] / 1920, physical[3] / 1080]
        smaller = [physical[0] * .75 / 1440, physical[1] * .75 / 810,
                   physical[2] * .75 / 1440, physical[3] * .75 / 810]
        first = tracker.update([{"box_norm": initial, "identity": ("operator", "a")}], context_key=(7, (.2, .6, .9, .95)))[0]
        second = tracker.update([{"box_norm": smaller, "identity": ("operator", "a")}], context_key=(7, (.2, .6, .9, .95)))[0]
        self.assertEqual(second["anchor_norm"], first["anchor_norm"])
        self.assertEqual(second["slot_anchor_id"], first["slot_anchor_id"])

    def test_context_and_manual_resets_never_recycle_old_tokens(self):
        tracker = SlotAnchorTracker()
        first = tracker.update([row()], context_key=(1, (.2, .4, .9, .9)))[0]
        second = tracker.update([row()], context_key=(2, (.2, .4, .9, .9)))[0]
        self.assertNotEqual(first["slot_anchor_id"], second["slot_anchor_id"])
        self.assertEqual(tracker.last_reset_reason, "context_changed")
        tracker.reset()
        third = tracker.update([row()], context_key=(2, (.2, .4, .9, .9)))[0]
        self.assertNotEqual(second["slot_anchor_id"], third["slot_anchor_id"])

    def test_obviously_new_coordinate_is_not_assigned_an_old_anchor(self):
        tracker = SlotAnchorTracker()
        first = tracker.update([row(.18)])[0]
        moved = tracker.update([row(.66, .62)])[0]
        self.assertNotEqual(first["slot_anchor_id"], moved["slot_anchor_id"])
        self.assertEqual(moved["anchor_norm"], [.66, .62])

    def test_coherent_vertical_reflow_rebuilds_all_covered_slots(self):
        tracker = SlotAnchorTracker()
        positions = (.16, .34, .52, .70)
        first = tracker.update([row(x) for x in positions])
        moved = tracker.update([row(x, .69) for x in positions])
        self.assertEqual(tracker.last_reset_reason, "layout_reflow")
        self.assertFalse({r["slot_anchor_id"] for r in first} & {r["slot_anchor_id"] for r in moved})
        self.assertTrue(all(item["anchor_norm"][1] == .69 for item in moved))

    def test_coherent_horizontal_reflow_rebuilds_columns(self):
        tracker = SlotAnchorTracker()
        positions = (.16, .34, .52, .70)
        first = tracker.update([row(x) for x in positions])
        moved = tracker.update([row(x + .06) for x in positions])
        self.assertEqual(tracker.last_reset_reason, "layout_reflow")
        self.assertFalse({r["slot_anchor_id"] for r in first} & {r["slot_anchor_id"] for r in moved})

    def test_partial_candidate_jitter_never_resets_other_slots(self):
        tracker = SlotAnchorTracker()
        positions = (.16, .34, .52, .70)
        first = tracker.update([row(x) for x in positions])
        partial = tracker.update([row(positions[1] + .001), row(positions[3] - .001)])
        self.assertEqual([r["slot_anchor_id"] for r in partial], [first[1]["slot_anchor_id"], first[3]["slot_anchor_id"]])
        self.assertNotEqual(tracker.last_reset_reason, "layout_reflow")
        restored = tracker.update([row(x) for x in positions])
        self.assertEqual([r["slot_anchor_id"] for r in restored], [r["slot_anchor_id"] for r in first])

    def test_one_truly_moved_card_does_not_reset_unchanged_neighbors(self):
        tracker = SlotAnchorTracker()
        positions = (.16, .34, .52, .70)
        first = tracker.update([row(x) for x in positions])
        current = tracker.update([row(positions[0], .69), *[row(x) for x in positions[1:]]])
        self.assertNotEqual(current[0]["slot_anchor_id"], first[0]["slot_anchor_id"])
        self.assertEqual(current[0]["anchor_norm"], [positions[0], .69])
        self.assertEqual([r["slot_anchor_id"] for r in current[1:]], [r["slot_anchor_id"] for r in first[1:]])

    def test_all_names_growing_is_not_a_fake_layout_reflow(self):
        for alignment_shift in (0, -.04, -.08):
            with self.subTest(alignment_shift=alignment_shift):
                tracker = SlotAnchorTracker()
                positions = (.16, .34, .52, .70)
                first = tracker.update([row(x, width=.05) for x in positions])
                changed = tracker.update([row(x + alignment_shift, width=.13) for x in positions])
                self.assertEqual([r["slot_anchor_id"] for r in changed], [r["slot_anchor_id"] for r in first])
                self.assertNotEqual(tracker.last_reset_reason, "layout_reflow")

    def test_invalid_boxes_are_dropped_and_inputs_are_never_mutated(self):
        tracker = SlotAnchorTracker()
        candidates = [row(), {"box_norm": [0, 0, math.nan, .5]}, {"box_norm": [.5, .5, .1, .9]},
                      {"box_norm": None}, {"box_norm": {"0": 1, "1": 2, "2": 3, "3": 4}}, "not-a-row"]
        original = copy.deepcopy(candidates)
        result = tracker.update(candidates)
        self.assertEqual(len(result), 1)
        self.assertNotIn("anchor_norm", candidates[0])
        self.assertEqual(candidates[0], original[0])
        result[0]["anchor_norm"][0] = 0
        again = tracker.update([row()])[0]
        self.assertEqual(again["anchor_norm"], [.25, .75])


if __name__ == "__main__":
    unittest.main(verbosity=2)
