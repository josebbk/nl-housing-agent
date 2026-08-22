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
from src.config import (
    DEFAULT_FILTERS,
    DEFAULT_RETENTION,
    FilterConfig,
    RetentionConfig,
)


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
        self.assertIsNone(DEFAULT_FILTERS.transaction_type)
        self.assertIsNone(DEFAULT_FILTERS.bedrooms_max)
        self.assertIsNone(DEFAULT_FILTERS.living_area_max)
        self.assertIsNone(DEFAULT_FILTERS.rooms_min)
        self.assertIsNone(DEFAULT_FILTERS.rooms_max)
        self.assertIsNone(DEFAULT_FILTERS.plot_size_max)
        self.assertIsNone(DEFAULT_FILTERS.energy_label_max)
        self.assertIsNone(DEFAULT_FILTERS.radius_km)
        self.assertIsNone(DEFAULT_FILTERS.construction_type)

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
        self.assertIsNone(filters.transaction_type)

    def test_missing_new_optional_fields_become_none(self):
        self._write({"price_min": 550000})
        filters = FilterConfig.from_file(self.filters_path)
        for attr in (
            "bedrooms_max", "living_area_max", "rooms_min", "rooms_max",
            "plot_size_max", "energy_label_max",
        ):
            self.assertIsNone(getattr(filters, attr))

    def test_custom_optional_max_bounds_load(self):
        self._write({
            "bedrooms_max": 5,
            "living_area_max": 160,
            "rooms_min": 4,
            "rooms_max": 8,
            "plot_size_max": 300,
            "energy_label_max": "C",
        })
        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.bedrooms_max, 5)
        self.assertEqual(filters.living_area_max, 160)
        self.assertEqual(filters.rooms_min, 4)
        self.assertEqual(filters.rooms_max, 8)
        self.assertEqual(filters.plot_size_max, 300)
        self.assertEqual(filters.energy_label_max, "C")

    def test_energy_label_max_normalized_uppercase(self):
        self._write({"energy_label_max": "a++"})
        self.assertEqual(
            FilterConfig.from_file(self.filters_path).energy_label_max, "A++"
        )

    def test_invalid_optional_max_type_raises(self):
        for key, value in [
            ("bedrooms_max", "5"),
            ("living_area_max", 160.5),
            ("rooms_min", True),
            ("rooms_max", "many"),
            ("plot_size_max", -1),
            ("energy_label_max", 5),
        ]:
            with self.subTest(key=key, value=value):
                self._write({key: value})
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_min_exceeds_max_raises(self):
        cases = [
            {"bedrooms_min": 5, "bedrooms_max": 3},
            {"living_area_min": 200, "living_area_max": 100},
            {"rooms_min": 8, "rooms_max": 4},
            {"plot_size_min": 500, "plot_size_max": 100},
        ]
        for values in cases:
            with self.subTest(values=values):
                self._write(values)
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    # --- radius_km and construction_type ---

    def test_custom_radius_and_construction_type_load(self):
        self._write({"radius_km": 10, "construction_type": "new"})
        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.radius_km, 10)
        self.assertEqual(filters.construction_type, "new")

    def test_construction_type_normalized_lowercase(self):
        self._write({"construction_type": "EXISTING"})
        self.assertEqual(
            FilterConfig.from_file(self.filters_path).construction_type, "existing"
        )

    def test_null_radius_and_construction_allowed(self):
        self._write({"radius_km": None, "construction_type": None})
        filters = FilterConfig.from_file(self.filters_path)
        self.assertIsNone(filters.radius_km)
        self.assertIsNone(filters.construction_type)

    def test_missing_radius_and_construction_become_none(self):
        self._write({"price_min": 550000})
        filters = FilterConfig.from_file(self.filters_path)
        self.assertIsNone(filters.radius_km)
        self.assertIsNone(filters.construction_type)

    def test_invalid_radius_raises(self):
        for value in ["5", 0, -5, 5.5, True]:
            with self.subTest(value=value):
                self._write({"radius_km": value})
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_invalid_construction_type_raises(self):
        for value in ["renovated", "", 5, "NIEUWBOUW"]:
            with self.subTest(value=value):
                self._write({"construction_type": value})
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    # --- transaction_type ---

    def test_custom_transaction_type_loads(self):
        self._write({"transaction_type": "huur"})
        self.assertEqual(
            FilterConfig.from_file(self.filters_path).transaction_type, "huur"
        )

    def test_transaction_type_normalized_lowercase(self):
        self._write({"transaction_type": "KOOP"})
        self.assertEqual(
            FilterConfig.from_file(self.filters_path).transaction_type, "koop"
        )

    def test_null_transaction_type_is_allowed(self):
        self._write({"transaction_type": None})
        self.assertIsNone(FilterConfig.from_file(self.filters_path).transaction_type)

    def test_invalid_transaction_type_raises_value_error(self):
        for value in ["rent", "koopx", "", 5, "VERKOCHT", True]:
            with self.subTest(value=value):
                self._write({"transaction_type": value})
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_publication_date_is_not_a_filter_key(self):
        # publication-date behavior must remain untouched and must NOT be a
        # configurable filter.
        self.assertNotIn("publication_date", config._FILTER_KEYS)
        self.assertIn("transaction_type", config._FILTER_KEYS)

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


class FilterConfigSectionedFileTestCase(unittest.TestCase):
    """Tests for the human-friendly sectioned filters.json layout.

    The committed file separates the four required Phase 1 criteria (under
    ``"required"``) from the optional preference keys (under ``"optional"``,
    where ``null`` means "no restriction"). The legacy flat layout remains
    accepted. Each test uses an isolated temporary ``filters.json`` except
    where the real committed file is inspected deliberately.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.filters_path = Path(self._tmp.name) / "filters.json"

    def _write(self, obj):
        self.filters_path.write_text(json.dumps(obj), encoding="utf-8")

    def _sectioned_defaults(self):
        return {
            "required": {
                "price_min": 550000,
                "price_max": 750000,
                "bedrooms_min": 3,
                "living_area_min": 100,
            },
            "optional": {
                key: None
                for key in (
                    "bedrooms_max", "living_area_max", "rooms_min",
                    "rooms_max", "plot_size_min", "plot_size_max",
                    "property_type", "energy_label_min", "energy_label_max",
                    "transaction_type", "radius_km", "construction_type",
                )
            },
        }

    # --- The committed file ---

    def test_committed_file_uses_sectioned_layout(self):
        raw = json.loads(config._FILTERS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(raw), ["optional", "required"])
        self.assertEqual(set(raw["required"]), set(config._REQUIRED_FILTER_KEYS))
        self.assertEqual(set(raw["optional"]), set(config._OPTIONAL_FILTER_KEYS))
        self.assertEqual(
            set(config._REQUIRED_FILTER_KEYS) | set(config._OPTIONAL_FILTER_KEYS),
            set(config._FILTER_KEYS),
        )

    def test_committed_file_loads_phase1_defaults(self):
        self.assertEqual(FilterConfig.from_file(), DEFAULT_FILTERS)

    # --- Loading sectioned values ---

    def test_sectioned_defaults_equal_default_filters(self):
        self._write(self._sectioned_defaults())
        self.assertEqual(FilterConfig.from_file(self.filters_path), DEFAULT_FILTERS)

    def test_sectioned_custom_values_load(self):
        payload = self._sectioned_defaults()
        payload["required"]["price_min"] = 400000
        payload["required"]["bedrooms_min"] = 4
        payload["optional"]["property_type"] = "appartement"
        payload["optional"]["plot_size_min"] = 50
        self._write(payload)

        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.price_min, 400000)
        self.assertEqual(filters.bedrooms_min, 4)
        self.assertEqual(filters.property_type, "appartement")
        self.assertEqual(filters.plot_size_min, 50)
        self.assertEqual(filters.price_max, 750000)
        self.assertIsNone(filters.energy_label_min)

    def test_missing_optional_section_becomes_none(self):
        self._write({"required": {"price_min": 550000}})
        filters = FilterConfig.from_file(self.filters_path)
        self.assertEqual(filters.price_min, 550000)
        self.assertEqual(filters.price_max, 750000)
        self.assertIsNone(filters.property_type)
        self.assertIsNone(filters.radius_km)

    def test_null_optional_values_in_sections_mean_no_restriction(self):
        payload = self._sectioned_defaults()
        payload["optional"]["transaction_type"] = None
        payload["optional"]["rooms_max"] = None
        self._write(payload)

        filters = FilterConfig.from_file(self.filters_path)
        self.assertIsNone(filters.transaction_type)
        self.assertIsNone(filters.rooms_max)

    def test_legacy_flat_layout_still_supported(self):
        self._write({"transaction_type": "huur"})
        self.assertEqual(
            FilterConfig.from_file(self.filters_path).transaction_type, "huur"
        )

    # --- Structural rejection ---

    def test_mixed_flat_and_sectioned_layout_rejected(self):
        self._write({
            "price_min": 400000,
            "optional": {"property_type": "appartement"},
        })
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_unknown_top_level_key_alongside_sections_rejected(self):
        payload = self._sectioned_defaults()
        payload["price_minn"] = 400000
        self._write(payload)
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_non_object_section_rejected(self):
        for bad_section in [{"required": [1, 2]}, {"optional": "nope"}]:
            with self.subTest(bad_section=bad_section):
                self._write(bad_section)
                with self.assertRaises(ValueError):
                    FilterConfig.from_file(self.filters_path)

    def test_required_key_in_optional_section_rejected(self):
        self._write({"optional": {"price_min": 100}})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_optional_key_in_required_section_rejected(self):
        self._write({"required": {"property_type": "appartement"}})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_unknown_key_inside_section_rejected(self):
        self._write({"optional": {"bedrooms_maximum": 5}})
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)

    def test_invalid_value_inside_section_still_rejected(self):
        payload = self._sectioned_defaults()
        payload["required"]["price_min"] = "cheap"
        self._write(payload)
        with self.assertRaises(ValueError):
            FilterConfig.from_file(self.filters_path)


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

    def test_direct_construction_validates_transaction_type(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, transaction_type="rent")
        self.assertEqual(
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, transaction_type="huur").transaction_type,
            "huur",
        )

    def test_direct_construction_validates_new_ranges(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3, living_area_min=100,
                         bedrooms_max=2)
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3, living_area_min=100,
                         rooms_min=6, rooms_max=3)
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3, living_area_min=100,
                         plot_size_min=500, plot_size_max=100)

    def test_direct_construction_validates_energy_label_max(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, energy_label_max=5)
        self.assertEqual(
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, energy_label_max="c").energy_label_max,
            "C",
        )

    def test_direct_construction_validates_radius_and_construction(self):
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, radius_km=0)
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, radius_km="10")
        with self.assertRaises(ValueError):
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, construction_type="renovated")
        self.assertEqual(
            FilterConfig(price_min=100, price_max=200, bedrooms_min=3,
                         living_area_min=100, radius_km=10,
                         construction_type="new").construction_type,
            "new",
        )


class RetentionConfigDefaultsTestCase(unittest.TestCase):
    """Phase 1 defaults and the default retention file."""

    def test_default_retention_value(self):
        self.assertEqual(DEFAULT_RETENTION.stale_days, 60)

    def test_default_path_is_project_root_relative(self):
        expected = Path(__file__).resolve().parent.parent / "config" / "retention.json"
        self.assertEqual(config._RETENTION_PATH, expected)
        self.assertTrue(config._RETENTION_PATH.is_absolute())
        self.assertTrue(config._RETENTION_PATH.exists())

    def test_default_retention_file_loads_60(self):
        self.assertEqual(RetentionConfig.from_file(), DEFAULT_RETENTION)


class RetentionConfigFileTestCase(unittest.TestCase):
    """File-driven tests for RetentionConfig.from_file()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.retention_path = Path(self._tmp.name) / "retention.json"

    def _write(self, obj):
        self.retention_path.write_text(json.dumps(obj), encoding="utf-8")

    def test_from_file_custom_stale_days(self):
        self._write({"stale_days": 90})
        retention = RetentionConfig.from_file(self.retention_path)
        self.assertEqual(retention.stale_days, 90)

    def test_missing_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_invalid_json_raises_value_error(self):
        self.retention_path.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_non_object_json_raises_value_error(self):
        self._write([1, 2, 3])
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_unknown_key_raises_value_error(self):
        self._write({"foo": 1})
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_missing_stale_days_falls_back_to_60(self):
        self._write({})
        retention = RetentionConfig.from_file(self.retention_path)
        self.assertEqual(retention.stale_days, 60)

    def test_string_stale_days_raises_value_error(self):
        self._write({"stale_days": "60"})
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_zero_stale_days_raises_value_error(self):
        self._write({"stale_days": 0})
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_negative_stale_days_raises_value_error(self):
        self._write({"stale_days": -5})
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)

    def test_bool_stale_days_raises_value_error(self):
        self._write({"stale_days": True})
        with self.assertRaises(ValueError):
            RetentionConfig.from_file(self.retention_path)


class RetentionConfigConstructionTestCase(unittest.TestCase):
    """Immutability and direct-construction validation."""

    def test_retentionconfig_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_RETENTION.stale_days = 1

    def test_direct_construction_validates_positive(self):
        with self.assertRaises(ValueError):
            RetentionConfig(stale_days=0)
        with self.assertRaises(ValueError):
            RetentionConfig(stale_days=-1)

    def test_direct_construction_accepts_valid(self):
        rc = RetentionConfig(stale_days=30)
        self.assertEqual(rc.stale_days, 30)


if __name__ == "__main__":
    unittest.main()
