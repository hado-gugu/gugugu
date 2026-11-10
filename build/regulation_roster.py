"""Build the assistant's roster and bonds from decoded regulation tables."""

from copy import deepcopy
import re


_STYLE_TAG = re.compile(r"<(?:[@$#][^>]*|color=[^>]*|/color|/)>", re.IGNORECASE)
_COUNT_MODES = {0: "BOARD", 1: "BOARD_AND_DECK", 2: "DECK", 3: "BOARD_ALL_CHESS"}
_PHASE_TYPES = {0: "NONE", 1: "PEOPLE_COUNT", 2: "STACK_COUNT"}


def _description(value):
    return _STYLE_TAG.sub("", (value or "").replace("\\n", "\n")).strip()


def _legacy_chess_ids(old):
    return list(dict.fromkeys([*(old.get("legacyChessIds") or []), *(old.get("chessIds") or [])]))


def _identity(char_id, template_id, characters, patch_chars, professions, subclasses):
    character = characters[char_id]
    form = patch_chars[template_id] if template_id else character
    profession = professions[str(form["profession"])]
    subclass_id = form["subProfessionId"]
    return {
        "charId": char_id,
        "tmplId": template_id,
        "name": character["name"],
        "appellation": character.get("appellation") or "",
        "rarity": form["rarity"] + 1,
        "profession": profession["code"],
        "professionName": profession["name"],
        "subclassId": subclass_id,
        "subclassName": subclasses[subclass_id]["subProfessionName"],
    }


def _chess_form(chess_id, is_golden, tier, season):
    chess = season["charChessDataDict"][chess_id]
    if chess["isGolden"] is not is_golden:
        raise ValueError(f"Unexpected ordinary/elite form for {chess_id}")
    prices = season["shopCharChessInfoData"][str(tier)]
    price = next(row for row in prices if row["isGolden"] is is_golden)
    garrison_ids = list(chess.get("garrisonIds") or [])
    garrisons = season["garrisonDataDict"]
    desc = _description(chess["desc"])
    base_price = price["purchasePrice"]
    purchase_price = base_price
    fixed_price = re.fullmatch(r"购买价格为(\d+)", desc)
    if fixed_price:
        discounts = [entry["value"] for trait_id in garrison_ids
                     if garrisons[trait_id]["effectType"] == "SERVER_CHESS_PRICE"
                     for entry in garrisons[trait_id]["blackboard"] if entry["key"] == "price"]
        purchase_price = int(fixed_price[1])
        if len(discounts) != 1 or base_price - discounts[0] != purchase_price:
            raise ValueError(f"Conflicting fixed purchase price for {chess_id}")
    return {
        "chessId": chess_id,
        "isGolden": is_golden,
        "bondIds": list(chess.get("bondIds") or []),
        "garrisonIds": garrison_ids,
        "desc": desc,
        "traits": [{"id": trait_id, "desc": _description(garrisons[trait_id]["garrisonDesc"]),
                    "eventType": garrisons[trait_id]["eventType"], "effectType": garrisons[trait_id]["effectType"]}
                   for trait_id in garrison_ids],
        "basePurchasePrice": base_price,
        "purchasePrice": purchase_price,
        "chessSoldPrice": price["chessSoldPrice"],
        "status": deepcopy(chess["status"]),
        "upgradeChessId": chess["upgradeChessId"],
        "upgradeNum": chess["upgradeNum"],
        "equipSlotNum": chess["equipSlotNum"],
    }


def build_operators(season, characters, professions, subclasses, old_operators, patches):
    """Return current fixed operators plus retired identities for saved presets."""
    old_by_char = {row["charId"]: row for row in old_operators}
    patch_chars = patches["patchChars"]
    active_chars = set()
    operators = []
    shops = sorted(season["charShopChessDatas"].values(),
                   key=lambda row: (row["chessLevel"], row["shopLevelSortId"], row["chessId"]))
    for shop in shops:
        normal_id = shop["chessId"]
        char_id = shop["charId"]
        if shop["chessType"] == 1:
            if char_id is not None or normal_id not in season["diyChessDict"]:
                raise ValueError(f"Unexpected self-selected slot {normal_id}")
            continue
        if shop["chessType"] not in (0, 2) or not char_id or char_id in active_chars:
            raise ValueError(f"Invalid fixed operator {normal_id}: {char_id}")
        active_chars.add(char_id)

        old = old_by_char.get(char_id, {})
        row = deepcopy(old)
        identity = _identity(char_id, shop["tmplId"], characters, patch_chars, professions, subclasses)
        tier = shop["chessLevel"]
        elite_id = shop["goldenChessId"]
        normal = _chess_form(normal_id, False, tier, season)
        elite = _chess_form(elite_id, True, tier, season)
        backup_id, backup_template = shop["backupCharId"], shop["backupTmplId"]
        prototype = None
        if backup_id and (backup_id, backup_template) != (char_id, shop["tmplId"]):
            prototype = _identity(backup_id, backup_template, characters, patch_chars, professions, subclasses)
            prototype.update(skillIndex=shop["backupCharSkillIndex"], uniEquipId=shop["backupCharUniEquipId"],
                             potentialRank=shop["backupCharPotRank"])

        row.update(identity)
        row.update(id=old.get("id", char_id), tier=tier, bonds=normal["bondIds"][:],
                   chessIds=[normal_id, elite_id], legacyChessIds=_legacy_chess_ids(old),
                   hidden=shop["isHidden"], retired=False, source=old.get("source", "preset"),
                   noNaturalBonds=not normal["bondIds"], avatar=f"assets/char/avatar/{char_id}.png",
                   chessType=shop["chessType"], defaultSkillIndex=shop["defaultSkillIndex"],
                   defaultUniEquipId=shop["defaultUniEquipId"], backupCharId=backup_id,
                   backupTmplId=backup_template, prototype=prototype, forms={"normal": normal, "elite": elite})
        operators.append(row)

    for old in old_operators:
        if old["charId"] in active_chars:
            continue
        row = deepcopy(old)
        # An old slot ID can name a different operator in this season. Retired
        # rows retain that ID only as history, never as a current catalogue alias.
        row.update(retired=True, hidden=True, bonds=[], noNaturalBonds=True,
                   legacyChessIds=_legacy_chess_ids(old), chessIds=[])
        operators.append(row)
    return operators


def build_bonds(season, common, old_bonds):
    """Return season bonds, separating activation counts from stack milestones."""
    old_by_id = {row["bondId"]: row for row in old_bonds}
    bonds = []
    for bond_id, bond in season["bondInfoDict"].items():
        metadata = common["bondInfoDict"][bond_id]
        template = bond["activeConditionTemplate"]
        active_count = bond["activeCount"]
        raw_params = list(bond["activeParamList"])
        params = [int(value) for value in raw_params]
        if not params or params[0] != active_count:
            raise ValueError(f"Activation count disagrees with its template: {bond_id}")
        max_count = None
        if template == "count_threshold_downward" and len(params) == 2 and params[1] > active_count:
            max_count = params[1] - 1
        elif template not in ("count_threshold_upward", "count_threshold_upward_golden") or len(params) != 1:
            raise ValueError(f"Unsupported activation template for {bond_id}: {template}")

        phase = season["effectDescParamDataDict"][bond["effectId"]]
        phase_type = _PHASE_TYPES[phase["phaseType"]]
        phase_counts = list(phase["bondPhaseCountList"] or [])
        # The FlatBuffers enum explicitly distinguishes people count from stack
        # count. Only the former is a second/third team-size threshold.
        extra_people = sorted({count for count in phase_counts if count > active_count}) if phase_type == "PEOPLE_COUNT" else []
        old = old_by_id.get(bond_id, {})
        row = deepcopy(old)
        row.update(bondId=bond_id, name=bond["name"], desc=_description(bond["desc"]),
                   activeCount=active_count, countMode=_COUNT_MODES[bond["activeCondition"]],
                   thresholdTemplate=template, maxCount=max_count, thresholds=[active_count, *extra_people],
                   extraPeopleThresholds=extra_people, activeParamList=raw_params,
                   effectPhaseType=phase_type, effectPhaseCounts=phase_counts,
                   isCore=metadata["isPower"], isPower=metadata["isPower"], bondType=metadata["bondType"],
                   bondOrder=metadata["bondOrder"], icon=old.get("icon") or f"assets/bond/{bond_id}.png",
                   iconId=bond["iconId"], effectId=bond["effectId"], activeType=bond["activeType"],
                   noStack=bond["noStack"], noSelect=bond["noSelect"],
                   chessIdList=list(bond["chessIdList"] or []))
        bonds.append(row)
    return bonds
