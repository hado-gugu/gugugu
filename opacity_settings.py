"""Window opacity preferences in the companion's shared settings.json."""

from collections.abc import Mapping
from pathlib import Path

from performance_settings import load_settings, save_settings


MIN_OPACITY = 20
MAX_OPACITY = 100
DEFAULT_OPACITY = 100
WINDOW_LABELS = {
    "controller": "主控悬浮窗",
    "planner": "配队预设窗口",
    "lookup": "干员速查窗口",
    "detail": "干员详情窗口",
    "overlay": "商店提醒层",
    "calibration": "商店校准窗口",
    "tutorial": "教程窗口",
}
_SETTINGS_KEY = "window_opacity"


def _is_opacity(value) -> bool:
    return type(value) is int and MIN_OPACITY <= value <= MAX_OPACITY


def normalize_opacity(value) -> int:
    """Use full opacity for a missing or invalid stored percentage."""
    return value if _is_opacity(value) else DEFAULT_OPACITY


def get_window_opacities(settings: Mapping | None) -> dict[str, int]:
    """Read each known window preference without changing the settings."""
    stored = settings.get(_SETTINGS_KEY) if isinstance(settings, Mapping) else None
    if not isinstance(stored, Mapping):
        stored = {}
    return {key: normalize_opacity(stored.get(key)) for key in WINDOW_LABELS}


def save_window_opacities(path: str | Path, values: Mapping[str, int]) -> dict:
    """Atomically update the given windows and return the shared settings."""
    if not isinstance(values, Mapping):
        raise ValueError("窗口不透明度必须按窗口编号保存。")
    updates = dict(values)
    for key, value in updates.items():
        if key not in WINDOW_LABELS:
            raise ValueError(f"未知的窗口编号：{key}")
        if not _is_opacity(value):
            raise ValueError(f"{WINDOW_LABELS[key]}的不透明度必须为 {MIN_OPACITY} 到 {MAX_OPACITY} 的整数。")

    settings, _ = load_settings(path)
    if not updates:
        return settings
    stored = settings.get(_SETTINGS_KEY)
    opacities = dict(stored) if isinstance(stored, Mapping) else {}
    opacities.update(updates)
    # Submit only this field so the shared writer preserves other preferences.
    return save_settings(path, {_SETTINGS_KEY: opacities})
