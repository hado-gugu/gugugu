"""Locate official shop rows from visible, theme-specific landmarks."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path
import math

import cv2
import numpy as np

from shop_geometry import _bounds


_ASSETS = Path(__file__).resolve().parent / "assets" / "official-shop"


@dataclass(frozen=True)
class _Landmark:
    x: float
    y: float
    tolerance: float
    threshold: float


@dataclass(frozen=True)
class _Theme:
    key: str
    name: str
    reference_size: tuple[int, int]
    fold: _Landmark
    card: tuple[int, int, int, int]
    pitch: float
    title: tuple[int, int, int, int]
    trim: _Landmark
    footer: _Landmark
    empty: _Landmark | None = None
    expand: _Landmark | None = None
    frozen: _Landmark | None = None
    covered: _Landmark | None = None


# Card, title and slot landmarks refer to the rightmost card. Remaining
# positions follow the same pitch, including purchased, empty slots.
_CLOSURE = _Theme(
    key="closure", name="可露希尔", reference_size=(2560, 1600),
    fold=_Landmark(2337, 1491, 37, .83),
    card=(1940, 1220, 2220, 1591), pitch=875 / 3,
    title=(2005, 1498, 2209, 1547),
    trim=_Landmark(2016, 1244, 9, .86),
    footer=_Landmark(2008, 1560, 9, .81),
    empty=_Landmark(2004, 1348, 12, .86),
)
_TERMINAL = _Theme(
    key="terminal", name="卫戍协议终端", reference_size=(2560, 1600),
    fold=_Landmark(2343, 1497, 35, .83),
    card=(1941, 1243, 2223, 1586), pitch=875 / 3,
    title=(1994, 1520, 2216, 1555),
    trim=_Landmark(1995, 1504, 9, .83),
    footer=_Landmark(1946, 1555, 9, .82),
    empty=_Landmark(2020, 1397, 10, .86),
    expand=_Landmark(2328, 1496, 35, .83),
    frozen=_Landmark(2057, 1550, 10, .86),
    covered=_Landmark(2012, 1514, 10, .86),
)
_IRON = _Theme(
    key="iron", name="乌萨斯齐心团", reference_size=(2560, 1600),
    fold=_Landmark(2353, 1465, 35, .83),
    card=(1941, 1243, 2223, 1587), pitch=875 / 3,
    title=(2007, 1501, 2211, 1543),
    trim=_Landmark(2172, 1502, 9, .83),
    footer=_Landmark(1948, 1558, 9, .82),
    empty=_Landmark(1968, 1287, 9, .86),
    expand=_Landmark(2336, 1465, 35, .86),
    frozen=_Landmark(2054, 1538, 10, .86),
    covered=_Landmark(1988, 1385, 9, .86),
)
_THEMES = (_CLOSURE, _TERMINAL, _IRON)


@lru_cache(maxsize=8)
def _templates(theme):
    templates = {}
    names = ("fold", "card-trim", "card-footer")
    if theme.empty is not None:
        names += ("empty-mark",)
    if theme.expand is not None:
        names += ("expand",)
    if theme.frozen is not None:
        names += ("card-frozen",)
    if theme.covered is not None:
        names += ("card-covered",)
    for name in names:
        try:
            data = (_ASSETS / f"{theme.key}-{name}.png").read_bytes()
            template = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        except (OSError, cv2.error):
            return {}
        if template is None or min(template.shape) < 4 or float(template.std()) < 1:
            return {}
        templates[name] = template
    return templates


@lru_cache(maxsize=24)
def _scaled_templates(theme, scale):
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return {name: cv2.resize(template, None, fx=scale, fy=scale, interpolation=interpolation)
            for name, template in _templates(theme).items()}


def _match(image, template, x, y, tolerance):
    height, width = image.shape[:2]
    th, tw = template.shape
    left = max(0, math.floor(x - tolerance))
    top = max(0, math.floor(y - tolerance))
    right = min(width, math.ceil(x + tw + tolerance))
    bottom = min(height, math.ceil(y + th + tolerance))
    if right - left < tw or bottom - top < th:
        return 0.0, x, y
    patch = cv2.cvtColor(image[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    scores = cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (dx, dy) = cv2.minMaxLoc(scores)
    return float(score), left + dx, top + dy


def _detect_at_scale(image, roi, theme, scale):
    templates = _scaled_templates(theme, scale)
    if not templates:
        return None, False
    height, width = image.shape[:2]
    ref_width, ref_height = theme.reference_size

    def match_panel(name, landmark):
        x = width + (landmark.x - ref_width) * scale
        y = height + (landmark.y - ref_height) * scale
        return _match(image, templates[name], x, y, landmark.tolerance * scale)

    fold_score, fold_x, fold_y = match_panel("fold", theme.fold)
    if fold_score < theme.fold.threshold:
        expand = theme.expand
        return None, expand is not None and match_panel("expand", expand)[0] >= expand.threshold

    dx = fold_x - theme.fold.x * scale
    dy = fold_y - theme.fold.y * scale

    def normalized(rect, x_offset):
        left, top, right, bottom = rect
        return [((left + x_offset) * scale + dx) / width, (top * scale + dy) / height,
                ((right + x_offset) * scale + dx) / width, (bottom * scale + dy) / height]

    def score_slot(name, landmark, x_offset):
        return _match(image, templates[name], (landmark.x + x_offset) * scale + dx,
                      landmark.y * scale + dy, landmark.tolerance * scale)[0]

    slots = []
    scores = []
    for index in range(6):
        x_offset = -theme.pitch * index
        box = normalized(theme.card, x_offset)
        if not (roi[0] <= box[0] < box[2] <= roi[2] and roi[1] <= box[1] < box[3] <= roi[3]):
            break
        # The top trim survives freeze and purchase confirmation. The footer
        # offers independent evidence when hover or a price badge covers it.
        trim = score_slot("card-trim", theme.trim, x_offset)
        footer = score_slot("card-footer", theme.footer, x_offset)
        empty_score = score_slot("empty-mark", theme.empty, x_offset) if theme.empty else 0.
        empty = theme.empty is not None and empty_score >= theme.empty.threshold
        covered_score = score_slot("card-covered", theme.covered, x_offset) if theme.covered else 0.
        covered = theme.covered is not None and covered_score >= theme.covered.threshold
        score = max(trim, footer, empty_score if empty else 0., covered_score if covered else 0.)
        if not (empty or covered) and trim < theme.trim.threshold and footer < theme.footer.threshold:
            if theme.frozen is None:
                break
            score = score_slot("card-frozen", theme.frozen, x_offset)
            if score < theme.frozen.threshold:
                break
        title = normalized(theme.title, x_offset)
        slots.append({"box_norm": box, "name_roi_norm": list(box), "title_box_norm": title,
                      "anchor_norm": box[:2], "empty": empty, "covered": covered})
        scores.append(score)
    if len(slots) < 4:
        return None, True

    fingerprint = (width, height, tuple(roi), len(slots), round(scale, 4),
                   round((dx + ref_width * scale - width) / 8),
                   round((dy + ref_height * scale - height) / 8))
    layout_key = theme.key + "-" + blake2b(repr(fingerprint).encode("ascii"), digest_size=8).hexdigest()
    slots.reverse()
    for index, slot in enumerate(slots):
        slot["id"] = f"{layout_key}:{index}"
    return {"slots": slots, "layout_key": layout_key, "confidence": min(.99, fold_score, min(scores)),
            "theme_key": theme.key, "theme": theme.name,
            "frame_size": [width, height], "roi_norm": list(roi),
            "diagnostics": {"path": f"{theme.key}_shop_geometry", "slot_count": len(slots),
                            "fold_score": fold_score, "min_slot_score": min(scores),
                            "scale": scale, "empty_slot_count": sum(slot["empty"] for slot in slots)}}, True


def locate_official_shop(image_bgr, roi_norm, *, themes=_THEMES):
    """Return (verified grid, recognized HUD), without reading card text.

    The fold label gates the open state and each slot needs independent art
    evidence. Coordinates are normalized to the client image, right exclusive.
    The input ROI must include the card row. The fold anchor can lie outside it.
    """
    if _bounds(image_bgr, roi_norm) is None:
        return None, False
    roi = tuple(float(value) for value in roi_norm)
    height, width = image_bgr.shape[:2]
    recognized_hud = False
    for theme in themes:
        ref_width, ref_height = theme.reference_size
        height_scale = round(height / ref_height, 5)
        width_scale = round(width / ref_width, 5)
        scales = [height_scale]
        if abs(width_scale - height_scale) >= .02 * height_scale:
            scales.append(width_scale)
        for scale in scales:
            if .35 <= scale <= 3:
                result, recognized = _detect_at_scale(image_bgr, roi, theme, scale)
                if result is not None:
                    return result, True
                recognized_hud |= recognized
    return None, recognized_hud


def detect_closure_grid(image_bgr, roi_norm):
    return locate_official_shop(image_bgr, roi_norm, themes=(_CLOSURE,))[0]
