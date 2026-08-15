"""
Contract tests for src/config.py (Phase 2 Step 2, config file migration).

Verifies the frozen ``FilterConfig`` dataclass, ``DEFAULT_FILTERS`` as the
single source of truth, and ``from_file()`` loading from the human-editable
``config/filters.json``. ``.env`` / environment variables are not used for
filter configuration. Tests never touch the network.
"""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.config import DEFAULT_FILTERS, FilterConfig


class FilterConfigDefaultsTestCase(unittest.TestCase):
    """Phase 1 defaults and the default filter file."""

    def test_default_filters_match_phase1_defaults(self):
        self.assertEqual(DEFAULT_FILTERS.price_min, 550000)
        self.assertEqual(DEFAULT_FILTERS.price_max, 750000)
        self.assertEqual(DEFAULT_FILTERS.bedrooms_min, 3)
        self.assertEqual(DEFAULT_FILTERS.living_area_min, 100)
        self.assertIsNone(DEFAULT_FILTERS.property_type)
        self.assertIsNone(DEFAULT_FILTERS.plot_size_min)
        self.assertIsNone(DEFAULT_FILTERS.energy_label_min)

    def test_default_path_is_project_root_relative(self):
        expected = Path(__file__).resolve().parent.parent / "config" / "filters.json"
        self.assertEqual(config._FILTERS_PATH, expected)
        self.assertTrue(config._FILTERS_PATH.is_absolute())
        self.assertTrue(config._FILTERS_PATH.exists())

    def test_default_filter_file_loads_phase1_defaults(self):
        self.assertEqual(FilterConfig.from_file(), DEFAULT_FILTERS)


class FilterConfigFileTestCase(unittest.TestCase):
    """File-driven tests for FilterConfig.from_file().

    Each test uses an isolated temporary ``filters.json`` so the real
    committed file and any real .env are never touched.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.filters_path = Path(self._tmp.name) / "filters.json"

    def _write(self, obj):
        self.filters_path.write_text(json.dumps(obj), encoding="utf-8")

    # --- Loading custom values ---

    def test_from_file_custom_values(self):
        self._write({
            "price_min": 400000,
            "price_max": 800000,
            "bedrooms_min": 4,
            "living_area_min": 120,
            "property_type": "appartement",
            "plot_size_min": 50,
            "energy_label_min": "B",
        })

        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.price_min, 400000)
        self.assertEqual(filters.price_max, 800000)
        self.assertEqual(filters.bedrooms_min, 4)
        self.assertEqual(filters.living_area_min, 120)
        self.assertEqual(filters.property_type, "appartement")
        self.assertEqual(filters.plot_size_min, 50)
        self.assertEqual(filters.energy_label_min, "B")

    def test_partial_file_keeps_other_defaults(self):
        self._write({"price_min": 400000})

        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.price_min, 400000)
        self.assertEqual(filters.price_max, 750000)
        self.assertEqual(filters.bedrooms_min, 3)
        self.assertEqual(filters.living_area_min, 100)
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.plot_size_min)
        self.assertIsNone(filters.energy_label_min)

    # --- Missing optional values ---

    def test_missing_optional_values_become_none(self):
        self._write({
            "price_min": 550000,
            "price_max": 750000,
            "bedrooms_min": 3,
            "living_area_min": 100,
        })

        filters = FilterConfig.from_file(self.filters_path)
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.plot_size_min)
        self.assertIsNone(filters.energy_label_min)

    # --- Invalid input rejection ---

    def test_missing_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_invalid_json_raises_value_error(self):
        self.filters_path.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_non_object_json_raises_value_error(self):
        self._write([1, 2, 3])
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_unknown_key_raises_value_error(self):
        self._write({"price_minn": 400000, "price_max": 750000})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_non_integer_values_raise_value_error(self):
        cases = [
            {"price_min": "550000"},
            {"price_min": 550000.5},
            {"price_min": True},
            {"bedrooms_min": "3"},
            {"living_area_min": 100.0},
            {"plot_size_min": 50.5},
        ]
        for values in cases:
            with self.subTest(values=values):
                self._write(values)
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_null_required_value_raises_value_error(self):
        self._write({"price_min": None})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_invalid_price_range_raises_value_error(self):
        self._write({"price_min": 800000, "price_max": 550000})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_negative_numeric_values_raise_value_error(self):
        cases = [
            {"price_min": -1},
            {"bedrooms_min": -2},
            {"living_area_min": -1},
            {"plot_size_min": -5},
        ]
        for values in cases:
            with self.subTest(values=values):
                self._write(values)
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_empty_property_type_raises_value_error(self):
        self._write({"property_type": ""})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_non_string_energy_label_raises_value_error(self):
        self._write({"energy_label_min": 5})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    # --- Normalization ---

    def test_energy_label_min_is_normalized_uppercase(self):
        self._write({"energy_label_min": "a+++"})
        self.assertEqual(FilterConfig.from_file(self.filters_path).energy_label_min, "A+++")

    # --- Environment independence ---

    def test_from_file_ignores_environment(self):
        self._write({})
        os.environ["FUNDA_PRICE_MIN"] = "1"
        os.environ["FUNDA_ENERGY_LABEL_MIN"] = "A"
        try:
            self.assertEqual(FilterConfig.from_file(self.filters_path), DEFAULT_FILTERS)
        finally:
            os.environ.pop("FUNDA_PRICE_MIN", None)
            os.environ.pop("FUNDA_ENERGY_LABEL_MIN", None)

    def test_no_env_based_api_remains(self):
        self.assertFalse(hasattr(config, "from_env"))
        self.assertFalse(hasattr(config, "_load_env"))


class FilterConfigConstructionTestCase(unittest.TestCase):
    """Immutability and direct-construction validation."""

    def test_filterconfig_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_FILTERS.price_min = 1
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_FILTERS.property_type = "huis"

    def test_direct_construction_validates_range_and_negatives(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=50, bedrooms_min=3, living_area_min=100)
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=-1, living_area_min=100)


if __name__ == "__main__":
    unittest.main()
