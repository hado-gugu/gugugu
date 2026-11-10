"""Native editor interactions, using a temporary store and no real user data."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from editor import PresetEditor
from planner import Catalog, PlannerStore


class EditorSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.root = Path(__file__).parent
        cls.catalog = Catalog(cls.root / "catalog.json")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = PlannerStore(Path(self.temporary.name) / "planner.json", self.catalog)
        self.store.import_json((self.root / "samples/web-demo.json").read_text(encoding="utf-8"))
        self.changes = []
        self.editor = PresetEditor(self.store, self.catalog, lambda: self.changes.append(1), self.root)
        self.editor.show()
        self.app.processEvents()
        self.addCleanup(self.editor.deleteLater)

    def test_filter_checkbox_persists_and_notifies(self):
        self.assertEqual(self.editor.roster.count(), 141)
        self.editor.search.setText("野鬃")
        self.app.processEvents()
        self.assertEqual(self.editor.roster.count(), 1)
        item = self.editor.roster.item(0)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.app.processEvents()
        self.assertNotIn("char_496_wildmn", self.store.active()["targets"])
        self.assertEqual(len(self.changes), 1)
        reloaded = PlannerStore(self.store.path, self.catalog)
        self.assertEqual(reloaded.state, self.store.state)

    def test_equipment_filter_and_close_hide(self):
        self.editor.tabs.setCurrentIndex(2)
        self.app.processEvents()
        self.assertEqual(self.editor.roster.count(), 70)
        self.editor.search.setText("盟约之币")
        self.app.processEvents()
        self.assertEqual(self.editor.roster.count(), 1)
        self.assertEqual(self.editor.roster.item(0).checkState(), Qt.CheckState.Checked)
        before = self.store.state
        self.editor.close()
        self.app.processEvents()
        self.assertFalse(self.editor.isVisible())
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.changes, [])

    def test_no_active_preset_keeps_global_transition_editable(self):
        for row in list(self.store.state["presets"]):
            self.store.delete_preset(row["id"])
        self.editor.refresh()
        self.assertFalse(self.editor.copy_button.isEnabled())
        self.editor.tabs.setCurrentIndex(1)
        self.editor.search.setText("野鬃")
        self.app.processEvents()
        item = self.editor.roster.item(0)
        self.assertTrue(item.flags() & Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked)
        self.app.processEvents()
        self.assertEqual(self.store.state["transitions"], ["char_496_wildmn"])


if __name__ == "__main__":
    unittest.main()
