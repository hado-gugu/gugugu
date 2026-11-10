"""Audit a community operator workbook and optionally update the bundled tags.

Run with a Python environment containing openpyxl. Omit --write to review first.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from operator_reference import REFERENCE_PATH
from planner import Catalog


TIERS = {name: tier for tier, name in enumerate(("I", "II", "III", "IV", "V", "VI"), 1)}
BOND_COLUMNS = ("特性1", "特性2", "特性3")
FORM_COLUMNS = (("normal", "普通售价", "特殊说明（普通）"), ("elite", "精锐售价", "特殊说明（精锐）"))
REQUIRED_COLUMNS = {"干员名称", "阶数", "触发时机", "效果标签", *BOND_COLUMNS,
                    *(column for _, price, desc in FORM_COLUMNS for column in (price, desc))}


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).strip() if value is not None else ""


def split_labels(value):
    return list(dict.fromkeys(label.strip() for label in normalized(value).split(",") if label.strip()))


def audit(workbook_path, catalog):
    current = defaultdict(list)
    for operator in catalog.operators:
        current[normalized(operator["name"])].append(operator)
    hidden = {normalized(operator["name"]) + "(隐藏)" for operator in catalog.all_operators
              if operator.get("hidden") and not operator.get("retired")}
    operators = {}
    report = {"hidden": [], "placeholders": [], "wordingDifferences": [], "errors": []}
    seen = set()

    workbook = load_workbook(workbook_path, read_only=True, data_only=True, keep_links=False)
    try:
        sheet = workbook["干员表"]
        # This export can declare A1:A1 even when the sheet contains a full roster.
        sheet.reset_dimensions()
        rows = iter(sheet.values)
        headers = [value.strip() if isinstance(value, str) else "" for value in next(rows, ())]
        if REQUIRED_COLUMNS.difference(headers) or len(set(headers)) != len(headers):
            raise ValueError("干员表缺少必需的列或存在重复列名。")
        for line, values in enumerate(rows, 2):
            if all(value is None for value in values):
                continue
            data = dict(zip(headers, values))
            name = normalized(data.get("干员名称"))
            if not name or name in seen:
                report["errors"].append(f"第 {line} 行名称缺失或重复：{name}")
                continue
            seen.add(name)
            if name in hidden:
                report["hidden"].append(name)
                continue
            if re.fullmatch(r"自选调配干员\(槽位[1-6]\)", name):
                report["placeholders"].append(name)
                continue
            matches = current.get(name, ())
            if len(matches) != 1:
                report["errors"].append(f"第 {line} 行无法唯一匹配：{name}")
                continue
            operator = matches[0]
            char_id = operator["charId"]
            bonds = {normalized(catalog.bond_by_id[bond]["name"]) for bond in operator["bonds"]}
            supplied_bonds = {normalized(data.get(column)) for column in BOND_COLUMNS} - {""}
            if TIERS.get(normalized(data.get("阶数"))) != operator["tier"] or supplied_bonds != bonds:
                report["errors"].append(f"第 {line} 行的阶数或盟约需要核对：{name}")
            for form_key, price_column, desc_column in FORM_COLUMNS:
                form = operator["forms"][form_key]
                if data.get(price_column) != form["purchasePrice"]:
                    report["errors"].append(f"第 {line} 行的{price_column}与购买价不同：{name}")
                if normalized(data.get(desc_column)) != normalized(form.get("desc")):
                    report["wordingDifferences"].append({"line": line, "operator": name, "form": form_key})
            operators[char_id] = {
                "effectTags": split_labels(data.get("效果标签")),
                "triggerTimes": split_labels(data.get("触发时机")),
            }
    finally:
        workbook.close()
    missing = [operator["name"] for operator in catalog.operators if operator["charId"] not in operators]
    if missing:
        report["errors"].append("表中缺少现役干员：" + "、".join(missing))
    report["matched"] = len(operators)
    return operators, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--write", action="store_true", help="Write tags after the audit passes")
    args = parser.parse_args()
    catalog = Catalog(ROOT / "catalog.json")
    if not catalog.season_id:
        parser.error("客户端目录缺少赛季标识。")
    operators, report = audit(args.workbook, catalog)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["errors"]:
        return 1
    if args.write:
        destination = ROOT / REFERENCE_PATH
        payload = {"seasonId": catalog.season_id, "operators": dict(sorted(operators.items()))}
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
                                             prefix="operator-reference-", suffix=".tmp", delete=False) as handle:
                pending = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            pending.replace(destination)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
