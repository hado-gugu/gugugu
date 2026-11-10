"""A refresh's temporarily blank names must not destroy learned shop geometry."""
from unittest.mock import patch
import unittest

from test_fast_scenarios import CropCoverageOracle, SynthesizedShop
from vision import VisionEngine


class EmptyBandTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = VisionEngine(["野鬃", "刺玫", "深靛", "送葬人", "圣约送葬人"], empty_band_grace_seconds=.6)

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.engine.reset_layout()
        self.oracle = CropCoverageOracle()
        self.sequence = 0
        self.patcher = patch.object(self.engine, "_ocr_lines", side_effect=self.oracle)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def observe(self, scene, at):
        self.sequence += 1
        self.oracle.scene = scene
        with patch("vision.time.monotonic", return_value=at):
            result = self.engine.analyze(scene.image, scene.roi, sequence=self.sequence)
        self.assertNotEqual(result["status"], "error", result.get("error"))
        return result

    @staticmethod
    def names(result):
        return [line["text"] for line in result["candidates"]]

    def initial(self):
        return self.observe(SynthesizedShop(["野鬃", "刺玫", "深靛"]), 100.0)

    def test_first_blank_frame_clears_names_but_keeps_bands_without_full_scan(self):
        self.initial()
        previous_bands = self.engine._layout["bands"]
        previous_signatures = self.engine._layout["signatures"]
        calls = len(self.oracle.calls)
        empty = self.observe(SynthesizedShop([None, None, None]), 100.1)
        self.assertEqual(self.names(empty), [])
        self.assertEqual(empty["diagnostics"]["path"], "name_band_detection")
        self.assertIsNone(empty["diagnostics"]["full_refresh_reason"])
        self.assertEqual(len(self.oracle.calls), calls + 1)
        self.assertIs(self.engine._layout["bands"], previous_bands)
        self.assertIs(self.engine._layout["signatures"], previous_signatures)
        self.assertEqual(self.engine._layout["raw_lines"], [])

    def test_blank_then_same_old_names_reappear_requires_new_ocr_not_empty_cache(self):
        original = SynthesizedShop(["野鬃", "刺玫", "深靛"])
        self.observe(original, 100.0)
        self.observe(SynthesizedShop([None, None, None]), 100.1)
        calls = len(self.oracle.calls)
        restored = self.observe(original, 100.3)
        self.assertEqual(self.names(restored), ["野鬃", "刺玫", "深靛"])
        self.assertGreater(len(self.oracle.calls), calls)
        self.assertFalse(restored["ocr_reused"])
        self.assertEqual(restored["diagnostics"]["path"], "name_band_detection")
        self.assertIsNone(self.engine._layout["empty_since"])

    def test_static_empty_cache_still_expires_into_full_relocalization(self):
        self.initial()
        empty = SynthesizedShop([None, None, None])
        self.observe(empty, 100.1)
        calls = len(self.oracle.calls)
        cached = self.observe(empty, 100.5)
        self.assertTrue(cached["ocr_reused"])
        self.assertEqual(len(self.oracle.calls), calls)
        confirmed = self.observe(empty, 100.71)
        self.assertEqual(confirmed["diagnostics"]["path"], "full_shop_detection")
        self.assertEqual(confirmed["diagnostics"]["full_refresh_reason"], "empty_band_timeout")
        self.assertEqual(self.names(confirmed), [])
        self.assertIsNotNone(self.engine._layout)
        self.assertEqual(self.engine._layout["raw_lines"], [])

    def test_slow_machine_restored_names_after_grace_still_try_fast_row_first(self):
        original = SynthesizedShop(["野鬃", "刺玫", "深靛"])
        self.observe(original, 100.0)
        self.observe(SynthesizedShop([None, None, None]), 100.1)
        calls = len(self.oracle.calls)
        restored = self.observe(original, 101.5)
        self.assertEqual(self.names(restored), ["野鬃", "刺玫", "深靛"])
        self.assertEqual(restored["diagnostics"]["path"], "name_band_detection")
        self.assertIsNone(restored["diagnostics"]["full_refresh_reason"])
        self.assertEqual(len(self.oracle.calls), calls + 1)
        self.assertIsNone(self.engine._layout["empty_since"])

    def test_changed_but_still_empty_row_after_grace_does_full_confirmation(self):
        self.initial()
        self.observe(SynthesizedShop([None, None, None]), 100.1)
        calls = len(self.oracle.calls)
        changed_empty = SynthesizedShop([None, None, None], frozen=True)
        checked = self.observe(changed_empty, 101.5)
        self.assertEqual(self.names(checked), [])
        self.assertEqual(checked["diagnostics"]["full_refresh_reason"], "empty_band_timeout")
        self.assertEqual(len(self.oracle.calls), calls + 2, "Try the changed row before falling back to the full ROI")

    def test_reflow_is_found_after_the_bounded_blank_grace(self):
        self.initial()
        reflow = SynthesizedShop(["野鬃", "刺玫", "深靛"], y_shift=-70)
        first = self.observe(reflow, 100.1)
        self.assertEqual(self.names(first), [])
        found = self.observe(reflow, 100.71)
        self.assertEqual(self.names(found), ["野鬃", "刺玫", "深靛"])
        self.assertEqual(found["diagnostics"]["full_refresh_reason"], "empty_band_timeout")
        self.assertLess(found["candidates"][0]["box_norm"][1], .75)

    def test_one_remaining_name_survives_periodic_full_verification_without_losing_layout(self):
        self.initial()
        one = SynthesizedShop(["野鬃", None, None])
        changed_one = self.observe(one, 111.0)
        self.assertEqual(changed_one["diagnostics"]["path"], "name_band_detection")
        self.assertEqual(self.names(changed_one), ["野鬃"])
        periodic = self.observe(one, 112.1)
        self.assertEqual(periodic["diagnostics"]["full_refresh_reason"], "periodic_verify")
        self.assertEqual(self.names(periodic), ["野鬃"])
        self.assertIsNotNone(self.engine._layout)
        self.assertIsNone(self.engine._layout["empty_since"])
        changed = self.observe(SynthesizedShop(["送葬人", None, None]), 112.3)
        self.assertEqual(self.names(changed), ["送葬人"])
        self.assertEqual(changed["diagnostics"]["path"], "name_band_detection")

    def test_overdue_periodic_scan_yields_to_new_names_then_runs_when_idle(self):
        self.initial()
        refreshed = SynthesizedShop(["送葬人", "刺玫", "深靛"])
        first = self.observe(refreshed, 111.0)
        self.assertEqual(self.names(first), ["送葬人", "刺玫", "深靛"])
        self.assertEqual(first["diagnostics"]["path"], "name_band_detection")
        self.assertTrue(first["diagnostics"]["periodic_pending"])
        self.assertIsNone(first["diagnostics"]["full_refresh_reason"])
        cached = self.observe(refreshed, 111.5)
        self.assertTrue(cached["ocr_reused"])
        maintenance = self.observe(refreshed, 112.1)
        self.assertEqual(maintenance["diagnostics"]["full_refresh_reason"], "periodic_verify")

    def test_overdue_periodic_scan_does_not_take_restoration_after_empty(self):
        original = SynthesizedShop(["野鬃", "刺玫", "深靛"])
        self.observe(original, 100.0)
        self.observe(SynthesizedShop([None, None, None]), 100.1)
        restored = self.observe(original, 111.5)
        self.assertEqual(self.names(restored), ["野鬃", "刺玫", "深靛"])
        self.assertEqual(restored["diagnostics"]["path"], "name_band_detection")
        self.assertIsNone(restored["diagnostics"]["full_refresh_reason"])
        self.assertIsNone(self.engine._layout["empty_since"])
        maintenance = self.observe(original, 112.7)
        self.assertEqual(maintenance["diagnostics"]["full_refresh_reason"], "periodic_verify")


if __name__ == "__main__":
    unittest.main(verbosity=2)
