"""Theme-confirmed title OCR for official shop cards."""
from __future__ import annotations

from dataclasses import dataclass
import time

import cv2
import numpy as np

from border_vision import BorderVisionEngine
from image_signature import NameSignature, build_name_signature, compare_name_signatures
from official_shop_geometry import locate_official_shop
from vision import _rect_pixels


_TITLE_RETRY_DELAY = .22
_TITLE_HOLD_SECONDS = .65


@dataclass(frozen=True)
class _TitleProof:
    box: tuple[int, int, int, int]
    signature: NameSignature
    match: dict
    sequence: int
    timestamp: float


@dataclass(frozen=True)
class _TitleRecord:
    fingerprint: str | None = None
    raw: dict | None = None
    match: dict | None = None
    proof: _TitleProof | None = None
    sequence: int = -1
    retry_after: float | None = None
    hold_until: float = 0.


class OfficialShopEngine(BorderVisionEngine):
    def __init__(self, *args, allow_legacy=False, stabilize_titles=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_legacy = allow_legacy
        self.stabilize_titles = stabilize_titles
        self._theme_seen = False
        self._title_layout = None

    def reset_layout(self):
        super().reset_layout()
        self._theme_seen = False
        self._title_layout = None

    def _read_titles(self, crops):
        from rapidocr.ch_ppocr_rec import TextRecInput

        prediction = self.engine.text_rec(TextRecInput(img=crops))
        texts = getattr(prediction, "txts", None)
        scores = getattr(prediction, "scores", None)
        if texts is None or scores is None or len(texts) != len(crops) or len(scores) != len(crops):
            raise RuntimeError("商店名称识别返回的槽位数不一致。")
        return zip(texts, scores)

    @staticmethod
    def _proves_title(proof, crop, box, sequence, timestamp):
        if (proof is None or proof.box != box or sequence <= proof.sequence
                or timestamp <= proof.timestamp or min(crop.shape[:2]) < 4):
            return False
        signature = build_name_signature(crop)
        return compare_name_signatures(proof.signature, signature, strict=True) is True

    def _title_record(self, previous, fingerprint, row, match, crop, box, sequence, timestamp, retry):
        if not self.stabilize_titles:
            return _TitleRecord(fingerprint, row, match)
        if match is not None:
            proof = (_TitleProof(box, build_name_signature(crop), match, sequence, timestamp)
                     if min(crop.shape[:2]) >= 4 else None)
            return _TitleRecord(fingerprint, row, match, proof, sequence)

        # A miss may be retried once for the same pixels on a newer frame.
        # Only an actual successful read can establish a new proof baseline.
        retry_after = None if retry else timestamp + _TITLE_RETRY_DELAY
        proof = previous.proof if previous is not None else None
        hold_until = (timestamp + _TITLE_HOLD_SECONDS if previous and previous.match is not None
                      else previous.hold_until if previous else 0.)
        if timestamp >= hold_until or not self._proves_title(proof, crop, box, sequence, timestamp):
            proof, hold_until = None, 0.
        return _TitleRecord(fingerprint, row, None, proof, sequence, retry_after, hold_until)

    def recheck_held_titles(self, observation, frame):
        """A later independent frame must still verify the held slot and title."""
        candidates = observation.get("candidates", [])
        if not any("title_hold_until" in row for row in candidates):
            return candidates
        if frame.sequence <= observation["sequence"]:
            return candidates
        regular = [row for row in candidates if "title_hold_until" not in row]
        height, width = frame.image_bgr.shape[:2]
        if [width, height] != observation["frame_size"] or frame.timestamp <= observation["timestamp"]:
            return regular
        grid, _ = locate_official_shop(frame.image_bgr, observation["roi_norm"])
        layout = self._title_layout
        if grid is None or layout is None or grid["layout_key"] != layout[0]:
            return regular
        slots = {slot["id"]: slot for slot in grid["slots"] if not (slot.get("empty") or slot.get("covered"))}
        kept = []
        for row in candidates:
            if "title_hold_until" not in row:
                kept.append(row)
                continue
            slot_id = row["border_slot_id"]
            if (row["border_layout_key"] != layout[0] or slot_id not in slots
                    or frame.timestamp >= row["title_hold_until"]):
                continue
            record = layout[1].get(slot_id)
            if record is None:
                continue
            box = _rect_pixels(slots[slot_id]["title_box_norm"], width, height)
            left, top, right, bottom = box
            crop = frame.image_bgr[top:bottom, left:right]
            if self._proves_title(record.proof, crop, box, frame.sequence, frame.timestamp):
                kept.append(row)
        return kept

    def analyze(self, image_bgr, roi_norm, *, sequence=0, timestamp=None, name_regions_norm=None):
        started = time.perf_counter()
        captured_at = time.monotonic() if timestamp is None else float(timestamp)
        height, width = image_bgr.shape[:2]
        frame_size = [width, height]
        result = {"status": "scene_changed", "sequence": int(sequence), "timestamp": captured_at,
                  "observed_at": time.time(), "frame_size": frame_size, "roi_norm": list(roi_norm),
                  "candidates": [], "raw_lines": [], "elapsed_ms": 0.0, "ocr_reused": False,
                  "stability_regions_norm": [], "layout_source": "official_shop",
                  "error": "等待识别展开的官方商店。", "diagnostics": {"path": "official_shop_waiting"}}
        try:
            epoch = self._layout_epoch
            if (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                result.update(status="frame_stale", error="窗口画面已过期，等待新画面。")
                return result
            grid, recognized_hud = locate_official_shop(image_bgr, roi_norm)
            geometry_ms = (time.perf_counter() - started) * 1000
            if grid is None:
                self._title_layout = None
                self._theme_seen |= recognized_hud
                if self.allow_legacy and not self._theme_seen:
                    fallback = super().analyze(image_bgr, roi_norm, sequence=sequence, timestamp=timestamp,
                                               name_regions_norm=name_regions_norm)
                    fallback["diagnostics"]["official_theme_fallback"] = True
                    fallback["elapsed_ms"] += geometry_ms
                    return fallback
                return result
            self._theme_seen = True
            theme_key = grid["theme_key"]
            result["layout_source"] = f"official_{theme_key}"
            layout_key = grid["layout_key"]
            slots = grid["slots"]
            diagnostics = result["diagnostics"]
            diagnostics.update(geometry_source="borders", theme=grid["theme"], geometry_ms=geometry_ms,
                               slot_count=len(slots), layout_key=layout_key, det_ms=0.0, rec_ms=0.0)
            with self._lock:
                if self._closed:
                    raise RuntimeError("识别引擎已关闭。")
                old = self._title_layout
                records = dict(old[1]) if old is not None and old[0] == layout_key else {}
                dirty, crops = [], []
                emptied = 0
                for slot in slots:
                    record = records.get(slot["id"])
                    if slot.get("empty") or slot.get("covered"):
                        if record is None or record.fingerprint is not None:
                            emptied += 1
                        records[slot["id"]] = _TitleRecord()
                        continue
                    region = slot["title_box_norm"]
                    fingerprint = self._fingerprint(image_bgr, [region])
                    same = record is not None and record.fingerprint == fingerprint
                    retry = (same and record.retry_after is not None and sequence > record.sequence
                             and captured_at >= record.retry_after)
                    if same and not retry:
                        continue
                    box = _rect_pixels(region, width, height)
                    left, top, right, bottom = box
                    crop = np.ascontiguousarray(image_bgr[top:bottom, left:right])
                    # Padding gives a steady failed crop one different OCR input.
                    crops.append(cv2.copyMakeBorder(crop, 3, 3, 5, 5, cv2.BORDER_REPLICATE) if retry else crop)
                    dirty.append((slot, fingerprint, box, crop, retry))
                if dirty:
                    rec_started = time.perf_counter()
                    retry_slots, retry_crops = [], []
                    results = {}
                    for (slot, _, box, crop, _), (raw, score) in zip(dirty, self._read_titles(crops)):
                        row = self._line(raw, score, box, width, height)
                        row["box_norm"] = list(slot["title_box_norm"])
                        matched = self._match_lines([row])
                        if theme_key == "closure" and not matched and row["raw_text"].rstrip().endswith("·"):
                            # The Closure sparkle can be read as a trailing dot.
                            trimmed = row["raw_text"].rstrip().removesuffix("·")
                            matched = self._match_lines([{**row, "raw_text": trimmed}])
                            if matched:
                                matched[0]["raw_text"] = row["raw_text"]
                        results[slot["id"]] = (row, matched[0] if matched else None)
                        if not matched and theme_key == "iron":
                            # Separate light lettering from the blue ice and textured banner.
                            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                            _, lettering = cv2.threshold(gray, 175, 255, cv2.THRESH_BINARY)
                            retry_crops.append(cv2.cvtColor(lettering, cv2.COLOR_GRAY2BGR))
                            retry_slots.append((slot, box))
                    if retry_slots:
                        for (slot, box), (raw, score) in zip(retry_slots, self._read_titles(retry_crops)):
                            row = self._line(raw, score, box, width, height)
                            row["box_norm"] = list(slot["title_box_norm"])
                            matched = self._match_lines([row])
                            if matched:
                                results[slot["id"]] = (row, matched[0])
                    for slot, fingerprint, box, crop, retry in dirty:
                        slot_id = slot["id"]
                        row, match = results[slot_id]
                        records[slot_id] = self._title_record(records.get(slot_id), fingerprint, row, match, crop,
                                                             box, sequence, captured_at, retry)
                    diagnostics["rec_ms"] = (time.perf_counter() - rec_started) * 1000
                changed = len(dirty) + emptied
                diagnostics.update(path=f"official_{theme_key}_titles" if changed else "unchanged_official_slots",
                                   changed_slots=changed, reused_slots=len(slots)-changed,
                                   recognized_lines=len(dirty))
                result["ocr_reused"] = not dirty
                if epoch != self._layout_epoch or (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                    result.update(status="frame_stale", error="商店画面已变化，等待新画面。")
                    return result
                self._title_layout = (layout_key, records)
                for slot in slots:
                    record = records[slot["id"]]
                    match = record.match
                    region = slot["title_box_norm"]
                    result["stability_regions_norm"].append(region)
                    if record.raw is not None:
                        result["raw_lines"].append(dict(record.raw))
                    if (match is None and record.proof is not None and captured_at < record.hold_until
                            and _rect_pixels(region, width, height) == record.proof.box):
                        match = {**record.proof.match, "title_hold_until": record.hold_until}
                    if match is not None:
                        left, top, right, bottom = slot["box_norm"]
                        result["candidates"].append({**match, "border_slot_id": slot["id"],
                            "slot_roi_norm": slot["box_norm"],
                            "border_anchor_norm": [left + .04 * (right-left), top + .60 * (bottom-top)],
                            "border_layout_key": layout_key, "name_roi_norm": region})
                result.update(status="ready" if result["candidates"] else "no_candidates", error=None)
        except Exception as error:
            self._title_layout = None
            result.update(status="error", error=str(error), candidates=[], raw_lines=[])
        finally:
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result
