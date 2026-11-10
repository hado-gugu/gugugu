import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from responsiveness import name_frames_stable, migrate_legacy_data


def frame(image, sequence):
    return SimpleNamespace(image_bgr=image, sequence=sequence, timestamp=float(sequence))


class StabilityTests(unittest.TestCase):
    def test_same_frame_is_not_independent_confirmation(self):
        image = np.zeros((30, 120, 3), dtype=np.uint8)
        self.assertIsNone(name_frames_stable(frame(image, 1), frame(image, 1), [[0, 0, 1, 1]]))

    def test_new_unchanged_pixels_are_stable(self):
        image = np.full((30, 120, 3), 30, dtype=np.uint8)
        self.assertTrue(name_frames_stable(frame(image, 1), frame(image.copy(), 2), [[0, 0, 1, 1]]))

    def test_small_name_change_is_not_diluted_across_whole_row(self):
        a = np.zeros((40, 2000, 3), dtype=np.uint8)
        b = a.copy()
        b[10:15, 1000:1004] = 240
        self.assertFalse(name_frames_stable(frame(a, 1), frame(b, 2), [[0, 0, 1, 1]]))

    def test_changed_long_suffix_outside_old_short_word_is_seen(self):
        a = np.zeros((40, 600, 3), dtype=np.uint8)
        a[10:25, 20:40] = 240
        b = a.copy()
        b[10:25, 90:105] = 240
        self.assertFalse(name_frames_stable(frame(a, 1), frame(b, 2), [[0, 0, 1, 1]]))

    def test_dim_disabled_name_change_is_also_detected(self):
        a = np.full((40, 600, 3), 25, dtype=np.uint8)
        b = a.copy()
        b[10:25, 90:105] = 105
        self.assertFalse(name_frames_stable(frame(a, 1), frame(b, 2), [[0, 0, 1, 1]]))

    def test_portrait_changes_outside_name_band_are_irrelevant(self):
        a = np.zeros((200, 600, 3), dtype=np.uint8)
        b = a.copy()
        b[:100] = 230
        self.assertTrue(name_frames_stable(frame(a, 1), frame(b, 2), [[0, .8, 1, 1]]))

    def test_resize_cannot_be_stable(self):
        self.assertFalse(name_frames_stable(frame(np.zeros((30, 50, 3), np.uint8), 1),
                                           frame(np.zeros((30, 60, 3), np.uint8), 2), [[0, 0, 1, 1]]))


class UpgradeTests(unittest.TestCase):
    def test_upgrade_keeps_old_bytes_and_existing_new_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root/"old", root/"new"
            old.mkdir(); new.mkdir()
            data = json.dumps({"version": 2, "activeId": "my-plan"}).encode()
            (old/"planner.json").write_bytes(data)
            (old/"calibration.json").write_text('{"version":1,"profiles":{}}')
            (new/"planner.json").write_text('{"already":"exists"}')
            report = migrate_legacy_data(new, old)
            self.assertEqual((old/"planner.json").read_bytes(), data)
            self.assertEqual((new/"planner.json").read_text(), '{"already":"exists"}')
            self.assertEqual(report["copied"], ["calibration.json"])

    def test_bad_old_json_is_not_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root/"old"; old.mkdir()
            (old/"planner.json").write_text('{broken')
            report = migrate_legacy_data(root/"new", old)
            self.assertTrue(report["warnings"])
            self.assertFalse((root/"new/planner.json").exists())


if __name__ == "__main__":
    unittest.main()
