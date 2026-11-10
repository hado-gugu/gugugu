"""Build the V3_1 assistant catalog from decoded client tables and a legacy catalog."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from regulation_roster import build_bonds, build_operators


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_equipment(season, characters, old_rows):
    old_by_trap = {row["iconId"]: row for row in old_rows}
    old_aliases = {alias: row["iconId"] for row in old_rows
                   for alias in [row["equipmentId"], *row["variantIds"]]}
    rows, used, reused_ids = [], set(), []
    shop_rows = sorted(season["trapShopChessDatas"].values(),
                       key=lambda row: (row["itemLevel"], row["shopLevelSortId"], row["itemId"]))
    for shop in shop_rows:
        if shop["hideInShop"] or shop["itemType"] != 1:
            continue
        trap_id = shop["trapId"]
        if trap_id in used:
            raise ValueError(f"Duplicate equipment identity: {trap_id}")
        used.add(trap_id)
        old = old_by_trap.get(trap_id)
        family = old["equipmentId"] if old else trap_id
        aliases = list(old["variantIds"]) if old else [family]
        variants = []
        for slot_id in filter(None, (shop["itemId"], shop["goldenItemId"])):
            current = season["trapChessDataDict"][slot_id]
            if current["charId"] != trap_id:
                raise ValueError(f"Equipment variant points to another identity: {slot_id}")
            effect = season["effectInfoDataDict"][current["effectId"]]
            desc = re.sub(r"</>|<[@$][^>]+>", "", effect.get("effectDesc") or "")
            variants.append({"shopId": slot_id, "isGolden": current["isGolden"],
                             "purchasePrice": current["purchasePrice"], "desc": desc,
                             "effectId": current["effectId"], "giveBondId": current.get("giveBondId"),
                             "canGiveBond": current["canGiveBond"], "canFuse": current["canFuse"]})
            previous_owner = old_aliases.get(slot_id, trap_id)
            if previous_owner == trap_id:
                if slot_id not in aliases:
                    aliases.append(slot_id)
            else:
                reused_ids.append({"slotId": slot_id, "old": old_by_trap[previous_owner]["name"],
                                   "current": characters[trap_id]["name"]})
        primary = variants[0]
        rows.append({"equipmentId": family, "id": family, "name": characters[trap_id]["name"],
                     "tier": shop["itemLevel"], "itemType": "EQUIP", "variantIds": aliases,
                     "baseId": next((v["shopId"] for v in variants if not v["isGolden"]), None),
                     "goldenId": next((v["shopId"] for v in variants if v["isGolden"]), None),
                     "iconId": trap_id, "icon": f"assets/item/{trap_id}.png", "desc": primary["desc"],
                     "giveBondId": primary["giveBondId"], "variants": variants})
    rows.extend({**row, "retired": True} for row in old_rows if row["iconId"] not in used)
    return rows, reused_ids


def compare(old_rows, new_rows, key, fields):
    before = {row[key]: row for row in old_rows}
    after = {row[key]: row for row in new_rows if not row.get("retired")}
    changes = []
    for identifier in sorted(before.keys() & after.keys()):
        old, current = before[identifier], after[identifier]
        changed = {field: [old.get(field), current.get(field)] for field in fields
                   if old.get(field) != current.get(field)}
        if changed:
            changes.append({"id": identifier, "name": current["name"], "fields": changed})
    return {"current": len(after), "added": [{"id": k, "name": after[k]["name"]} for k in sorted(after.keys()-before.keys())],
            "removed": [{"id": k, "name": before[k]["name"]} for k in sorted(before.keys()-after.keys())],
            "changed": changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unpacked", type=Path)
    parser.add_argument("legacy", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    decoded = args.unpacked / "data" / "decoded"
    summary = read_json(decoded / "season-summary.json")
    if (summary["activityId"], summary["versionId"]) != ("act3autochess", "V3_1"):
        raise ValueError("Expected the 规程·序列 I data")
    season_path = decoded / "act3autochess-V3_1.json"
    season, old = read_json(season_path), read_json(args.legacy)
    characters = read_json(decoded / "character_table.json")["characters"]
    common = read_json(decoded / "autochess-data.json")
    operators = build_operators(season, characters, read_json(decoded / "profession-enum.json"),
                                read_json(decoded / "subprofession-table.json"), old["operators"],
                                read_json(decoded / "char_patch_table.json"))
    bonds = build_bonds(season, common, old["bonds"])
    equipment, reused_ids = build_equipment(season, characters, old["equipment"])
    source = {"type": "decoded-windows-client", "activityId": summary["activityId"],
              "seasonId": summary["versionId"], "title": summary["title"],
              "startLocal": summary["startLocal"], "dataVersion": summary["dataVersion"],
              "seasonTableSHA256": hashlib.sha256(season_path.read_bytes()).hexdigest(),
              "manifestVersion": read_json(args.unpacked / "data" / "manifest-provenance.json")["active"]["versionId"]}
    output = {"version": 2, "source": source, "operators": operators, "equipment": equipment, "bonds": bonds}
    audit = {"source": source,
             "operators": compare(old["operators"], operators, "charId", ("name", "tier", "profession", "rarity", "bonds", "hidden")),
             "equipment": compare(old["equipment"], equipment, "iconId", ("name", "tier", "desc", "giveBondId", "baseId", "goldenId")),
             "bonds": compare(old["bonds"], bonds, "bondId", ("name", "activeCount", "thresholds", "countMode", "desc", "isCore")),
             "equipmentSlotIdsReservedForOldPresets": reused_ids,
             "diySlotCount": summary["diySlots"]}
    if audit["operators"]["current"] != summary["fixedOperatorSlots"]:
        raise ValueError("Fixed operator count differs from the source manifest")
    if audit["equipment"]["current"] != len(season["trapShopChessDatas"]):
        raise ValueError("Equipment count differs from the source manifest")
    if audit["bonds"]["current"] != len(season["bondInfoDict"]):
        raise ValueError("Bond count differs from the source manifest")
    write_json(args.catalog, output)
    write_json(args.report, audit)


if __name__ == "__main__":
    main()
