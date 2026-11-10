"""Screen-pixel card frames, independent of OCR, names, levels and game state.

The detector deliberately needs three matching outer rectangles. Missing or
ambiguous evidence returns None so the caller can use its original OCR path.
All public rectangles use normalized client-image coordinates, right exclusive.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

import cv2
import numpy as np


def _bounds(image, roi):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        return None
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 16:
        return None
    try:
        values = [float(value) for value in roi]
        if len(values) != 4 or not all(math.isfinite(value) for value in values):
            return None
        left, top, right, bottom = values
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            return None
        height, width = image.shape[:2]
        box = [int(math.floor(left * width)), int(math.floor(top * height)),
               int(math.ceil(right * width)), int(math.ceil(bottom * height))]
        if box[2] - box[0] < 60 or box[3] - box[1] < 40:
            return None
        return box
    except (TypeError, ValueError, OverflowError):
        return None


def _line_support(image, start, end, *, tolerance=2, samples=None, gap_tolerance=0):
    """Fraction of sampled positions with a nearby cross-line color edge."""
    height, width = image.shape[:2]
    x0, y0 = start
    x1, y1 = end
    length = math.hypot(x1 - x0, y1 - y0)
    samples = min(768, max(24, int(length) + 1)) if samples is None else samples
    if abs(y1 - y0) >= abs(x1 - x0):
        yy = np.rint(np.linspace(y0, y1, samples)).astype(np.intp)
        xx = np.rint(np.linspace(x0, x1, samples)).astype(np.intp)
        offsets = np.arange(-tolerance - 1, tolerance + 2)
        xx = np.clip(xx[:, None] + offsets[None, :], 0, width - 1)
        yy = np.clip(yy, 0, height - 1)[:, None]
    else:
        xx = np.rint(np.linspace(x0, x1, samples)).astype(np.intp)
        yy = np.rint(np.linspace(y0, y1, samples)).astype(np.intp)
        offsets = np.arange(-tolerance - 1, tolerance + 2)
        yy = np.clip(yy[:, None] + offsets[None, :], 0, height - 1)
        xx = np.clip(xx, 0, width - 1)[:, None]
    pixels = image[yy, xx].astype(np.int16)
    contrast = np.max(np.abs(np.diff(pixels, axis=1)), axis=(1, 2))
    supported = contrast >= 12
    fraction = float(np.mean(supported))
    if gap_tolerance and fraction >= .25:
        # A purchased/empty website card has a dashed outline. Join only tiny
        # physical gaps along this same straight edge, never missing card sides.
        gap_samples = max(1, int(math.ceil(gap_tolerance * (samples - 1) / max(1., length))))
        transitions = np.diff(np.r_[True, supported, True].astype(np.int8))
        starts, ends = np.flatnonzero(transitions == -1), np.flatnonzero(transitions == 1)
        joined = supported.copy()
        for first, last in zip(starts, ends):
            if last - first <= gap_samples:
                joined[first:last] = True
        if np.mean(joined) >= .85:
            return float(np.mean(joined))
    return fraction


def _frame_support(image, box):
    left, top, right, bottom = [float(value) for value in box]
    width, height = right - left, bottom - top
    inset = max(3., min(width, height) * .07)
    tolerance = max(2, min(5, round(min(width, height) * .012)))
    dash_gap = max(3, round(min(width, height) * .025))
    sides = [
        _line_support(image, (left, top + inset), (left, bottom - 1 - inset), tolerance=tolerance, gap_tolerance=dash_gap),
        _line_support(image, (right - 1, top + inset), (right - 1, bottom - 1 - inset), tolerance=tolerance, gap_tolerance=dash_gap),
        _line_support(image, (left + inset, top), (right - 1 - inset, top), tolerance=tolerance, gap_tolerance=dash_gap),
        _line_support(image, (left + inset, bottom - 1), (right - 1 - inset, bottom - 1), tolerance=tolerance, gap_tolerance=dash_gap),
    ]
    good = min(sides[:2]) >= .50 and min(sides[2:]) >= .34 and sum(sides[2:]) >= 1.05
    return bool(good), float(np.mean(sides)), sides


def _iou(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    areas = [(box[2] - box[0]) * (box[3] - box[1]) for box in (first, second)]
    return intersection / max(1., sum(areas) - intersection)


def _candidates(image, roi_box):
    left, top, right, bottom = roi_box
    crop = image[top:bottom, left:right]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 18, 54)
    closing = max(3, round(crop.shape[0] * .018)) | 1
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((closing, closing), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    roi_width, roi_height = right - left, bottom - top
    proposed = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        proposed.append([x + left, y + top, x + width + left, y + height + top])

    # A border can be broken by artwork or low contrast. Paired long vertical
    # segments recover proposals, which must still pass all four side checks.
    segments = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=max(18, int(roi_height * .15)),
                               minLineLength=max(28, int(roi_height * .48)),
                               maxLineGap=max(4, int(roi_height * .035)))
    vertical = []
    if segments is not None:
        for x0, y0, x1, y1 in np.asarray(segments).reshape(-1, 4):
            if abs(x1 - x0) <= 3 and abs(y1 - y0) >= roi_height * .48:
                vertical.append((float(x0 + x1) / 2 + left, min(y0, y1) + top, max(y0, y1) + top + 1))
    for index, first in enumerate(vertical):
        for second in vertical[index + 1:]:
            x0, x1 = sorted((first[0], second[0]))
            y0, y1 = max(first[1], second[1]), min(first[2], second[2])
            if x1 - x0 >= max(24, roi_width * .035) and y1 - y0 >= roi_height * .55:
                proposed.append([x0, y0, x1 + 1, y1])

    accepted = []
    for box in proposed:
        width, height = box[2] - box[0], box[3] - box[1]
        if not (max(24, roi_width * .035) <= width <= roi_width * .36
                and max(40, roi_height * .55) <= height <= roi_height + 2
                and .32 <= width / height <= 1.0):
            continue
        good, quality, _ = _frame_support(image, box)
        if good:
            accepted.append({"box": box, "quality": quality})
    # Remove duplicate strokes of the SAME frame, not every overlapping box.
    # A Hough proposal can span a neighboring gap; retaining alternatives lets
    # the repeated-size group reject that proposal instead of hiding real cards.
    accepted.sort(key=lambda row: (row["quality"], (row["box"][2] - row["box"][0])
                                   * (row["box"][3] - row["box"][1])), reverse=True)
    unique = []
    for row in accepted:
        box = row["box"]
        if not any(_iou(box, previous["box"]) > .75
                   and abs((box[2] - box[0]) / (previous["box"][2] - previous["box"][0]) - 1) < .06
                   and abs((box[3] - box[1]) / (previous["box"][3] - previous["box"][1]) - 1) < .06
                   for previous in unique):
            unique.append(row)
    return unique


def _best_group(candidates):
    options = []
    for reference in candidates:
        ref = reference["box"]
        width, height = ref[2] - ref[0], ref[3] - ref[1]
        rows = [row for row in candidates
                if abs((row["box"][2] - row["box"][0]) / width - 1) <= .085
                and abs((row["box"][3] - row["box"][1]) / height - 1) <= .085
                and abs(row["box"][1] - ref[1]) <= height * .075]
        rows.sort(key=lambda row: row["box"][0])
        # Slightly wider equipment separation is valid. Missing entire borders
        # do not justify inventing unobserved columns on a cold start.
        chains, chain = [], []
        for row in rows:
            if chain:
                gap = row["box"][0] - chain[-1]["box"][2]
                if not (-width * .035 <= gap <= width * .38):
                    chains.append(chain)
                    chain = []
            chain.append(row)
        chains.append(chain)
        for chain in chains:
            if len(chain) >= 3:
                median_height = float(np.median([row["box"][3] - row["box"][1] for row in chain]))
                score = len(chain) * median_height * float(np.mean([row["quality"] for row in chain]))
                options.append((score, chain))
    if not options:
        return None

    def nested(inner, outer):
        if len(outer) < len(inner):
            return False
        used = set()
        for row in inner:
            box = row["box"]
            container = next((index for index, candidate in enumerate(outer)
                              if index not in used
                              and candidate["box"][0] <= box[0] + 2
                              and candidate["box"][2] >= box[2] - 2
                              and candidate["box"][1] <= box[1] + 2
                              and candidate["box"][3] >= box[3] - 2
                              and candidate["box"][3] - candidate["box"][1] >= (box[3] - box[1]) * 1.08), None)
            if container is None:
                return False
            used.add(container)
        return True

    # Strong portrait borders cannot outrank the independently verified outer
    # card frames containing them. Their lower score may just be a faint border.
    options = [(score, group) for score, group in options
               if not any(nested(group, outer) for _, outer in options)]
    if not options:
        return None
    options.sort(key=lambda item: item[0], reverse=True)
    best_score, best = options[0]
    # Distinct, similarly supported competing grids are genuinely ambiguous.
    for score, other in options[1:]:
        if score < best_score * .90:
            break
        same_family = all(any(
            abs((row["box"][2] - row["box"][0]) / (candidate["box"][2] - candidate["box"][0]) - 1) <= .05
            and abs((row["box"][3] - row["box"][1]) / (candidate["box"][3] - candidate["box"][1]) - 1) <= .05
            and _iou(row["box"], candidate["box"]) >= .85
            for candidate in best) for row in other)
        if not same_family:
            return None
    return best


def _extra_frame_present(image, boxes, roi_box):
    """Cheap new-column sentinel, including left expansion at a new level."""
    widths = [box[2] - box[0] for box in boxes]
    card_width = float(np.median(widths))
    top = float(np.median([box[1] for box in boxes]))
    bottom = float(np.median([box[3] for box in boxes]))
    intervals, cursor = [], roi_box[0]
    for box in sorted(boxes, key=lambda row: row[0]):
        if box[0] - cursor >= card_width * .90:
            intervals.append((cursor, box[0]))
        cursor = max(cursor, box[2])
    if roi_box[2] - cursor >= card_width * .90:
        intervals.append((cursor, roi_box[2]))
    inset = max(4., (bottom - top) * .08)
    for left, right in intervals:
        peaks = []
        for x in range(int(left), int(right) + 1, 2):
            strength = _line_support(image, (x, top + inset), (x, bottom - 1 - inset), samples=24,
                                     gap_tolerance=max(3, round(card_width * .025)))
            if strength >= .70:
                if peaks and x - peaks[-1][-1] <= 4:
                    peaks[-1].append(x)
                else:
                    peaks.append([x])
        centers = [float(np.median(group)) for group in peaks]
        for index, first in enumerate(centers):
            for second in centers[index + 1:]:
                if abs((second - first) / card_width - 1) <= .085:
                    good, _, _ = _frame_support(image, [first, top, second + 1, bottom])
                    if good:
                        return True
    return False


def validate_shop_grid(image_bgr, grid, roi_norm):
    """Check edge samples without contours, Hough, OCR, or image resizing.

    One locally hovered card may move/scale while at least two other original
    frames remain present. Bounds and IDs stay at the original layout anchors.
    """
    roi_box = _bounds(image_bgr, roi_norm)
    if roi_box is None or not isinstance(grid, dict):
        return False
    height, width = image_bgr.shape[:2]
    try:
        if grid["frame_size"] != [width, height] or grid["roi_norm"] != [float(value) for value in roi_norm]:
            return False
        slots = grid["slots"]
        if len(slots) < 3:
            return False
        boxes = [[row["box_norm"][0] * width, row["box_norm"][1] * height,
                  row["box_norm"][2] * width, row["box_norm"][3] * height] for row in slots]
        if any(not all(math.isfinite(value) for value in box)
               or not (roi_box[0] - 2 <= box[0] < box[2] <= roi_box[2] + 2
                       and roi_box[1] - 2 <= box[1] < box[3] <= roi_box[3] + 2) for box in boxes):
            return False
        good = sum(_frame_support(image_bgr, box)[0] for box in boxes)
        if good < max(2, len(boxes) - 1):
            return False
        return not _extra_frame_present(image_bgr, boxes, roi_box)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return False


def detect_shop_grid(image_bgr, roi_norm, previous=None):
    """Return validated external card frames, or None for the original OCR path."""
    roi_box = _bounds(image_bgr, roi_norm)
    if roi_box is None:
        return None
    if previous is not None and validate_shop_grid(image_bgr, previous, roi_norm):
        result = copy.deepcopy(previous)
        result["diagnostics"] = {**result.get("diagnostics", {}), "path": "validated_previous_frames"}
        return result
    candidates = _candidates(image_bgr, roi_box)
    group = _best_group(candidates)
    if group is None:
        return None
    height, width = image_bgr.shape[:2]
    card_width = float(np.median([row["box"][2] - row["box"][0] for row in group]))
    card_height = float(np.median([row["box"][3] - row["box"][1] for row in group]))
    top = float(np.median([row["box"][1] for row in group]))
    boxes = []
    for row in group:
        center = (row["box"][0] + row["box"][2]) / 2
        box = [max(roi_box[0], center - card_width / 2), max(roi_box[1], top),
               min(roi_box[2], center + card_width / 2), min(roi_box[3], top + card_height)]
        boxes.append([box[0] / width, box[1] / height, box[2] / width, box[3] / height])
    fingerprint = {"frame_size": [width, height], "roi": [round(float(value), 6) for value in roi_norm],
                   "boxes": [[round(value, 5) for value in box] for box in boxes]}
    layout_key = "border-" + hashlib.blake2b(json.dumps(fingerprint, sort_keys=True).encode("ascii"), digest_size=8).hexdigest()
    quality = float(np.mean([row["quality"] for row in group]))
    confidence = min(.99, .62 * quality + .32 + min(.05, (len(group) - 3) * .015))
    if confidence < .74:
        return None
    slots = [{"id": f"{layout_key}:{index}", "box_norm": box, "name_roi_norm": list(box),
              "anchor_norm": box[:2]} for index, box in enumerate(boxes)]
    return {"slots": slots, "layout_key": layout_key, "confidence": confidence,
            "frame_size": [width, height], "roi_norm": [float(value) for value in roi_norm],
            "diagnostics": {"path": "detected_frames", "candidate_count": len(candidates),
                            "slot_count": len(slots), "mean_frame_support": quality}}
