"""Name-row structure checks that separate ink geometry from hover colors.

This is a change detector, not OCR. It never guesses an operator name. True is
positive evidence of unchanged structure, False is a clear local structure
change, and None means that the caller should obtain fresh OCR evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class NameSignature:
    gray: np.ndarray
    edges: np.ndarray
    border: np.ndarray

    @property
    def shape(self):
        return self.gray.shape


def build_name_signature(image_bgr: np.ndarray) -> NameSignature:
    if (not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8
            or image_bgr.ndim != 3 or image_bgr.shape[2] != 3
            or min(image_bgr.shape[:2]) < 4):
        raise ValueError("Name structure requires a nonempty uint8 BGR image.")
    # Floating luminance avoids the extra rounding of an 8-bit grayscale image.
    pixels = image_bgr.astype(np.float32)
    gray = pixels[:, :, 0] * .114 + pixels[:, :, 1] * .587 + pixels[:, :, 2] * .299
    dx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3, scale=1 / 8)
    dy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3, scale=1 / 8)
    local_range = cv2.dilate(gray, np.ones((9, 9), np.uint8)) - cv2.erode(gray, np.ones((9, 9), np.uint8))
    normalized = cv2.magnitude(dx, dy) / np.maximum(local_range, 1.0)
    edges = ((normalized > .105) & (local_range >= 7.0)).astype(np.uint8)
    height, width = gray.shape
    horizontal_length = min(width, max(48, round(height * .9)))
    vertical_length = min(height, max(32, round(height * .75)))
    horizontal = cv2.morphologyEx(edges, cv2.MORPH_OPEN, np.ones((1, horizontal_length), np.uint8))
    vertical = cv2.morphologyEx(edges, cv2.MORPH_OPEN, np.ones((vertical_length, 1), np.uint8))
    # Suppress only actual long-line pixels and their immediate antialias fringe.
    # No card, slot or full x column is excluded from text-change detection.
    border = cv2.dilate(np.maximum(horizontal, vertical), np.ones((3, 3), np.uint8)).astype(bool)
    edges = edges.astype(bool) & ~border
    return NameSignature(gray, edges, border)


def _photometric_same(a, b, valid):
    """A strict, positive-gain affine-lighting test for one local text tile.

    Small per-pixel rounding is allowed. A handful of changed high-contrast ink
    pixels is enough to fail; an entire character is never averaged into the
    background of a full shop row.
    """
    x, y = a[valid], b[valid]
    if x.size < 12:
        return None
    if np.array_equal(x, y):
        return True
    x_mean, y_mean = float(np.mean(x)), float(np.mean(y))
    x_var, y_var = float(np.var(x)), float(np.var(y))
    if x_var < 1.5 and y_var < 1.5:
        return True  # Uniform background fill changed, with no text to hide.
    if min(x_var, y_var) < 1.5:
        return False
    gain = float(np.mean((x - x_mean) * (y - y_mean))) / x_var
    if not .12 <= gain <= 8.0:
        return False
    prediction = np.clip(gain * (x - x_mean) + y_mean, 0, 255)
    residual = np.abs(y - prediction)
    # Quantization can cause <=2 intensity units after dimming/tinting. A changed
    # stroke produces many far larger residuals. Do not use a whole-row average.
    outliers = int(np.count_nonzero(residual > 4.5))
    return outliers <= 2 and float(np.max(residual, initial=0)) <= 9.0


def _shifted_mask(image, dx, dy):
    result = np.zeros_like(image)
    height, width = image.shape
    source_x0, source_x1 = max(0, -dx), min(width, width - dx)
    source_y0, source_y1 = max(0, -dy), min(height, height - dy)
    if source_x0 < source_x1 and source_y0 < source_y1:
        result[source_y0 + dy:source_y1 + dy, source_x0 + dx:source_x1 + dx] = image[source_y0:source_y1, source_x0:source_x1]
    return result


def structure_metrics(first: NameSignature, second: NameSignature, *, strict: bool = False) -> dict:
    """Return the tri-state decision plus local metrics for tests/diagnostics."""
    if first.shape != second.shape:
        return {"same": False, "reason": "shape_changed", "tiles": []}
    height, width = first.shape
    excluded = first.border | second.border
    tile_width = max(20, min(40, round(height * .35)))
    tiles = []
    uncertain = False
    for left in range(0, width, tile_width):
        right = min(width, left + tile_width)
        valid = ~excluded[:, left:right]
        a_gray, b_gray = first.gray[:, left:right], second.gray[:, left:right]
        photo_same = _photometric_same(a_gray, b_gray, valid)
        if photo_same is True:
            tiles.append({"left": left, "right": right, "same": True, "path": "local_photometric"})
            continue
        if strict:
            # Cache reuse has no geometric tolerance: uncertain pixels trigger
            # OCR, even if the looser A/B display comparison could tolerate them.
            tiles.append({"left": left, "right": right, "same": None, "path": "cache_requires_ocr"})
            uncertain = True
            continue
        a = first.edges[:, left:right] & valid
        b = second.edges[:, left:right] & valid
        count_a, count_b = int(np.count_nonzero(a)), int(np.count_nonzero(b))
        if max(count_a, count_b) < 6:
            tiles.append({"left": left, "right": right, "same": None, "path": "insufficient_structure"})
            uncertain = True
            continue
        best_mismatch = max(count_a, count_b)
        best_shift = (0, 0)
        # One coherent translation for the whole tile; never dilate each stroke
        # independently, which could erase the difference between similar glyphs.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                shifted = _shifted_mask(b, dx, dy)
                mismatch = int(np.count_nonzero(a ^ shifted))
                if mismatch < best_mismatch:
                    best_mismatch, best_shift = mismatch, (dx, dy)
        relative = best_mismatch / max(1, count_a + count_b)
        if best_mismatch <= 3 or relative <= .065:
            decision = True
        elif best_mismatch >= 8 and relative >= .12:
            decision = False
        else:
            decision = None
            uncertain = True
        tiles.append({"left": left, "right": right, "same": decision, "path": "local_edge_geometry",
                      "edge_pixels": [count_a, count_b], "mismatched_pixels": best_mismatch,
                      "relative_mismatch": relative, "shift": list(best_shift)})
        if decision is False:
            return {"same": False, "reason": "local_structure_changed", "tiles": tiles}
    return {"same": None if uncertain else True,
            "reason": "uncertain" if uncertain else "same_structure", "tiles": tiles}


def compare_name_signatures(first: NameSignature, second: NameSignature, *, strict: bool = False) -> bool | None:
    return structure_metrics(first, second, strict=strict)["same"]


def same_name_structure(before_bgr: np.ndarray, after_bgr: np.ndarray, *, strict: bool = False) -> bool | None:
    try:
        first = build_name_signature(before_bgr)
        second = build_name_signature(after_bgr)
        return compare_name_signatures(first, second, strict=strict)
    except (ValueError, TypeError, cv2.error):
        return None
