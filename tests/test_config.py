"""
Contract tests for src/config.py (Phase 2 Step 2).

Verifies the frozen ``FilterConfig`` dataclass, ``DEFAULT_FILTERS`` as the
single source of truth, and ``from_env()`` environment loading following the
project's python-dotenv convention. Tests are isolated from any real .env and
never touch the network.
"""

import os
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.config import DEFAULT_FILTERS, FilterConfig


class FilterConfigTestCase(unittest.TestCase):
    """Environment-driven tests for FilterConfig / from_env().

    Each test starts from a clean process environment so no real .env or
    shell variables can leak in, and _load_env() is stubbed out so the
    project's .env file is never read.
    """

    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        load_patcher = mock.patch.object(config, "_load_env")
        load_patcher.start()
        self.addCleanup(load_patcher.stop)

    # --- Defaults / single source of truth ---

    def test_default_filters_match_phase1_defaults(self):
        self.assertEqual(DEFAULT_FILTERS.price_min, 550000)
        self.assertEqual(DEFAULT_FILTERS.price_max, 750000)
        self.assertEqual(DEFAULT_FILTERS.bedrooms_min, 3)
        self.assertEqual(DEFAULT_FILTERS.living_area_min, 100)
        self.assertIsNone(DEFAULT_FILTERS.property_type)
        self.assertIsNone(DEFAULT_FILTERS.plot_size_min)
        self.assertIsNone(DEFAULT_FILTERS.energy_label_min)

    def test_from_env_without_variables_returns_exact_phase1_defaults(self):
        self.assertEqual(FilterConfig.from_env(), DEFAULT_FILTERS)

    # --- Environment overrides ---

    def test_from_env_overrides_values(self):
        os.environ["FUNDA_PRICE_MIN"] = "400000"
        os.environ["FUNDA_PRICE_MAX"] = "800000"
        os.environ["FUNDA_BEDROOMS_MIN"] = "4"
        os.environ["FUNDA_LIVING_AREA_MIN"] = "120"
        os.environ["FUNDA_PROPERTY_TYPE"] = "appartement"
        os.environ["FUNDA_PLOT_SIZE_MIN"] = "50"
        os.environ["FUNDA_ENERGY_LABEL_MIN"] = "B"

        filters = FilterConfig.from_env()
        self.assertEqual(filters.price_min, 400000)
        self.assertEqual(filters.price_max, 800000)
        self.assertEqual(filters.bedrooms_min, 4)
        self.assertEqual(filters.living_area_min, 120)
        self.assertEqual(filters.property_type, "appartement")
        self.assertEqual(filters.plot_size_min, 50)
        self.assertEqual(filters.energy_label_min, "B")

    def test_partial_override_keeps_other_defaults(self):
        os.environ["FUNDA_PRICE_MIN"] = "400000"

        filters = FilterConfig.from_env()
        self.assertEqual(filters.price_min, 400000)
        self.assertEqual(filters.price_max, 750000)
        self.assertEqual(filters.bedrooms_min, 3)
        self.assertEqual(filters.living_area_min, 100)
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.plot_size_min)
        self.assertIsNone(filters.energy_label_min)

    def test_from_env_empty_required_variable_falls_back_to_default(self):
        os.environ["FUNDA_PRICE_MIN"] = ""
        self.assertEqual(FilterConfig.from_env().price_min, 550000)

    # --- Optional values ---

    def test_missing_optional_values_become_none(self):
        filters = FilterConfig.from_env()
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.plot_size_min)
        self.assertIsNone(filters.energy_label_min)

    def test_empty_optional_values_become_none(self):
        os.environ["FUNDA_PROPERTY_TYPE"] = ""
        os.environ["FUNDA_PLOT_SIZE_MIN"] = ""
        os.environ["FUNDA_ENERGY_LABEL_MIN"] = ""

        filters = FilterConfig.from_env()
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.plot_size_min)
        self.assertIsNone(filters.energy_label_min)
        self.assertEqual(filters.price_min, 550000)

    # --- Integer parsing ---

    def test_integer_parsing_with_whitespace(self):
        os.environ["FUNDA_PRICE_MAX"] = "  800000 "
        self.assertEqual(FilterConfig.from_env().price_max, 800000)

    def test_invalid_integer_raises_value_error(self):
        os.environ["FUNDA_PRICE_MIN"] = "abc"
        with self.assertRaises(ValueError):
            FilterConfig.from_env()

    def test_invalid_decimal_raises_value_error(self):
        os.environ["FUNDA_PRICE_MIN"] = "550000.5"
        with self.assertRaises(ValueError):
            FilterConfig.from_env()

    def test_invalid_optional_integer_raises_value_error(self):
        os.environ["FUNDA_PLOT_SIZE_MIN"] = "large"
        with self.assertRaises(ValueError):
            FilterConfig.from_env()

    # --- Range / negative validation ---

    def test_invalid_price_range_raises_value_error(self):
        os.environ["FUNDA_PRICE_MIN"] = "800000"
        os.environ["FUNDA_PRICE_MAX"] = "550000"
        with self.assertRaises(ValueError):
            FilterConfig.from_env()

    def test_negative_numeric_values_raise_value_error(self):
        cases = [
            ("FUNDA_PRICE_MIN", "-1"),
            ("FUNDA_BEDROOMS_MIN", "-2"),
            ("FUNDA_LIVING_AREA_MIN", "-1"),
            ("FUNDA_PLOT_SIZE_MIN", "-5"),
        ]
        for var, value in cases:
            with self.subTest(var=var):
                os.environ[var] = value
                with self.assertRaises(ValueError):
                    FilterConfig.from_env()

    # --- Immutability ---

    def test_filterconfig_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_FILTERS.price_min = 1
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_FILTERS.property_type = "huis"

    # --- Normalization ---

    def test_energy_label_min_is_normalized_uppercase(self):
        os.environ["FUNDA_ENERGY_LABEL_MIN"] = "a+++"
        self.assertEqual(FilterConfig.from_env().energy_label_min, "A+++")

    # --- Direct construction validation ---

    def test_direct_construction_validates_range_and_negatives(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=50, bedrooms_min=3, living_area_min=100)
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=-1, living_area_min=100)


if __name__ == "__main__":
    unittest.main()
