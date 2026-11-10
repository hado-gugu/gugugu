"""Spatially locked marker anchors, independent of OCR names and game state.

SlotAnchorTracker.update returns only current candidates. It never carries an
old identity/reminder into a new frame. ``anchor_norm`` is the stable top-left
name reference point; the painter can place its icon above that point. Positions
use normalized client coordinates so proportional window resizing is harmless.
The caller must reset on target/calibration changes (or pass context_key).
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import math
import statistics


@dataclass
class _Slot:
    slot_id: str
    reference_box: tuple[float, float, float, float]
    anchor: tuple[float, float]
    identity: object
    last_seen: int


def _box(value):
    try:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        left, top, right, bottom = (float(v) for v in value)
        if not all(math.isfinite(v) for v in (left, top, right, bottom)):
            return None
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            return None
        return left, top, right, bottom
    except (ValueError, TypeError, OverflowError):
        return None


def _center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def anchor_border_candidates(candidates, *, layout_key):
    """Anchor only current identities to independently detected card geometry.

    This path has no temporal name storage or OCR-box association. Duplicate
    identities in one physical slot are rejected rather than stacked.
    """
    if layout_key is None:
        return []
    rows = [row for row in candidates if isinstance(row, dict)]
    tokens = [str(row.get("border_slot_id")) for row in rows]
    output = []
    for row, token in zip(rows, tokens):
        if (row.get("border_slot_id") is None or tokens.count(token) != 1
                or row.get("border_layout_key") != layout_key):
            continue
        slot, box = _box(row.get("slot_roi_norm")), _box(row.get("box_norm"))
        name_region = _box(row.get("name_roi_norm"))
        anchor = row.get("border_anchor_norm")
        try:
            if slot is None or box is None or name_region is None or len(anchor) != 2:
                continue
            if not (slot[0] <= name_region[0] <= box[0] < box[2] <= name_region[2] <= slot[2]
                    and slot[1] <= name_region[1] <= box[1] < box[3] <= name_region[3] <= slot[3]):
                continue
            x, y = map(float, anchor)
            if (not math.isfinite(x) or not math.isfinite(y)
                    or not (slot[0] <= x <= slot[2] and slot[1] <= y <= slot[3])):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        output.append({**copy.deepcopy(row), "anchor_norm": [x, y],
                       "slot_anchor_id": f"border:{layout_key}:{token}"})
    return output


def _hungarian(costs):
    """Minimum-cost one-to-one assignment, with caller-supplied new-slot columns."""
    count = len(costs)
    if not count:
        return []
    columns = len(costs[0])
    u, v = [0.] * (count + 1), [0.] * (columns + 1)
    owner, way = [0] * (columns + 1), [0] * (columns + 1)
    for row in range(1, count + 1):
        owner[0], column = row, 0
        minimum, used = [float("inf")] * (columns + 1), [False] * (columns + 1)
        while True:
            used[column] = True
            current_row, delta, next_column = owner[column], float("inf"), 0
            for candidate_column in range(1, columns + 1):
                if used[candidate_column]:
                    continue
                value = costs[current_row - 1][candidate_column - 1] - u[current_row] - v[candidate_column]
                if value < minimum[candidate_column]:
                    minimum[candidate_column], way[candidate_column] = value, column
                if minimum[candidate_column] < delta:
                    delta, next_column = minimum[candidate_column], candidate_column
            for candidate_column in range(columns + 1):
                if used[candidate_column]:
                    u[owner[candidate_column]] += delta
                    v[candidate_column] -= delta
                else:
                    minimum[candidate_column] -= delta
            column = next_column
            if owner[column] == 0:
                break
        while column:
            previous = way[column]
            owner[column] = owner[previous]
            column = previous
    result = [-1] * count
    for column in range(1, columns + 1):
        if owner[column]:
            result[owner[column] - 1] = column - 1
    return result


class SlotAnchorTracker:
    """One spatial slot per observed card, never one slot per operator ID.

    Example::

        rows = tracker.update(candidates, context_key=(hwnd, tuple(profile['roi'])))
        # row['anchor_norm'] -> [name_left, name_top], fixed until a true reflow
        # row['slot_anchor_id'] -> stable token; combine with kind/id for semantics

    A word can grow left-, center-, or right-aligned: association accepts a
    stable corresponding edge/center, while the marker keeps its first anchor.
    It deliberately does not infer unknown physical card boundaries from names.
    """

    def __init__(self, *, max_horizontal_drift=.03, max_vertical_drift=.012,
                 row_reflow_threshold=.018, horizontal_reflow_threshold=.035,
                 max_tracks=32):
        self.max_horizontal_drift = float(max_horizontal_drift)
        self.max_vertical_drift = float(max_vertical_drift)
        self.row_reflow_threshold = float(row_reflow_threshold)
        self.horizontal_reflow_threshold = float(horizontal_reflow_threshold)
        self.max_tracks = int(max_tracks)
        limits = (self.max_horizontal_drift, self.max_vertical_drift,
                  self.row_reflow_threshold, self.horizontal_reflow_threshold)
        if any(not math.isfinite(value) or value <= 0 for value in limits) or self.max_tracks < 1:
            raise ValueError("Anchor tracking thresholds and capacity must be positive")
        self._slots = []
        self._serial = 0
        self._step = 0
        self._has_context = False
        self._context = None
        self.last_reset_reason = None

    def reset(self, *, reason="manual"):
        self._slots.clear()
        self._has_context = False
        self._context = None
        # Never recycle tokens across resets: even an unchanged identity needs
        # fresh semantic verification after a calibration/layout change.
        self.last_reset_reason = reason

    def _cost(self, current, reference, *, relaxed_row=False):
        cx, cy = _center(current)
        rx, ry = _center(reference)
        horizontal = (abs(current[0] - reference[0]), abs(cx - rx), abs(current[2] - reference[2]))
        aligned = min(horizontal)
        vertical = abs(cy - ry)
        row_limit = .20 if relaxed_row else self.max_vertical_drift
        horizontal_limit = .10 if relaxed_row else self.max_horizontal_drift
        if aligned > horizontal_limit or vertical > row_limit:
            return 1_000_000.
        return min(.95, .60 * aligned / horizontal_limit + .30 * vertical / row_limit
                   + .10 * min(1., statistics.mean(horizontal) / .15))

    def _assign(self, boxes, *, relaxed_row=False):
        costs = [[self._cost(box, slot.reference_box, relaxed_row=relaxed_row) for slot in self._slots]
                 + [2.] * len(boxes) for box in boxes]
        return _hungarian(costs)

    def _coherent_reflow(self, boxes):
        # Require at least three and broad coverage. A partial detection of one
        # or two cards must never reset otherwise stable anchors elsewhere.
        required = max(3, math.ceil(.75 * len(self._slots)))
        if len(boxes) < required or len(self._slots) < 3:
            return False
        assignment = self._assign(boxes, relaxed_row=True)
        differences = []
        for box, column in zip(boxes, assignment):
            if column >= len(self._slots):
                continue
            reference = self._slots[column].reference_box
            if self._cost(box, reference, relaxed_row=True) >= 1_000_000:
                continue
            differences.append((box[0] - reference[0], _center(box)[0] - _center(reference)[0],
                                box[2] - reference[2], _center(box)[1] - _center(reference)[1]))
        if len(differences) < required:
            return False
        horizontal_left = [row[0] for row in differences]
        horizontal_center = [row[1] for row in differences]
        horizontal_right = [row[2] for row in differences]
        vertical = [row[3] for row in differences]
        dy = statistics.median(vertical)
        row_moved = (abs(dy) >= self.row_reflow_threshold
                     and sum(abs(value - dy) <= .006 for value in vertical) >= required)
        dx, dc, dr = (statistics.median(values) for values in
                      (horizontal_left, horizontal_center, horizontal_right))
        columns_moved = (abs(dx) >= self.horizontal_reflow_threshold
                         and abs(dc) >= self.horizontal_reflow_threshold
                         and abs(dr) >= self.horizontal_reflow_threshold and dx * dc > 0 and dc * dr > 0
                         and max(dx, dc, dr) - min(dx, dc, dr) <= .012
                         and sum(abs(value - dx) <= .008 for value in horizontal_left) >= required
                         and sum(abs(value - dc) <= .008 for value in horizontal_center) >= required
                         and sum(abs(value - dr) <= .008 for value in horizontal_right) >= required)
        return row_moved or columns_moved

    def update(self, candidates, *, context_key=None):
        """Copy valid current rows and attach stable normalized anchors and tokens.

        Empty input produces empty output while retaining spatial references.
        Invalid boxes are dropped. No identity, marker, or reminder is replayed
        from a previous frame, and input dictionaries are never modified.
        """
        if not self._has_context or context_key != self._context:
            if self._has_context:
                self.reset(reason="context_changed")
            self._context = copy.deepcopy(context_key)
            self._has_context = True
        current = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            box = _box(row.get("box_norm"))
            if box is not None:
                current.append((dict(row), box))
        self._step += 1
        boxes = [box for _, box in current]
        if self._coherent_reflow(boxes):
            self._slots.clear()
            self.last_reset_reason = "layout_reflow"
        assignment = self._assign(boxes)
        previous_slots = list(self._slots)
        output, active_ids = [], set()
        for (row, box), column in zip(current, assignment):
            if column < len(previous_slots) and self._cost(box, previous_slots[column].reference_box) < 1_000_000:
                slot = previous_slots[column]
            else:
                self._serial += 1
                slot = _Slot(f"slot-{self._serial}", box, (box[0], box[1]), None, self._step)
                self._slots.append(slot)
            slot.last_seen = self._step
            slot.identity = copy.deepcopy(row.get("identity", (row.get("kind"), row.get("id"))))
            row["anchor_norm"] = list(slot.anchor)
            row["slot_anchor_id"] = slot.slot_id
            active_ids.add(slot.slot_id)
            output.append(row)
        if len(self._slots) > self.max_tracks:
            inactive = sorted((slot for slot in self._slots if slot.slot_id not in active_ids), key=lambda slot: slot.last_seen)
            remove = {slot.slot_id for slot in inactive[:max(0, len(self._slots) - self.max_tracks)]}
            self._slots = [slot for slot in self._slots if slot.slot_id not in remove]
        return output

