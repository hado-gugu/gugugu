"""Parity checks for data preservation and the website's reminder semantics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planner import Catalog, PlannerError, PlannerStore, parse_import, validate_state


class PlannerParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "planner.json"
        self.store = PlannerStore(self.path, self.catalog)

    def export_fixture(self, version=2):
        preset = {"id": "incoming", "name": "导入", "targets": ["char_future_unknown"]}
        if version == 2:
            preset["equipment"] = ["chess_item_1_03_e_b", "chess_item_1_03_e_a", "item_future_unknown"]
        return json.dumps({"kind": "stronghold.team-planner", "scope": "current", "version": version,
                           "presets": [preset], "activeId": "incoming", "transitions": ["char_469_indigo"],
                           "enabled": True})

    def test_v1_upgrade_keeps_unknown_operator(self):
        state, scope = parse_import(self.export_fixture(1), self.catalog)
        self.assertEqual(scope, "current")
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["presets"][0]["equipment"], [])
        self.assertEqual(state["presets"][0]["targets"], ["char_future_unknown"])

    def test_import_appends_remaps_ids_and_retains_switch_and_transitions(self):
        self.store.toggle_transition("char_494_vendla")
        self.store.set_enabled(False)
        old = self.store.state
        report = self.store.import_json(self.export_fixture())
        state = self.store.state
        self.assertEqual(len(state["presets"]), 2)
        self.assertEqual(state["presets"][0], old["presets"][0])
        self.assertNotEqual(state["activeId"], "incoming")
        self.assertFalse(state["enabled"])
        self.assertEqual(state["transitions"], ["char_494_vendla"])
        self.assertEqual(self.store.active()["equipment"], ["chess_item_1_03_e_a", "item_future_unknown"])
        self.assertEqual(report["unknownIds"], ["char_future_unknown"])
        self.assertEqual(report["unknownEquipmentIds"], ["item_future_unknown"])

    def test_import_can_explicitly_replace_global_transitions(self):
        self.store.toggle_transition("char_494_vendla")
        self.store.import_json(self.export_fixture(), apply_transitions=True)
        self.assertEqual(self.store.state["transitions"], ["char_469_indigo"])

    def test_target_precedence_and_global_switch(self):
        self.store.toggle_transition("char_496_wildmn")
        self.assertEqual(self.catalog.reminder(self.store.state, "operator", "char_496_wildmn"), "transition")
        self.store.toggle_target("char_496_wildmn")
        self.assertEqual(self.catalog.reminder(self.store.state, "operator", "char_496_wildmn"), "target")
        self.store.set_enabled(False)
        self.assertIsNone(self.catalog.reminder(self.store.state, "operator", "char_496_wildmn"))

    def test_saved_legacy_operator_ids_match_current_ocr_ids(self):
        state = self.store.state
        legacy = "chess_char_1_19_a"
        state["transitions"] = [legacy]
        self.assertEqual(self.catalog.reminder(state, "operator", "char_496_wildmn"), "transition")
        state["presets"][0]["targets"] = [legacy, "chess_char_1_01_a"]
        self.assertEqual(self.catalog.reminder(state, "operator", "char_496_wildmn"), "target")
        self.assertIsNone(self.catalog.reminder(state, "operator", "char_4207_branch"))
        self.assertIsNone(self.catalog.reminder(state, "operator", "char_498_inside"))

    def test_equipment_variants_share_one_selection_and_reminder(self):
        self.store.toggle_equipment("chess_item_1_03_e_b", True)
        self.assertEqual(self.store.active()["equipment"], ["chess_item_1_03_e_a"])
        for alias in ("chess_item_1_03_e_a", "chess_item_1_03_e_b"):
            self.assertEqual(self.catalog.reminder(self.store.state, "equipment", alias), "target")
        self.assertIsNone(self.catalog.reminder(self.store.state, "equipment", "trap_1066_acarm066"))
        self.store.toggle_equipment("chess_item_1_03_e_a")
        self.assertEqual(self.store.active()["equipment"], [])

    def test_failed_atomic_replace_keeps_memory_file_and_no_temporary(self):
        self.store.toggle_target("char_496_wildmn")
        original_state, original_bytes = self.store.state, self.path.read_bytes()
        with patch("planner.os.replace", side_effect=PermissionError("in use")):
            with self.assertRaises(PlannerError):
                self.store.toggle_target("char_494_vendla")
        self.assertEqual(self.store.state, original_state)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_existing_file_is_never_silently_replaced(self):
        self.path.write_bytes(b"{incomplete")
        store = PlannerStore(self.path, self.catalog)
        self.assertTrue(store.load_error)
        with self.assertRaises(PlannerError):
            store.create_preset("new")
        self.assertEqual(self.path.read_bytes(), b"{incomplete")

    def test_strict_validation_rejects_unsafe_or_excessive_input_without_mutation(self):
        original = self.store.state
        invalids = []
        unsafe = self.store.state
        unsafe["presets"][0]["targets"] = ["__proto__"]
        invalids.append(unsafe)
        extra = self.store.state
        extra["presets"][0]["quantities"] = {}
        invalids.append(extra)
        oversized = self.store.state
        oversized["presets"][0]["targets"] = [f"char_{i}" for i in range(21)]
        invalids.append(oversized)
        boolean_version = self.store.state
        boolean_version["version"] = True
        invalids.append(boolean_version)
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlannerError):
                    validate_state(invalid)
        self.assertEqual(self.store.state, original)

    def test_export_current_includes_global_transitions_and_unknown_ids(self):
        self.store.import_json(self.export_fixture(), apply_transitions=True)
        self.store.create_preset("second")
        self.store.toggle_target("char_extra_unknown")
        exported = json.loads(self.store.export_json("current"))
        self.assertEqual(len(exported["presets"]), 1)
        self.assertEqual(exported["presets"][0]["targets"], ["char_extra_unknown"])
        self.assertEqual(exported["transitions"], ["char_469_indigo"])
        self.assertEqual(PlannerStore(self.path, self.catalog).state, self.store.state)

    def test_natural_bonds_deduplicate_and_respect_inactive_and_conditionals(self):
        rows = self.catalog.bond_summary(["char_4207_branch", "char_4207_branch", "char_194_leto", "char_127_estell"])
        by_id = {r["bondId"]: r for r in rows}
        self.assertEqual(by_id["ursusShip"]["count"], 2)
        self.assertFalse(by_id["ursusShip"]["active"])
        self.assertEqual(by_id["soloShip"]["count"], 1)
        off = {r["bondId"]: r for r in self.catalog.bond_summary(["char_127_estell"], ["soloShip"])}
        self.assertTrue(off["soloShip"]["off"])
        self.assertEqual(off["soloShip"]["tier"], 0)
        for row in rows:
            if row["requiresElite"]:
                self.assertFalse(row["active"])

    def test_exact_name_lookup_is_typed_and_does_not_guess_substrings(self):
        self.assertEqual(self.catalog.resolve_name(" 盟约之币 "),
                         [{"kind": "equipment", "id": "chess_item_1_03_e_a", "name": "盟约之币"}])
        self.assertEqual(self.catalog.resolve_name("野 鬃")[0]["id"], "char_496_wildmn")
        self.assertEqual(self.catalog.resolve_name("野鬃：资金不足"), [])
        self.assertEqual(self.catalog.resolve_name("盟约"), [])

    def test_recycled_slot_ids_preserve_legacy_preset_identities(self):
        self.assertEqual(self.catalog.name_of("chess_char_1_01_a"), "隐现")
        self.assertEqual(self.catalog.resolve_name("折桠")[0]["id"], "char_4207_branch")
        retired = "chess_item_3_03_e_a"
        current = self.catalog.resolve_name("炎国短刀")[0]["id"]
        self.assertEqual(self.catalog.name_of(retired, "equipment"), "激光发射器")
        self.assertNotEqual(current, retired)
        self.store.toggle_equipment(retired, True)
        self.store.toggle_target("char_498_inside", True)
        self.assertIn(retired, self.store.active()["equipment"])
        self.assertEqual(self.catalog.resolve_name("激光发射器"), [])
        self.assertEqual(self.catalog.bond_summary(["char_498_inside"]), [])
        self.assertIsNone(self.catalog.reminder(self.store.state, "equipment", retired))
        self.assertIsNone(self.catalog.reminder(self.store.state, "equipment", current))
        self.assertIsNone(self.catalog.reminder(self.store.state, "operator", "char_498_inside"))
        self.store.toggle_equipment(current, True)
        self.assertEqual(self.catalog.reminder(self.store.state, "equipment", current), "target")

    def test_crud_delete_final_preset_preserves_global_transitions(self):
        self.store.toggle_transition("char_469_indigo")
        original = self.store.state["activeId"]
        clone = self.store.duplicate_preset()
        self.store.rename_preset(clone, "副本改名")
        self.store.delete_preset(clone)
        self.assertEqual(self.store.state["activeId"], original)
        self.store.delete_preset(original)
        self.assertIsNone(self.store.active())
        self.assertEqual(self.store.state["transitions"], ["char_469_indigo"])


if __name__ == "__main__":
    unittest.main()
