"""Community tags used by the operator reference and preset editor."""

import json
from pathlib import Path


REFERENCE_PATH = Path("assets") / "operator-reference.json"


def load_operator_reference(resource_root, catalog):
    try:
        payload = json.loads((Path(resource_root) / REFERENCE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or not catalog.season_id or payload.get("seasonId") != catalog.season_id:
        return {}
    records = payload.get("operators")
    if not isinstance(records, dict):
        return {}

    reference = {}
    for row in catalog.operators:
        char_id = row["charId"]
        record = records.get(char_id)
        if not isinstance(record, dict):
            continue
        labels = {}
        for key in ("effectTags", "triggerTimes"):
            values = record.get(key)
            if isinstance(values, list):
                labels[key] = tuple(dict.fromkeys(value.strip() for value in values
                                                  if isinstance(value, str) and value.strip()))
            else:
                labels[key] = ()
        reference[char_id] = labels
    return reference
