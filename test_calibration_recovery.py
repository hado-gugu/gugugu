"""Recovery must preserve damaged data before any explicit replacement save."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import calibration
from calibration import load_profiles, profile_key, profile_matches, save_profile


class CalibrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "calibration.json"
        self.identity = {"exe": "C:/Example/browser.exe", "class_name": "Browser"}
        self.roi = [.2, .3, .8, .9]

    def test_reading_corrupt_json_is_non_mutating_and_explicit_save_recovers(self):
        original = b'{"version":1,"profiles": not valid json'
        self.path.write_bytes(original)
        with self.assertRaises(ValueError):
            load_profiles(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])
        saved = save_profile(self.path, self.identity, self.roi, [1600, 900])
        self.assertEqual(load_profiles(self.path)["profiles"][profile_key(self.identity)], saved)
        backups = list(self.path.parent.glob("calibration.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertIn("Z-", backups[0].name)

    def test_one_bad_entry_is_rejected_on_load_and_valid_others_survive_recovery(self):
        valid = {"roi": self.roi, "reference_size": [1600, 900]}
        original = json.dumps({"version": 1, "profiles": {"valid-other-window": valid, "invalid-window": "broken"}}).encode()
        self.path.write_bytes(original)
        with self.assertRaises(ValueError):
            load_profiles(self.path)
        save_profile(self.path, self.identity, [.1, .2, .7, .8], [1200, 675])
        profiles = load_profiles(self.path)["profiles"]
        self.assertEqual(profiles["valid-other-window"], valid)
        self.assertNotIn("invalid-window", profiles)
        self.assertIn(profile_key(self.identity), profiles)
        self.assertEqual(next(self.path.parent.glob("calibration.corrupt-*.json")).read_bytes(), original)

    def test_failed_backup_prevents_any_original_replacement(self):
        original = b"damaged original content"
        self.path.write_bytes(original)
        with patch("calibration.os.open", side_effect=PermissionError("backup access denied")):
            with self.assertRaises(PermissionError):
                save_profile(self.path, self.identity, self.roi, [1600, 900])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])

    def test_backup_uses_exclusive_creation_and_failed_commit_keeps_original(self):
        original = b"bad original"
        self.path.write_bytes(original)
        real_open = os.open
        backup_flags = []

        def observed_open(path, flags, *args, **kwargs):
            if ".corrupt-" in str(path):
                backup_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with patch("calibration.os.open", side_effect=observed_open), \
                patch("calibration.os.replace", side_effect=OSError("commit failed")):
            with self.assertRaises(OSError):
                save_profile(self.path, self.identity, self.roi, [1600, 900])
        self.assertTrue(backup_flags and all(flags & os.O_EXCL for flags in backup_flags))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(next(self.path.parent.glob("calibration.corrupt-*.json")).read_bytes(), original)

    def test_invalid_roi_does_not_trigger_recovery_or_touch_original(self):
        self.path.write_bytes(b"bad original")
        with self.assertRaises(ValueError):
            save_profile(self.path, self.identity, [0, 0, .001, .001], [1600, 900])
        self.assertEqual(self.path.read_bytes(), b"bad original")
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])

    def test_malformed_profiles_fail_closed_without_geometry_exceptions(self):
        for profile in (None, "bad", {}, {"roi": self.roi, "reference_size": [True, 900]},
                        {"roi": "bad", "reference_size": [1600, 900]},
                        {"roi": self.roi, "reference_size": [10 ** 1000, 900]},
                        {"roi": self.roi, "reference_size": ["1600", 900]}):
            self.assertFalse(profile_matches(profile, 1600, 900))
        self.assertFalse(profile_matches({"roi": self.roi, "reference_size": [1600, 900]}, float("nan"), 900))


if __name__ == "__main__":
    unittest.main(verbosity=2)
