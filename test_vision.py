"""Safety/coordinate checks without touching any user window or game process."""

import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from vision import CapturedFrame, CaptureUnavailable, WindowCapture, _rect_pixels, map_capture_to_client, normalize_name


class ClientPixelMappingTests(unittest.TestCase):
    def setUp(self):
        # Negative desktop coordinates model a monitor left of the primary one.
        self.geometry = {
            "pid": 7,
            "client_screen_rect": (-96, 25, -90, 29),
            "frame_rect": (-98, 21, -88, 31),
            "window_rect": (-100, 20, -86, 32),
        }

    def test_frame_bounds_are_cropped_using_screen_offset(self):
        image = np.arange(10 * 10 * 3, dtype=np.uint8).reshape(10, 10, 3)
        client = map_capture_to_client(image, self.geometry)
        np.testing.assert_array_equal(client, image[4:8, 2:8])

    def test_full_window_padding_is_not_treated_as_client_pixels(self):
        image = np.arange(12 * 14 * 3, dtype=np.uint8).reshape(12, 14, 3)
        client = map_capture_to_client(image, self.geometry)
        np.testing.assert_array_equal(client, image[5:9, 4:10])

    def test_already_client_sized_buffer_remains_unchanged(self):
        image = np.full((4, 6, 3), 19, np.uint8)
        np.testing.assert_array_equal(map_capture_to_client(image, self.geometry), image)

    def test_unknown_size_is_rejected_instead_of_stretched(self):
        with self.assertRaises(CaptureUnavailable) as error:
            map_capture_to_client(np.zeros((9, 9, 3), np.uint8), self.geometry)
        self.assertEqual(error.exception.code, "geometry_mismatch")

    def test_region_is_ltrb_and_includes_boundary_pixels(self):
        self.assertEqual(_rect_pixels([0.25, 0.2, 0.75, 0.8], 100, 50), (25, 10, 75, 40))
        for rect in ([0.5, 0, 0.4, 1], [-0.1, 0, 1, 1], [0, 0, float("nan"), 1]):
            with self.assertRaises(ValueError):
                _rect_pixels(rect, 100, 50)


class CaptureFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.capture = WindowCapture(123)
        self.geometry = {"pid": 7, "client_screen_rect": (10, 20, 16, 24)}
        self.capture._running = True
        self.capture._frame = CapturedFrame(np.zeros((4, 6, 3), np.uint8), 5, time.monotonic(), self.geometry)

    def test_stale_frame_never_returns_as_current(self):
        self.capture._frame = CapturedFrame(np.zeros((4, 6, 3), np.uint8), 5, time.monotonic() - 10, self.geometry)
        with patch("vision._geometry", return_value=self.geometry), self.assertRaises(CaptureUnavailable) as error:
            self.capture.latest_frame(max_age_ms=2000)
        self.assertEqual(error.exception.code, "frame_stale")

    def test_resize_rejects_previous_buffer(self):
        changed = {**self.geometry, "client_screen_rect": (10, 20, 17, 24)}
        with patch("vision._geometry", return_value=changed), self.assertRaises(CaptureUnavailable) as error:
            self.capture.latest_frame()
        self.assertEqual(error.exception.code, "window_resizing")

    def test_move_updates_geometry_without_forging_capture_timestamp(self):
        changed = {**self.geometry, "client_screen_rect": (60, 20, 66, 24)}
        with patch("vision._geometry", return_value=changed):
            result = self.capture.latest_frame()
        self.assertEqual(result.geometry["client_screen_rect"], changed["client_screen_rect"])
        self.assertEqual(result.timestamp, self.capture._frame.timestamp)
        self.assertEqual(result.sequence, 5)

    def test_pid_replacement_is_rejected(self):
        self.capture._expected_pid = 7
        with patch("vision._geometry", return_value={**self.geometry, "pid": 8}), self.assertRaises(CaptureUnavailable) as error:
            self.capture.latest_frame()
        self.assertEqual(error.exception.code, "window_replaced")

    def test_start_avoids_newer_windows_native_interval_option(self):
        observed = {}

        class FakeCapture:
            def __init__(self, **kwargs):
                observed.update(kwargs)

            def event(self, function):
                return function

            def start_free_threaded(self):
                return SimpleNamespace(stop=lambda: None)

        with patch("vision._geometry", return_value=self.geometry), \
                patch("native.window_identity", return_value={"hwnd": 123, "pid": 7}), \
                patch.dict("sys.modules", {"windows_capture": SimpleNamespace(WindowsCapture=FakeCapture)}):
            self.capture._running = False
            self.capture.start()
            self.capture.stop()
        self.assertEqual(observed["window_hwnd"], 123)
        self.assertIsNone(observed["minimum_update_interval"])


class NameMatchingTests(unittest.TestCase):
    def test_only_harmless_unicode_spacing_and_case_are_normalized(self):
        self.assertEqual(normalize_name(" 野\u200b鬃\n"), normalize_name("野鬃"))
        self.assertEqual(normalize_name("Ｕ-Official"), normalize_name("U-Official"))
        self.assertNotEqual(normalize_name("送葬人"), normalize_name("圣约送葬人"))
        self.assertNotEqual(normalize_name("深靛"), normalize_name("深蓝"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
