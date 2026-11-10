"""Experimental border-defined shop slots layered over the 0.4.5 OCR engine.

Borders supply spatial identity; OCR supplies names. A changed slot never inherits
its previous name merely because OCR missed it. This module does not control the
game, load a browser, or infer card kinds from column order.
"""
from __future__ import annotations

import copy
import time

import numpy as np

from image_signature import build_name_signature, compare_name_signatures
from shop_geometry import detect_shop_grid, validate_shop_grid
from vision import VisionEngine, _rect_pixels


class BorderVisionEngine(VisionEngine):
    """VisionEngine-compatible border-slot experiment (no name-derived columns)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._border_layout = None
        self._border_revision = 0
        self._border_mode = False
        self._border_geometry_retry = None

    def reset_layout(self):
        # Base close() already owns its non-reentrant lock. Never acquire it here.
        super().reset_layout()
        self._border_layout = None
        self._border_mode = False
        self._border_geometry_retry = None

    def _forget_borders(self):
        if self._border_mode or self._border_layout is not None:
            super().reset_layout()
        self._border_layout = None
        self._border_mode = False

    @staticmethod
    def _contains(outer, inner, tolerance=0.0):
        return (outer[0] - tolerance <= inner[0] < inner[2] <= outer[2] + tolerance
                and outer[1] - tolerance <= inner[1] < inner[3] <= outer[3] + tolerance)

    @staticmethod
    def _name_region(slot, selected=None):
        left, top, right, bottom = slot["box_norm"]
        height = bottom - top
        search_top = top + .48 * height
        search_bottom = top + .99 * height
        if selected:
            title = selected["box_norm"]
            pad = max(.06 * height, .4 * (title[3] - title[1]))
            search_top = max(top, min(search_top, title[1] - pad))
            search_bottom = min(bottom, max(search_bottom, title[3] + pad))
        return [left, search_top, right, search_bottom]

    def _choose_title(self, raw_lines, slot, frame_size):
        """At most one unambiguous title; small description matches cannot win."""
        width, height = frame_size
        card = slot["box_norm"]
        rows = [row for row in raw_lines if self._contains(card, row["box_norm"])]
        minimum = max(8.0, (card[3] - card[1]) * height * .035)
        # Upper artwork/text is irrelevant unless it is itself a known title.
        lower = [row for row in rows if row["box_norm"][1] >= card[1] + .35 * (card[3] - card[1])]
        largest = max((row["box_px"][3] - row["box_px"][1] for row in lower), default=0)
        matched = [row for row in self._match_lines(rows)
                   if row["box_px"][3] - row["box_px"][1] >= max(minimum, largest * .82)]
        by_name = {}
        for row in matched:
            rank = (row["box_px"][3] - row["box_px"][1], row["score"])
            old = by_name.get(row["text"])
            if old is None or rank > (old["box_px"][3] - old["box_px"][1], old["score"]):
                by_name[row["text"]] = row
        ordered = sorted(by_name.values(), key=lambda row: row["box_px"][3] - row["box_px"][1], reverse=True)
        if not ordered:
            return None
        if len(ordered) > 1:
            first, second = (row["box_px"][3] - row["box_px"][1] for row in ordered[:2])
            if first < second * 1.25:
                return None
        return copy.deepcopy(ordered[0])

    @staticmethod
    def _add_stats(diagnostics, stages):
        for key in ("det_ms", "rec_ms", "recognized_lines", "detected_lines", "filtered_small_lines"):
            diagnostics[key] += stages.get(key, 0)

    def _read_regions(self, image, regions, frame_size):
        """One detector call for any number of regions, preserving client boxes.

        A line must lie entirely within one tile. Seam, gap, and cross-tile lines
        are discarded before either raw output or semantic matching is exposed.
        """
        width, height = frame_size
        rectangles = [_rect_pixels(region, width, height) for region in regions]
        if len(rectangles) == 1:
            x0, y0, x1, y1 = rectangles[0]
            lines, stats = self._ocr_lines(np.ascontiguousarray(image[y0:y1, x0:x1]),
                                          offset_px=(x0, y0), frame_size=frame_size)
            return [[row for row in lines if self._contains(regions[0], row["box_norm"])]], stats
        padding, gap = 12, 40
        mosaic_width = max(rect[2] - rect[0] for rect in rectangles) + padding * 2
        mosaic_height = sum(rect[3] - rect[1] for rect in rectangles) + gap * (len(rectangles) - 1) + padding * 2
        mosaic = np.full((mosaic_height, mosaic_width, 3), 245, dtype=np.uint8)
        tiles = []
        cursor = padding
        for x0, y0, x1, y1 in rectangles:
            tile = [padding, cursor, padding + x1 - x0, cursor + y1 - y0]
            mosaic[tile[1]:tile[3], tile[0]:tile[2]] = image[y0:y1, x0:x1]
            tiles.append(tile)
            cursor = tile[3] + gap
        lines, stats = self._ocr_lines(mosaic, offset_px=(0, 0), frame_size=(mosaic_width, mosaic_height))
        grouped = [[] for _ in rectangles]
        for row in lines:
            box = row["box_px"]
            owners = [index for index, tile in enumerate(tiles) if self._contains(tile, box)]
            if len(owners) != 1:
                continue
            index = owners[0]
            tile, original = tiles[index], rectangles[index]
            dx, dy = original[0] - tile[0], original[1] - tile[1]
            mapped = [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy]
            translated = {**row, "box_px": mapped,
                          "box_norm": [mapped[0] / width, mapped[1] / height,
                                       mapped[2] / width, mapped[3] / height]}
            grouped[index].append(translated)
        return grouped, stats

    def _new_record(self, image, slot, lines, frame_size, *, prior_region=None):
        selected = self._choose_title(lines, slot, frame_size)
        region = self._name_region(slot, selected)
        if prior_region is not None:
            region[1] = min(region[1], prior_region[1])
            region[3] = max(region[3], prior_region[3])
        x0, y0, x1, y1 = _rect_pixels(region, *frame_size)
        self._border_revision += 1
        return {"slot": copy.deepcopy(slot), "region": region,
                "selected": selected, "raw_lines": copy.deepcopy(lines),
                "fingerprint": self._fingerprint(image, [region]),
                "signature": build_name_signature(np.ascontiguousarray(image[y0:y1, x0:x1])),
                # Empty columns need artwork changes to trigger a search too: a
                # new high title might appear above the previously empty band.
                "empty_guard": self._fingerprint(image, [slot["box_norm"]]) if selected is None else None,
                "revision": self._border_revision}

    def _fallback(self, image, roi, *, sequence, timestamp, name_regions_norm, reason, started, geometry_ms):
        self._forget_borders()
        result = super().analyze(image, roi, sequence=sequence, timestamp=timestamp,
                                 name_regions_norm=name_regions_norm)
        result["diagnostics"].update(geometry_source="fallback", geometry_reason=reason,
                                     geometry_ms=geometry_ms)
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result

    def analyze(self, image_bgr, roi_norm, *, sequence=0, timestamp=None, name_regions_norm=None):
        started = time.perf_counter()
        captured_at = time.monotonic() if timestamp is None else float(timestamp)
        geometry_ms = 0.0
        result = {"status": "no_candidates", "sequence": int(sequence), "timestamp": captured_at,
                  "observed_at": time.time(), "frame_size": [], "roi_norm": list(roi_norm),
                  "candidates": [], "raw_lines": [], "elapsed_ms": 0.0,
                  "layout_source": "border_initial_detection", "stability_regions_norm": [],
                  "ocr_reused": False, "error": None,
                  "diagnostics": {"path": "border_initial_detection", "geometry_source": "borders",
                                  "det_ms": 0.0, "rec_ms": 0.0, "recognized_lines": 0,
                                  "detected_lines": 0, "filtered_small_lines": 0,
                                  "full_refresh_reason": None, "geometry_ms": 0.0,
                                  "slot_count": 0, "changed_slots": 0, "reused_slots": 0}}
        try:
            if not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8 or image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
                raise ValueError("识别输入必须为 BGR uint8 三通道图像。")
            height, width = image_bgr.shape[:2]
            frame_size = (width, height)
            result["frame_size"] = list(frame_size)
            rect = _rect_pixels(roi_norm, width, height)
            if min(rect[2] - rect[0], rect[3] - rect[1]) < 8:
                raise ValueError("识别区域过小，请重新框选商店。")
            if timestamp is not None and (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                self.reset_layout()
                result.update(status="frame_stale", error="画面已过期，本次不生成提醒。")
                return result
            if not self.smart or name_regions_norm:
                return self._fallback(image_bgr, roi_norm, sequence=sequence, timestamp=timestamp,
                                      name_regions_norm=name_regions_norm, reason="explicit_or_smart_disabled",
                                      started=started, geometry_ms=0.0)
            if float(np.std(image_bgr[rect[1]:rect[3], rect[0]:rect[2]])) < 1:
                self.reset_layout()
                result.update(status="blank_frame", error="识别区域没有可读画面。")
                return result
            context = (width, height, *(float(v) for v in roi_norm))
            with self._lock:
                if self._closed:
                    raise RuntimeError("识别引擎已关闭。")
                epoch = self._layout_epoch
                previous = self._border_layout
                geometry_started = time.perf_counter()
                # A context change cannot borrow semantics, even if the detector
                # returns the same layout identifier after a resize/calibration.
                compatible = previous is not None and previous["context"] == context
                geometry_error = None
                retry = self._border_geometry_retry
                backoff = (retry is not None and retry["context"] == context
                           and time.monotonic() < retry["retry_at"])
                if backoff:
                    valid, grid = False, None
                else:
                    try:
                        valid = compatible and validate_shop_grid(image_bgr, previous["grid"], roi_norm)
                        # We already rejected the previous grid above. Passing it to
                        # detect would repeat the same cheap validator on this frame.
                        grid = previous["grid"] if valid else detect_shop_grid(image_bgr, roi_norm, previous=None)
                    except Exception as exc:
                        valid, grid = False, None
                        geometry_error = str(exc)
                geometry_ms = (time.perf_counter() - geometry_started) * 1000
                if not grid or not grid.get("slots") or not grid.get("layout_key"):
                    fallback_reason = ("border_retry_backoff" if backoff else
                                       "border_geometry_error" if geometry_error else "no_reliable_border_grid")
                else:
                    fallback_reason = None
                    # Refuse malformed detector output instead of indexing an
                    # incomplete/overlapping spatial cache.
                    boxes = [slot["box_norm"] for slot in grid["slots"]]
                    if (len(boxes) < 3 or len({slot["id"] for slot in grid["slots"]}) != len(boxes)
                            or any(not self._contains(roi_norm, box) for box in boxes)):
                        fallback_reason = "invalid_border_grid"
                    for index, box in enumerate(boxes):
                        _rect_pixels(box, width, height)
                        if any(min(box[2], other[2]) > max(box[0], other[0])
                               and min(box[3], other[3]) > max(box[1], other[1]) for other in boxes[:index]):
                            fallback_reason = "overlapping_border_slots"
                if fallback_reason is not None and not backoff:
                    self._border_geometry_retry = {"context": context, "retry_at": time.monotonic() + .6}
                if fallback_reason is None:
                    self._border_geometry_retry = None
                    now = time.monotonic()
                    diagnostics = result["diagnostics"]
                    diagnostics.update(geometry_ms=geometry_ms, slot_count=len(grid["slots"]),
                                       layout_key=grid["layout_key"])
                    cold = (not valid or not compatible or previous["grid"]["layout_key"] != grid["layout_key"])
                    records = {} if cold else copy.copy(previous["records"])
                    dirty, reused = [], []
                    current_fingerprints = {}
                    for slot in grid["slots"]:
                        old = records.get(slot["id"])
                        if old is None:
                            dirty.append(slot)
                            continue
                        fingerprint = self._fingerprint(image_bgr, [old["region"]])
                        current_fingerprints[slot["id"]] = fingerprint
                        empty_changed = old["empty_guard"] is not None and old["empty_guard"] != self._fingerprint(image_bgr, [slot["box_norm"]])
                        same = not empty_changed and fingerprint == old["fingerprint"]
                        if not same and not empty_changed:
                            x0, y0, x1, y1 = _rect_pixels(old["region"], width, height)
                            signature = build_name_signature(np.ascontiguousarray(image_bgr[y0:y1, x0:x1]))
                            same = compare_name_signatures(old["signature"], signature, strict=True) is True
                        (reused if same else dirty).append(slot)
                    periodic_due = not cold and now - previous["verified_at"] >= self.full_refresh_seconds
                    idle = not cold and now - previous["changed_at"] >= 1.0
                    do_full = cold or (periodic_due and not dirty and idle)
                    diagnostics.update(changed_slots=len(dirty), reused_slots=len(reused), periodic_pending=periodic_due)
                    if do_full:
                        x0, y0, x1, y1 = rect
                        lines, stages = self._ocr_lines(np.ascontiguousarray(image_bgr[y0:y1, x0:x1]),
                                                       offset_px=(x0, y0), frame_size=frame_size)
                        self._add_stats(diagnostics, stages)
                        for slot in grid["slots"]:
                            owned = [row for row in lines if self._contains(slot["box_norm"], row["box_norm"])]
                            records[slot["id"]] = self._new_record(image_bgr, slot, owned, frame_size)
                        diagnostics["path"] = "border_initial_detection" if cold else "border_periodic_detection"
                        verified_at, changed_at = now, now
                    elif dirty:
                        regions = [records[slot["id"]]["region"] if slot["id"] in records else self._name_region(slot) for slot in dirty]
                        groups, stages = self._read_regions(image_bgr, regions, frame_size)
                        self._add_stats(diagnostics, stages)
                        # A changed slot with a miss is unknown immediately. A
                        # wider search may find an unusually high new title; it
                        # never restores an old identity from memory.
                        missing = [index for index, (slot, rows) in enumerate(zip(dirty, groups))
                                   if self._choose_title(rows, slot, frame_size) is None]
                        if missing:
                            broad, stages = self._read_regions(image_bgr, [dirty[index]["box_norm"] for index in missing], frame_size)
                            self._add_stats(diagnostics, stages)
                            for index, rows in zip(missing, broad):
                                groups[index] = rows
                        for slot, rows, region in zip(dirty, groups, regions):
                            records[slot["id"]] = self._new_record(image_bgr, slot, rows, frame_size, prior_region=region)
                        diagnostics["path"] = "border_slot_detection" if len(dirty) == 1 else "border_batch_detection"
                        verified_at, changed_at = previous["verified_at"], now
                    else:
                        diagnostics["path"] = "unchanged_border_slots"
                        result["ocr_reused"] = True
                        verified_at, changed_at = previous["verified_at"], previous["changed_at"]
                    # Preserve original structural signatures after tolerated
                    # hover changes: repeated lighting steps cannot drift into a
                    # new character. Only the exact pixel shortcut is refreshed.
                    for slot in reused if not do_full else []:
                        records[slot["id"]] = {**records[slot["id"]], "fingerprint": current_fingerprints[slot["id"]]}
                    for slot in grid["slots"]:
                        record = records[slot["id"]]
                        result["raw_lines"].extend(copy.deepcopy(record["raw_lines"]))
                        result["stability_regions_norm"].append(list(record["region"]))
                        if record["selected"] is not None:
                            left, top, right, bottom = slot["box_norm"]
                            result["candidates"].append({**copy.deepcopy(record["selected"]),
                                "border_slot_id": slot["id"], "slot_roi_norm": list(slot["box_norm"]),
                                "border_anchor_norm": [left + .04 * (right - left), top + .60 * (bottom - top)],
                                "border_layout_key": grid["layout_key"], "name_roi_norm": list(record["region"]),
                                "slot_revision": record["revision"]})
                    result["layout_source"] = diagnostics["path"]
                    if epoch != self._layout_epoch:
                        result.update(status="frame_stale", candidates=[], raw_lines=[], error="识别区域已改变，本次不生成提醒。")
                    elif timestamp is not None and (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                        self.reset_layout()
                        result.update(status="frame_stale", candidates=[], raw_lines=[], error="识别完成时画面已过期，本次不生成提醒。")
                    else:
                        self._layout = self._last_full = None
                        self._border_layout = {"context": context, "grid": copy.deepcopy(grid), "records": records,
                                               "verified_at": verified_at, "changed_at": changed_at}
                        self._border_mode = True
                        result["status"] = "ready" if result["candidates"] else "no_candidates"
            # Never invoke the base analyzer while owning its non-reentrant lock.
            if fallback_reason is not None:
                return self._fallback(image_bgr, roi_norm, sequence=sequence, timestamp=timestamp,
                                      name_regions_norm=name_regions_norm, reason=fallback_reason,
                                      started=started, geometry_ms=geometry_ms)
        except Exception as exc:
            self.reset_layout()
            result.update(status="error", error=str(exc), candidates=[], raw_lines=[])
        finally:
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result
