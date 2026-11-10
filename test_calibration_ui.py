"""Exercise the real screenshot selection dialog using its own Qt events only."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QWidget

from calibration import load_profiles, open_calibration, profile_key, profile_matches, save_profile


class CalibrationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def run_dialog(self, frame, interaction, existing=None):
        failures = []
        watchdog = QTimer()
        watchdog.setSingleShot(True)

        def timeout():
            failures.append(AssertionError("Selection dialog did not finish."))
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QDialog):
                dialog.reject()

        def act():
            dialog = QApplication.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QDialog)
                selection = next(widget for widget in dialog.findChildren(QWidget) if hasattr(widget, "image_rect"))
                buttons = dialog.findChild(QDialogButtonBox)
                interaction(dialog, selection, buttons)
            except BaseException as error:
                failures.append(error)
                if isinstance(dialog, QDialog):
                    dialog.reject()

        watchdog.timeout.connect(timeout)
        watchdog.start(3000)
        QTimer.singleShot(0, act)
        result = open_calibration(frame, existing)
        watchdog.stop()
        if failures:
            raise failures[0]
        return result

    def test_drag_in_letterboxed_image_saves_client_normalized_roi(self):
        # A 4:1 source produces large top/bottom letterbox areas in the dialog.
        frame = np.full((400, 1600, 3), (30, 70, 110), dtype=np.uint8)
        measured = {}

        def interact(dialog, selection, buttons):
            rect = selection.image_rect()
            self.assertGreater(rect.top(), 10)
            # Clicking a preview margin must not start a selection.
            margin = QPoint(int(rect.center().x()), 1)
            QTest.mousePress(selection, Qt.MouseButton.LeftButton, pos=margin)
            QTest.mouseRelease(selection, Qt.MouseButton.LeftButton, pos=margin)
            self.assertFalse(selection.dragging)
            self.assertIsNone(selection.roi)

            def point(x, y):
                return QPoint(round(rect.left() + x * rect.width()), round(rect.top() + y * rect.height()))

            # Reverse direction is intentional: normalization must reorder edges.
            start, end = point(.8, .9), point(.2, .3)
            QTest.mousePress(selection, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(selection, end)
            QTest.mouseRelease(selection, Qt.MouseButton.LeftButton, pos=end)
            measured["display_width"] = rect.width()
            measured["display_height"] = rect.height()
            QTest.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)

        roi = self.run_dialog(frame, interact)
        self.assertIsNotNone(roi)
        expected = [.2, .3, .8, .9]
        for index, (actual, value) in enumerate(zip(roi, expected)):
            span = measured["display_width" if index % 2 == 0 else "display_height"]
            self.assertAlmostEqual(actual, value, delta=1.5 / span)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calibration.json"
            identity = {"exe": "C:/Example/browser.exe", "class_name": "Browser"}
            saved = save_profile(path, identity, roi, [1600, 400])
            self.assertEqual(load_profiles(path)["profiles"][profile_key(identity)], saved)
            self.assertTrue(profile_matches(saved, 2400, 600))

    def test_cancel_returns_none_and_preserves_existing_roi(self):
        existing = [.2, .3, .8, .9]

        def interact(dialog, selection, buttons):
            self.assertEqual(selection.roi, existing)
            QTest.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)

        self.assertIsNone(self.run_dialog(np.zeros((400, 1600, 3), np.uint8), interact, existing))
        self.assertEqual(existing, [.2, .3, .8, .9])


if __name__ == "__main__":
    unittest.main(verbosity=2)
