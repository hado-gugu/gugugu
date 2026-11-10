"""Website-compatible local presets. This module never reads or controls a game.

UI API: ``catalog = Catalog(); store = PlannerStore(path, catalog)``.
``store.state`` returns a detached dictionary with the website's five state fields;
``store.active()`` returns the selected preset or None. Mutations raise PlannerError
and persist successfully before publishing new state. Imports append by default.
Catalog.operators and Catalog.equipment provide the current season's choices.
Catalog.all_operators and Catalog.all_equipment keep historical records readable.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

VERSION = 2
EXPORT_KIND = "stronghold.team-planner"
MAX_TARGETS = 20
MAX_TRANSITIONS = 20
MAX_IMPORT_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_UNSAFE = {"__proto__", "constructor", "prototype"}
_STATE_KEYS = {"version", "presets", "activeId", "transitions", "enabled"}


class PlannerError(ValueError):
    """A validation or persistence failure safe to display to the user."""


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and value not in _UNSAFE


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _new_id() -> str:
    return "plan_" + uuid.uuid4().hex


def validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise PlannerError("预设名称必须是文字。")
    try:
        length = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise PlannerError("预设名称包含无效字符。") from exc
    if not name.strip() or length > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise PlannerError("预设名称需为 1–80 个字符，且不能包含控制字符。")
    return name.strip()


def _checked_ids(values: Any, limit: int | None, label: str) -> list[str]:
    if not isinstance(values, list):
        raise PlannerError(f"{label}必须是编号数组。")
    if limit is not None and len(values) > limit:
        raise PlannerError(f"{label}最多 {limit} 名；文件未导入。")
    if not all(valid_id(item) for item in values):
        raise PlannerError(f"{label}包含无效或不安全的编号。")
    return _unique(values)


def validate_state(raw: Any) -> dict:
    """Strict website v1/v2 validation; return normalized v2 without dropping IDs."""
    if not isinstance(raw, dict) or not set(raw).issubset(_STATE_KEYS):
        raise PlannerError("配队数据格式不正确。")
    version = raw.get("version")
    if type(version) is not int or version not in (1, VERSION):
        raise PlannerError("不支持此配队文件版本。")
    if not isinstance(raw.get("presets"), list):
        raise PlannerError("预设列表格式不正确。")
    if type(raw.get("enabled")) is not bool:
        raise PlannerError("提醒开关格式不正确。")
    transitions = _checked_ids(raw.get("transitions"), MAX_TRANSITIONS, "全局过渡干员")
    presets: list[dict] = []
    seen: set[str] = set()
    preset_keys = {"id", "name", "targets"} | ({"equipment"} if version == VERSION else set())
    for row in raw["presets"]:
        if not isinstance(row, dict) or not set(row).issubset(preset_keys):
            raise PlannerError("预设条目格式不正确。")
        preset_id = row.get("id")
        if not valid_id(preset_id) or preset_id in seen:
            raise PlannerError("预设编号无效或重复。")
        seen.add(preset_id)
        name = validate_name(row.get("name"))
        targets = _checked_ids(row.get("targets"), MAX_TARGETS, f"预设「{name}」目标干员")
        equipment = [] if version == 1 else _checked_ids(row.get("equipment"), None, "装备预设")
        presets.append({"id": preset_id, "name": name, "targets": targets, "equipment": equipment})
    if "activeId" not in raw:
        raise PlannerError("当前预设编号不存在。")
    active_id = raw["activeId"]
    if active_id is not None and (not valid_id(active_id) or active_id not in seen):
        raise PlannerError("当前预设编号不存在。")
    return {"version": VERSION, "presets": presets, "activeId": active_id,
            "transitions": transitions, "enabled": raw["enabled"]}


def new_state() -> dict:
    preset_id = _new_id()
    return {"version": VERSION,
            "presets": [{"id": preset_id, "name": "配队 1", "targets": [], "equipment": []}],
            "activeId": preset_id, "transitions": [], "enabled": True}


def active_preset(state: dict) -> dict | None:
    return next((p for p in state.get("presets", []) if p["id"] == state.get("activeId")), None)


def reminder_of(state: dict, char_id: str) -> str | None:
    if not state.get("enabled") or not valid_id(char_id):
        return None
    preset = active_preset(state)
    if preset and char_id in preset["targets"]:
        return "target"
    return "transition" if char_id in state.get("transitions", []) else None


class Catalog:
    """Small offline website catalogue; unknown imported IDs remain valid presets."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else Path(__file__).with_name("catalog.json")
        with self.path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        self.season_id = payload.get("source", {}).get("seasonId")
        self.all_operators: list[dict] = payload["operators"]
        current_operators = [row for row in self.all_operators if not row.get("retired", False)]
        self.operators = [row for row in current_operators if not row.get("hidden", False)]
        self.all_equipment: list[dict] = payload["equipment"]
        self.equipment = [row for row in self.all_equipment if not row.get("retired", False)]
        self.bonds: list[dict] = payload["bonds"]
        self.operator_by_id = {row["charId"]: row for row in self.all_operators}
        self.equipment_by_id = {row["equipmentId"]: row for row in self.all_equipment}
        self.bond_by_id = {row["bondId"]: row for row in self.bonds}
        self.recognized_names = [row["name"] for row in current_operators + self.equipment]
        self.operator_aliases = {char_id: char_id for char_id in self.operator_by_id}
        self.equipment_aliases = {equipment_id: equipment_id for equipment_id in self.equipment_by_id}
        # Preserve the historical owner of reused shop slot IDs. A canonical
        # character ID always takes precedence over any slot alias.
        for row in self.all_operators:
            for alias in row.get("legacyChessIds", []):
                self.operator_aliases.setdefault(alias, row["charId"])
        for row in sorted(self.all_operators, key=lambda row: not row.get("retired", False)):
            for alias in row.get("chessIds", []):
                self.operator_aliases.setdefault(alias, row["charId"])
        for row in self.all_equipment:
            for alias in row.get("variantIds", []):
                self.equipment_aliases.setdefault(alias, row["equipmentId"])
        self._name_index: dict[str, list[dict]] = {}
        for kind, rows, id_key in (("operator", current_operators, "charId"),
                                   ("equipment", self.equipment, "equipmentId")):
            for row in rows:
                candidate = {"kind": kind, "id": row[id_key], "name": row["name"]}
                for name in [row["name"], row.get("appellation", ""), *row.get("aliases", [])]:
                    key = self.normalize_name(name)
                    if key and candidate not in self._name_index.setdefault(key, []):
                        self._name_index[key].append(candidate)

    @staticmethod
    def normalize_name(value: str) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[\s|·•,，。.!！:：;；()（）\[\]【】]", "", value).casefold()

    def resolve_name(self, text: str) -> list[dict]:
        """Exact normalized whole-name lookup; no substring, fuzzy, or slot guesses."""
        return copy.deepcopy(self._name_index.get(self.normalize_name(text), []))

    def resolve_operator_id(self, value: str) -> str:
        return self.operator_aliases.get(value, value)

    def resolve_equipment_id(self, value: str) -> str:
        return self.equipment_aliases.get(value, value)

    def name_of(self, value: str, kind: str = "operator") -> str:
        if kind == "equipment":
            row = self.equipment_by_id.get(self.resolve_equipment_id(value))
        else:
            row = self.operator_by_id.get(self.resolve_operator_id(value))
        return row["name"] if row else value

    def reminder(self, state: dict, kind: str, value: str) -> str | None:
        if kind in ("operator", "chess", "char"):
            char_id = self.resolve_operator_id(value)
            if self.operator_by_id.get(char_id, {}).get("retired", False):
                return None
            return reminder_of(state, char_id)
        if kind not in ("equipment", "item") or not state.get("enabled"):
            return None
        family = self.resolve_equipment_id(value)
        equipment = self.equipment_by_id.get(family)
        if equipment is None or equipment.get("retired", False):
            return None
        preset = active_preset(state)
        selected = preset["equipment"] if preset else []
        return "target" if any(self.resolve_equipment_id(item) == family for item in selected) else None

    def unknown_ids(self, state: dict) -> dict:
        operator_ids = _unique([*state["transitions"],
                                *(value for p in state["presets"] for value in p["targets"])])
        equipment_ids = _unique([value for p in state["presets"] for value in p["equipment"]])
        return {"operators": [v for v in operator_ids if v not in self.operator_by_id],
                "equipment": [v for v in equipment_ids if self.resolve_equipment_id(v) not in self.equipment_by_id]}

    def bond_summary(self, target_ids: list[str], inactive_bond_ids: list[str] | None = None) -> list[dict]:
        """Natural bonds only, matching website thresholds and conditional elite rules."""
        members: dict[str, list[str]] = {}
        for char_id in _unique([v for v in target_ids if valid_id(v)]):
            row = self.operator_by_id.get(char_id, {})
            if row.get("retired", False):
                continue
            for bond_id in row.get("bonds", []):
                members.setdefault(bond_id, []).append(char_id)
        inactive = set(inactive_bond_ids or [])
        rows = []
        for bond in self.bonds:
            bond_id = bond["bondId"]
            if bond_id not in members:
                continue
            count = len(members[bond_id])
            thresholds = sorted(v for v in bond.get("thresholds", []) if type(v) in (int, float) and v >= 0)
            if not thresholds:
                thresholds = [bond.get("activeCount", 2)]
            requires_elite = (bond.get("countMode") == "BOARD_ALL_CHESS"
                              or bond.get("thresholdTemplate") == "count_threshold_upward_golden")
            tier = sum(count >= value for value in thresholds)
            maximum = bond.get("maxCount")
            if bond.get("thresholdTemplate") == "count_threshold_downward":
                upper = maximum if type(maximum) in (int, float) else thresholds[0]
                tier = int(thresholds[0] <= count <= upper)
            if requires_elite or bond_id in inactive:
                tier = 0
            rows.append({"bondId": bond_id, "name": bond.get("name", bond_id), "count": count,
                         "naturalCount": count, "members": list(members[bond_id]), "thresholds": thresholds,
                         "tier": tier, "active": tier > 0, "off": bond_id in inactive,
                         "requiresElite": requires_elite, "countMode": bond.get("countMode"),
                         "maxCount": maximum, "isCore": bool(bond.get("isCore")),
                         "bondOrder": bond.get("bondOrder", 99)})
        rows.sort(key=lambda row: (row["off"], not row["active"], -row["count"], row["bondOrder"], row["bondId"]))
        return rows


def parse_import(text: str, catalog: Catalog | None = None) -> tuple[dict, str]:
    if not isinstance(text, str):
        raise PlannerError("请选择 JSON 配队文件或粘贴 JSON 文本。")
    try:
        if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise PlannerError("配队文件超过 8 MiB，请分批导入。")
        raw = json.loads(text.lstrip("\ufeff"))
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise PlannerError("JSON 内容无法读取，请确认文件完整。") from exc
    allowed = _STATE_KEYS | {"kind", "scope"}
    if (not isinstance(raw, dict) or not set(raw).issubset(allowed)
            or raw.get("kind") != EXPORT_KIND or raw.get("scope") not in ("current", "all")):
        raise PlannerError("这不是卫戍协议配队导出文件。")
    state = validate_state({key: value for key, value in raw.items() if key in _STATE_KEYS})
    if catalog:
        for row in state["presets"]:
            row["equipment"] = _unique([catalog.resolve_equipment_id(value) for value in row["equipment"]])
    return state, raw["scope"]


class PlannerStore:
    """Atomic local state. Never silently overwrites an unreadable existing file."""

    def __init__(self, path: Path | str, catalog: Catalog | None = None):
        self.path = Path(path)
        self.catalog = catalog
        self.load_error: str | None = None
        self._load_blocked = False
        self._state = new_state()
        if self.path.exists():
            try:
                self._state = validate_state(json.loads(self.path.read_text(encoding="utf-8-sig")))
            except (OSError, UnicodeError, ValueError) as exc:
                self.load_error = f"已保存的配队数据无法载入，原文件已保留：{exc}"
                self._load_blocked = True

    @property
    def state(self) -> dict:
        return copy.deepcopy(self._state)

    def active(self) -> dict | None:
        return copy.deepcopy(active_preset(self._state))

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _commit(self, candidate: dict) -> None:
        checked = validate_state(candidate)
        if self._load_blocked:
            raise PlannerError(self.load_error or "原配队文件无法读取，已停止写入以保留原文件。")
        try:
            self._write(checked)
        except OSError as exc:
            raise PlannerError(f"配队未保存，原有预设已保留：{exc}") from exc
        self._state = checked

    def save(self) -> None:
        self._commit(self._state)

    def _find(self, state: dict, preset_id: str | None = None) -> dict:
        preset_id = state["activeId"] if preset_id is None else preset_id
        preset = next((p for p in state["presets"] if p["id"] == preset_id), None)
        if preset is None:
            raise PlannerError("请先新建或选择一个预设。")
        return preset

    def create_preset(self, name: str) -> str:
        name = validate_name(name)
        candidate = self.state
        preset_id = _new_id()
        candidate["presets"].append({"id": preset_id, "name": name, "targets": [], "equipment": []})
        candidate["activeId"] = preset_id
        self._commit(candidate)
        return preset_id

    def duplicate_preset(self, preset_id: str | None = None) -> str:
        candidate = self.state
        row = copy.deepcopy(self._find(candidate, preset_id))
        row["id"] = _new_id()
        # 76 UTF-16 code units leave room for the website's four-character suffix.
        row["name"] = row["name"].encode("utf-16-le")[:152].decode("utf-16-le", errors="ignore") + "（副本）"
        candidate["presets"].append(row)
        candidate["activeId"] = row["id"]
        self._commit(candidate)
        return row["id"]

    def rename_preset(self, preset_id: str, name: str) -> None:
        candidate = self.state
        self._find(candidate, preset_id)["name"] = validate_name(name)
        self._commit(candidate)

    def delete_preset(self, preset_id: str | None = None) -> None:
        candidate = self.state
        row = self._find(candidate, preset_id)
        index = candidate["presets"].index(row)
        candidate["presets"].remove(row)
        if row["id"] == candidate["activeId"]:
            candidate["activeId"] = (candidate["presets"][min(index, len(candidate["presets"]) - 1)]["id"]
                                      if candidate["presets"] else None)
        self._commit(candidate)

    def select_preset(self, preset_id: str | None) -> None:
        candidate = self.state
        if preset_id is not None:
            self._find(candidate, preset_id)
        candidate["activeId"] = preset_id
        self._commit(candidate)

    @staticmethod
    def _toggle(values: list[str], value: str, selected: bool | None, limit: int | None) -> list[str]:
        if not valid_id(value):
            raise PlannerError("干员或装备编号无效。")
        wanted = value not in values if selected is None else selected
        if type(wanted) is not bool:
            raise PlannerError("选择状态无效。")
        if wanted and value not in values:
            if limit is not None and len(values) >= limit:
                raise PlannerError(f"此名单最多选择 {limit} 名干员。")
            return [*values, value]
        return list(values) if wanted else [v for v in values if v != value]

    def toggle_target(self, char_id: str, selected: bool | None = None) -> None:
        candidate = self.state
        row = self._find(candidate)
        row["targets"] = self._toggle(row["targets"], char_id, selected, MAX_TARGETS)
        self._commit(candidate)

    def toggle_transition(self, char_id: str, selected: bool | None = None) -> None:
        candidate = self.state
        candidate["transitions"] = self._toggle(candidate["transitions"], char_id, selected, MAX_TRANSITIONS)
        self._commit(candidate)

    def toggle_equipment(self, equipment_id: str, selected: bool | None = None) -> None:
        if not valid_id(equipment_id):
            raise PlannerError("装备编号无效。")
        candidate = self.state
        row = self._find(candidate)
        resolve = self.catalog.resolve_equipment_id if self.catalog else lambda value: value
        family = resolve(equipment_id)
        had = any(resolve(value) == family for value in row["equipment"])
        wanted = not had if selected is None else selected
        if type(wanted) is not bool:
            raise PlannerError("选择状态无效。")
        remaining = [value for value in row["equipment"] if resolve(value) != family]
        row["equipment"] = [*remaining, family] if wanted else remaining
        self._commit(candidate)

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise PlannerError("提醒开关无效。")
        candidate = self.state
        candidate["enabled"] = enabled
        self._commit(candidate)

    def import_json(self, text: str, apply_transitions: bool = False) -> dict:
        if type(apply_transitions) is not bool:
            raise PlannerError("导入选项无效。")
        incoming, scope = parse_import(text, self.catalog)
        candidate = self.state
        remap = {p["id"]: _new_id() for p in incoming["presets"]}
        for row in incoming["presets"]:
            candidate["presets"].append({**row, "id": remap[row["id"]]})
        candidate["activeId"] = remap.get(incoming["activeId"], candidate["activeId"])
        if apply_transitions:
            candidate["transitions"] = incoming["transitions"]
        # Website import intentionally keeps the recipient's reminder switch.
        checked_incoming = {**incoming, "transitions": incoming["transitions"] if apply_transitions else []}
        unknown = self.catalog.unknown_ids(checked_incoming) if self.catalog else {"operators": [], "equipment": []}
        warnings = []
        if unknown["operators"]:
            warnings.append(f"{len(unknown['operators'])} 名导入干员暂不在当前目录，已保留编号；无法匹配时不显示提醒。")
        if unknown["equipment"]:
            warnings.append("部分装备暂不在当前目录，已保留编号；无法匹配时不显示提醒。")
        self._commit(candidate)
        return {"imported": len(incoming["presets"]), "scope": scope, "warnings": warnings,
                "unknownIds": unknown["operators"], "unknownEquipmentIds": unknown["equipment"]}

    def export_json(self, scope: str = "all") -> str:
        if scope not in ("current", "all"):
            raise PlannerError("导出范围无效。")
        candidate = self.state
        if scope == "current":
            active = active_preset(candidate)
            candidate["presets"] = [active] if active else []
            candidate["activeId"] = active["id"] if active else None
        return json.dumps({"kind": EXPORT_KIND, "scope": scope, **candidate}, ensure_ascii=False, indent=2) + "\n"

