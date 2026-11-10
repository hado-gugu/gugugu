"""Small independent OCR settings file; never writes presets or calibration.

load_settings(path) -> (settings_dict, warnings). Loading never writes a file.
save_settings(path, settings) -> saved_dict. Explicit saves preserve unknown
JSON fields from an existing object and atomically replace this settings file.
resolve_ocr_threads(choice, cpu_count=None) -> concrete 1/2/4/8 native thread count.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile


THREAD_CHOICES = ("auto", 1, 2, 4, 8)
DEFAULT_SETTINGS = {"ocr_threads": 2}


def validate_thread_choice(choice):
    if choice == "auto" and type(choice) is str:
        return choice
    if type(choice) is int and choice in (1, 2, 4, 8):
        return choice
    raise ValueError("识别线程设置必须为 auto、1、2、4 或 8。")


def resolve_ocr_threads(choice="auto", cpu_count=None):
    """Logical CPU heuristic; a valid explicit override is never downgraded."""
    choice = validate_thread_choice(choice)
    if choice != "auto":
        return choice
    logical = os.cpu_count() if cpu_count is None else cpu_count
    if type(logical) is not int or logical < 1:
        logical = 1
    return 4 if logical >= 8 else 2 if logical >= 4 else 1


def load_settings(path):
    """Return safe defaults and warnings on a damaged/missing configuration.

    A missing file is the normal first-run case, with no warning. Unknown JSON
    fields in a valid object are retained for future or independently owned UI
    preferences. No recovery path mutates an existing file.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"),
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"无效数值 {value}")))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS), []
    except (OSError, UnicodeError, ValueError) as exc:
        return dict(DEFAULT_SETTINGS), [f"无法读取性能设置，已使用 2 线程：{exc}"]
    if not isinstance(payload, dict):
        return dict(DEFAULT_SETTINGS), ["性能设置格式无效，已使用 2 线程。"]
    settings = copy.deepcopy(payload)
    warnings = []
    try:
        settings["ocr_threads"] = validate_thread_choice(settings.get("ocr_threads", DEFAULT_SETTINGS["ocr_threads"]))
    except ValueError:
        settings["ocr_threads"] = DEFAULT_SETTINGS["ocr_threads"]
        warnings.append("保存的识别线程设置无效，已使用 2 线程。")
    return settings, warnings


def save_settings(path, settings):
    """Save explicit user settings with same-directory temporary + os.replace.

    Serialization and validation finish before touching the destination. A
    failed write/replace leaves the old file intact and removes its temporary.
    Calling this for a damaged old JSON file explicitly repairs that file; merely
    loading damaged settings never does so.
    """
    path = Path(path)
    if not isinstance(settings, dict):
        raise ValueError("性能设置必须为 JSON 对象。")
    existing, _ = load_settings(path)
    merged = {**existing, **copy.deepcopy(settings)}
    merged["ocr_threads"] = validate_thread_choice(merged.get("ocr_threads", DEFAULT_SETTINGS["ocr_threads"]))
    encoded = json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return merged
