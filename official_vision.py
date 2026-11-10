"""Theme-confirmed title OCR for official shop cards."""
from __future__ import annotations

import time

import cv2
import numpy as np

from border_vision import BorderVisionEngine
from official_shop_geometry import locate_official_shop
from vision import _rect_pixels


class OfficialShopEngine(BorderVisionEngine):
    def __init__(self, *args, allow_legacy=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_legacy = allow_legacy
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
                        if record is None or record[0] is not None:
                            emptied += 1
                        records[slot["id"]] = (None, None, None)
                        continue
                    region = slot["title_box_norm"]
                    fingerprint = self._fingerprint(image_bgr, [region])
                    if record is not None and record[0] == fingerprint:
                        continue
                    left, top, right, bottom = _rect_pixels(region, width, height)
                    crops.append(np.ascontiguousarray(image_bgr[top:bottom, left:right]))
                    dirty.append((slot, fingerprint, (left, top, right, bottom)))
                if dirty:
                    rec_started = time.perf_counter()
                    retry_slots, retry_crops = [], []
                    for (slot, fingerprint, box), crop, (raw, score) in zip(dirty, crops, self._read_titles(crops)):
                        row = self._line(raw, score, box, width, height)
                        row["box_norm"] = list(slot["title_box_norm"])
                        matched = self._match_lines([row])
                        if theme_key == "closure" and not matched and row["raw_text"].rstrip().endswith("·"):
                            # The Closure sparkle can be read as a trailing dot.
                            trimmed = row["raw_text"].rstrip().removesuffix("·")
                            matched = self._match_lines([{**row, "raw_text": trimmed}])
                            if matched:
                                matched[0]["raw_text"] = row["raw_text"]
                        records[slot["id"]] = (fingerprint, row, matched[0] if matched else None)
                        if not matched and theme_key == "iron":
                            # Separate light lettering from the blue ice and textured banner.
                            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                            _, lettering = cv2.threshold(gray, 175, 255, cv2.THRESH_BINARY)
                            retry_crops.append(cv2.cvtColor(lettering, cv2.COLOR_GRAY2BGR))
                            retry_slots.append((slot, fingerprint, box))
                    if retry_slots:
                        for (slot, fingerprint, box), (raw, score) in zip(retry_slots, self._read_titles(retry_crops)):
                            row = self._line(raw, score, box, width, height)
                            row["box_norm"] = list(slot["title_box_norm"])
                            matched = self._match_lines([row])
                            if matched:
                                records[slot["id"]] = (fingerprint, row, matched[0])
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
                    _, raw, match = records[slot["id"]]
                    region = slot["title_box_norm"]
                    result["stability_regions_norm"].append(region)
                    if raw is not None:
                        result["raw_lines"].append(dict(raw))
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
