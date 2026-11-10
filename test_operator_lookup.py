"""Operator reference checks using only the bundled data and offscreen Qt."""

import copy
import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from officer_lookup import OperatorLookup
from operator_reference import load_operator_reference
from planner import Catalog


class OperatorLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.root = Path(__file__).parent
        cls.catalog = Catalog(cls.root / "catalog.json")

    def test_multi_label_operator_matches_combined_filters(self):
        lookup = OperatorLookup(self.catalog, self.root)
        self.addCleanup(lookup.close)
        self.assertEqual(lookup.roster.count(), len(self.catalog.operators))
        lookup.show()
        self.app.processEvents()
        for combo in (lookup.effect_filter, lookup.tier_filter, lookup.trigger_filter):
            self.assertTrue(combo.isVisible())
        self.assertEqual(lookup.effect_filter.x(), lookup.core_filter.x())
        self.assertEqual(lookup.trigger_filter.x(), lookup.other_filter.x())
        self.assertEqual(lookup.effect_filter.y(), lookup.trigger_filter.y())
        self.assertGreater(lookup.effect_filter.y(), lookup.core_filter.y())
        self.assertGreater(lookup.tier_filter.y(), lookup.trigger_filter.y())
        lookup.effect_filter.setCurrentIndex(lookup.effect_filter.findData("战力"))
        lookup.trigger_filter.setCurrentIndex(lookup.trigger_filter.findData("战斗中"))
        lookup.tier_filter.setCurrentIndex(lookup.tier_filter.findData(6))
        lookup.other_filter.setCurrentIndex(lookup.other_filter.findData("arcaneShip"))
        lookup.search.setText("逻各斯")
        self.assertEqual(lookup.roster.count(), 1)
        lookup._open_detail(lookup.roster.item(0))
        self.assertIn("单次叠层 · 战力", lookup.detail.effects.text())
        self.assertIn("获得时 · 战斗中", lookup.detail.triggers.text())
        self.assertIn("购买价", lookup.detail.common_price.text())
        self.assertIn("<获得时>", lookup.detail.normal.text())

        lookup.tier_filter.setCurrentIndex(lookup.tier_filter.findData(5))
        self.assertEqual(lookup.roster.count(), 0)
        self.assertTrue(lookup.detail.isHidden())
        lookup.tier_filter.setCurrentIndex(lookup.tier_filter.findData(6))
        self.assertEqual(lookup.roster.count(), 1)
        lookup.trigger_filter.setCurrentIndex(lookup.trigger_filter.findData("售出时"))
        self.assertEqual(lookup.roster.count(), 0)

    def test_effect_and_tier_options(self):
        lookup = OperatorLookup(self.catalog, self.root)
        self.addCleanup(lookup.close)
        self.assertEqual([lookup.effect_filter.itemText(i) for i in range(lookup.effect_filter.count())],
                         ["全部", "持续叠加", "单次叠层", "辅助叠层", "战力", "资源", "经济"])
        self.assertEqual([lookup.tier_filter.itemData(i) for i in range(lookup.tier_filter.count())],
                         [None, 1, 2, 3, 4, 5, 6])
        for tier in range(1, 7):
            lookup.tier_filter.setCurrentIndex(lookup.tier_filter.findData(tier))
            actual = {lookup.roster.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lookup.roster.count())}
            self.assertEqual(actual, {row["charId"] for row in self.catalog.operators if row["tier"] == tier})

    def test_reference_is_limited_to_the_current_season(self):
        reference = load_operator_reference(self.root, self.catalog)
        self.assertEqual(set(reference), {row["charId"] for row in self.catalog.operators})
        stale = copy.copy(self.catalog)
        stale.season_id = "future-season"
        self.assertEqual(load_operator_reference(self.root, stale), {})
