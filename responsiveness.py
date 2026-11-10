"""Cheap, conservative frame stability and non-destructive upgrade helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics

import numpy as np
from image_signature import same_name_structure


def name_frames_stable(first, second, regions):
    """Return True/False/None: stable, changed, or no independent next frame.

    Compare local name structure across the full-width bands. Hover tint and
    thin borders can vary without changing text; uncertainty requests fresh OCR.
    All regions are normalized CLIENT coordinates; desktop movement is harmless.
    """
    if second.sequence <= first.sequence or second.timestamp <= first.timestamp:
        return None
    if first.image_bgr.shape != second.image_bgr.shape:
        return False
    if not regions:
        return None
    before, after = first.image_bgr, second.image_bgr
    height, width = before.shape[:2]
    for region in regions:
        if len(region) != 4 or not all(np.isfinite(region)):
            return False
        left, top, right, bottom = map(float, region)
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            return False
        x0, x1 = int(left*width), int(np.ceil(right*width))
        y0, y1 = int(top*height), int(np.ceil(bottom*height))
        a = before[y0:y1, x0:x1]
        b = after[y0:y1, x0:x1]
        if not a.size or not b.size:
            return False
        if np.array_equal(a, b):
            continue
        if same_name_structure(a, b) is not True:
            return False
    return True


def confirm_independent_names(previous, current):
    """Annotate the newest OCR names independently repeated in the same slot.

    A purchase or hover can change one part of a band. Independent current-frame
    observations can include names verified by the upstream pixel cache.
    Association is one-to-one, so duplicate names in different slots stay apart.
    """
    rows = [dict(row) for row in current.get("candidates", [])]
    if (current.get("sequence", -1) <= previous.get("sequence", -1)
            or current.get("timestamp", 0) <= previous.get("timestamp", 0)
            or current.get("frame_size") != previous.get("frame_size")):
        return rows
    width, height = current.get("frame_size", [1, 1])
    used = set()
    for row in rows:
        box = row["box_norm"]
        options = []
        for index, old in enumerate(previous.get("candidates", [])):
            if index in used or row["text"] != old["text"]:
                continue
            border_slot, old_border_slot = row.get("border_slot_id"), old.get("border_slot_id")
            if border_slot is not None or old_border_slot is not None:
                if (border_slot is not None and border_slot == old_border_slot
                        and row.get("border_layout_key") is not None
                        and row.get("border_layout_key") == old.get("border_layout_key")):
                    options.append((0.0, index, old))
                continue
            old_box = old["box_norm"]
            line_height = min(box[3]-box[1], old_box[3]-old_box[1])
            tolerance_x = min(.012, max(.003, line_height*height/max(1, width)*.4))
            tolerance_y = min(.015, max(.003, line_height*.4))
            dx = abs(box[0]-old_box[0])
            dy = abs((box[1]+box[3]-old_box[1]-old_box[3])/2)
            if dx <= tolerance_x and dy <= tolerance_y:
                options.append((dx+dy, index, old))
        if options:
            _, index, old = min(options, key=lambda item: item[0])
            used.add(index)
            row["independently_confirmed"] = min(row["score"], old["score"]) >= .95
    return rows


def _candidate_verification_regions(observation):
    """Use card columns for width and each actual title for vertical bounds.

    Operator and equipment titles can sit at different heights. A shared OCR
    band is not evidence that their card slots share the same text baseline.
    """
    candidates = observation.get("candidates", [])
    roi = observation.get("roi_norm", [0, 0, 1, 1])
    width, height = observation.get("frame_size", [1200, 720])
    width, height = max(1, width), max(1, height)
    padding = 3/width
    boxes = [candidate["box_norm"] for candidate in candidates]
    if not boxes:
        return
    typical_height_px = statistics.median((box[3]-box[1])*height for box in boxes)
    same_column_tolerance = max(padding*2, typical_height_px*.5/width)
    lefts = sorted(box[0] for box in boxes)
    gaps = [right-left for left, right in zip(lefts, lefts[1:])
            if right-left > same_column_tolerance]
    pitch = min(gaps) if gaps else None
    for candidate in candidates:
        box = candidate["box_norm"]
        if candidate.get("border_slot_id") is not None:
            region = candidate.get("name_roi_norm")
            slot = candidate.get("slot_roi_norm")
            try:
                valid_region = (region is not None and slot is not None
                                and len(region) == len(slot) == 4
                                and all(np.isfinite(region)) and all(np.isfinite(slot))
                                and roi[0] <= slot[0] <= region[0] < region[2] <= slot[2] <= roi[2]
                                and roi[1] <= slot[1] <= region[1] < region[3] <= slot[3] <= roi[3]
                                and region[0] <= box[0] + 1/width
                                and region[2] >= box[2] - 1/width
                                and region[1] <= box[1] + 1/height
                                and region[3] >= box[3] - 1/height)
            except (TypeError, ValueError):
                valid_region = False
            if valid_region:
                yield candidate, list(map(float, region))
            # A border candidate must not silently fall back to name-derived
            # columns when its own geometry/mosaic mapping is invalid.
            continue
        peers = [left for left in lefts if left > box[0]+same_column_tolerance]
        right = min(peers)-padding if peers else (box[0]+pitch-padding if pitch else roi[2])
        left = max(roi[0], box[0]-padding)
        right = min(roi[2], max(box[2]+padding, right))
        padding_y = max(2/height, min(6/height, (box[3]-box[1])*.12))
        top, bottom = max(roi[1], box[1]-padding_y), min(roi[3], box[3]+padding_y)
        if left < right and top < bottom:
            yield candidate, [left, top, right, bottom]


def name_verification_regions(observation):
    """Cover title text and possible long suffixes without unrelated card rows."""
    return [region for _, region in _candidate_verification_regions(observation)]


def filter_current_candidates(observation, source_frame, latest_frame):
    """Drop changed slots without dropping titles missing from a learned band."""
    output = []
    for candidate, region in _candidate_verification_regions(observation):
        stable = name_frames_stable(source_frame, latest_frame, [region])
        if stable is not False:
            output.append({**candidate, "frame_verified": stable is True})
    return output


def migrate_legacy_data(destination, legacy):
    """Copy valid JSON only into absent new-version files; never touch old data."""
    destination, legacy = Path(destination), Path(legacy)
    report = {"copied": [], "warnings": []}
    if destination.resolve() == legacy.resolve() or not legacy.is_dir():
        return report
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("planner.json", "calibration.json", "settings.json"):
        source, target = legacy/name, destination/name
        if not source.is_file() or target.exists():
            continue
        created = False
        try:
            raw = source.read_bytes()
            payload = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            report["copied"].append(name)
        except FileExistsError:
            pass
        except Exception as error:
            if created:
                target.unlink(missing_ok=True)
            report["warnings"].append(f"{name}: {error}")
    return report
