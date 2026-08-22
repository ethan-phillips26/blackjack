"""
Picture geometry persistence.

Two things matter here and neither is the happy path. First, config.py must
remain the default, so deleting the file resets the machine. Second, nothing a
person can do to state/display.json by hand may stop the cabinet booting -- it
is plain JSON sitting in a directory the README invites you to look inside.
"""

import json
import os
import shutil
import tempfile
import unittest

import config
import display_state
from display_state import Geometry


class DisplayStateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bjdisplay")
        self.path = os.path.join(self.tmpdir, "display.json")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def write(self, payload) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))


class TestDefaults(DisplayStateTestCase):
    def test_missing_file_falls_back_to_config(self):
        loaded = display_state.load(self.path)
        self.assertEqual(loaded, Geometry.defaults())
        self.assertEqual(loaded.offset_x, config.IMAGE_OFFSET_X)
        self.assertEqual(loaded.scale, config.IMAGE_SCALE)

    def test_deleting_the_file_resets_to_config(self):
        display_state.save(Geometry(-12, 8, 0.9), self.path)
        os.unlink(self.path)
        self.assertEqual(display_state.load(self.path), Geometry.defaults())

    def test_a_partial_file_keeps_config_for_the_missing_keys(self):
        self.write({"offset_x": -10})
        loaded = display_state.load(self.path)
        self.assertEqual(loaded.offset_x, -10)
        self.assertEqual(loaded.offset_y, config.IMAGE_OFFSET_Y)
        self.assertEqual(loaded.scale, config.IMAGE_SCALE)


class TestRoundTrip(DisplayStateTestCase):
    def test_what_is_saved_is_what_loads(self):
        geometry = Geometry(offset_x=-6, offset_y=4, scale=0.94)
        self.assertTrue(display_state.save(geometry, self.path))
        self.assertEqual(display_state.load(self.path), geometry)

    def test_save_leaves_no_temp_file_behind(self):
        display_state.save(Geometry(0, 0, 1.0), self.path)
        self.assertEqual(os.listdir(self.tmpdir), ["display.json"])

    def test_save_creates_the_directory(self):
        nested = os.path.join(self.tmpdir, "deeper", "display.json")
        self.assertTrue(display_state.save(Geometry(1, 2, 0.8), nested))
        self.assertEqual(display_state.load(nested), Geometry(1, 2, 0.8))


class TestHostileFiles(DisplayStateTestCase):
    """A cabinet must boot whatever is in this file."""

    def test_corrupt_json_falls_back(self):
        self.write("{not json at all")
        self.assertEqual(display_state.load(self.path), Geometry.defaults())

    def test_a_json_list_falls_back(self):
        self.write([1, 2, 3])
        self.assertEqual(display_state.load(self.path), Geometry.defaults())

    def test_an_empty_file_falls_back(self):
        self.write("")
        self.assertEqual(display_state.load(self.path), Geometry.defaults())

    def test_strings_where_numbers_belong_fall_back_per_field(self):
        self.write({"offset_x": "left a bit", "offset_y": 5, "scale": None})
        loaded = display_state.load(self.path)
        self.assertEqual(loaded.offset_x, config.IMAGE_OFFSET_X)
        self.assertEqual(loaded.offset_y, 5)
        self.assertEqual(loaded.scale, config.IMAGE_SCALE)

    def test_a_numeric_string_is_accepted(self):
        self.write({"offset_x": "-8", "offset_y": 0, "scale": "0.9"})
        loaded = display_state.load(self.path)
        self.assertEqual(loaded.offset_x, -8)
        self.assertEqual(loaded.scale, 0.9)

    def test_booleans_are_not_coordinates(self):
        self.write({"offset_x": True, "offset_y": False, "scale": True})
        self.assertEqual(display_state.load(self.path), Geometry.defaults())

    def test_nan_scale_falls_back(self):
        self.write('{"offset_x": 0, "offset_y": 0, "scale": NaN}')
        self.assertEqual(display_state.load(self.path).scale, config.IMAGE_SCALE)

    def test_unknown_keys_are_ignored(self):
        self.write({"offset_x": 3, "offset_y": 3, "scale": 0.9, "wat": "hello"})
        self.assertEqual(display_state.load(self.path), Geometry(3, 3, 0.9))


class TestClamping(DisplayStateTestCase):
    """An out-of-range value would put the picture off the tube, which on a
    cabinet is indistinguishable from a machine that no longer boots."""

    def test_scale_is_clamped_to_the_configured_range(self):
        self.write({"offset_x": 0, "offset_y": 0, "scale": 99.0})
        self.assertEqual(display_state.load(self.path).scale, config.IMAGE_SCALE_MAX)
        self.write({"offset_x": 0, "offset_y": 0, "scale": 0.0})
        self.assertEqual(display_state.load(self.path).scale, config.IMAGE_SCALE_MIN)

    def test_offsets_are_clamped_to_half_the_screen(self):
        self.write({"offset_x": 99999, "offset_y": -99999, "scale": 1.0})
        loaded = display_state.load(self.path)
        self.assertEqual(loaded.offset_x, config.SCREEN_WIDTH // 2)
        self.assertEqual(loaded.offset_y, -(config.SCREEN_HEIGHT // 2))

    def test_a_clamped_value_stays_usable_after_a_round_trip(self):
        self.write({"offset_x": 99999, "offset_y": 0, "scale": 1.0})
        loaded = display_state.load(self.path)
        display_state.save(loaded, self.path)
        self.assertEqual(display_state.load(self.path), loaded)


class TestSaveNeverRaises(DisplayStateTestCase):
    def test_an_unwritable_path_reports_false_rather_than_raising(self):
        # A directory where the file should be: open() fails, and the shutdown
        # path must survive it.
        os.mkdir(self.path)
        self.assertFalse(display_state.save(Geometry(0, 0, 1.0), self.path))


if __name__ == "__main__":
    unittest.main()
