"""
Phase 10 — scraper -> storage integration contract tests.

These tests verify that dictionaries produced by src/scraper.py
(``_extract_listing_data`` shape) can be consumed by src/storage.py
and persisted in SQLite without key errors, integrity errors, or
silent data loss.

They use temporary SQLite databases only. No live Funda access.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_FILTERS, FilterConfig
from src.storage import (
    init_db,
    insert_listing,
    listing_exists,
    fetch_unnotified_matching_listings,
    mark_as_notified,
)


def scraper_shaped_listing(listing_id="80913842", **overrides):
    """A dict matching the exact shape produced by scraper.py.

    Reproduces the card-level extraction contract: status, rooms, and
    year_built are always None; the other optional fields may be None
    when not parseable from the card text.
    """
    data = {
        "listing_id": listing_id,
        "url": f"https://www.funda.nl/detail/koop/amsterdam/huis-x/{listing_id}/",
        "address": "Schaarbeekstraat 71",
        "neighborhood": "amsterdam",
        "price": 650000,
        "living_area_m2": 110,
        "plot_size_m2": None,
        "bedrooms": 3,
        "property_type": "huis",
        "energy_label": "B",
        "status": None,
        "rooms": None,
        "year_built": None,
    }
    data.update(overrides)
    return data


class StorageContractTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "funda.db")
        init_db(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def _count_rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def _notified(self, listing_id):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()[0]

    def test_table_columns_match_scraper_contract(self):
        with sqlite3.connect(self.db) as conn:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()
            }
        expected = {
            "listing_id", "url", "address", "neighborhood", "price",
            "living_area_m2", "plot_size_m2", "rooms", "bedrooms",
            "property_type", "year_built", "energy_label", "status",
            "first_seen_at", "notified",
            # Phase 2 detail/scoring columns
            "ownership_type", "erfpacht_canon_annual", "garden_present",
            "garden_type", "garden_size_m2", "garden_orientation",
            "balcony_present", "building_bound_outdoor_m2",
            "garage_type", "parking_type", "insulation_raw", "insulation_score",
            "heating_type", "boiler_year",
            "bathrooms", "neighborhood_avg_price_m2",
            "score", "score_breakdown", "score_confidence", "detail_fetched_at",
        }
        self.assertEqual(expected, cols)

    def test_insert_repr_scraper_shaped_listing(self):
        listing = scraper_shaped_listing()
        self.assertTrue(insert_listing(listing, self.db))
        self.assertEqual(self._count_rows(), 1)
        self.assertTrue(listing_exists("80913842", self.db))

    def test_nullable_card_level_fields_are_stored_not_dropped(self):
        missing = scraper_shaped_listing(listing_id="missing-req", price=None,
                                         living_area_m2=None, bedrooms=None)
        self.assertEqual(insert_listing(missing, self.db), "unchanged")
        self.assertEqual(self._count_rows(), 0)
        self.assertFalse(listing_exists("missing-req", self.db))

        listing = scraper_shaped_listing(listing_id="null-fields",
                                         plot_size_m2=None,
                                         property_type=None,
                                         year_built=None,
                                         energy_label=None)
        self.assertEqual(insert_listing(listing, self.db), "inserted")
        self.assertEqual(self._count_rows(), 1)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT price, living_area_m2, rooms, plot_size_m2, property_type, "
                "status, year_built, energy_label FROM listings WHERE listing_id = ?",
                ("null-fields",),
            ).fetchone()
        self.assertEqual(row, (650000, 110, None, None, None, None, None, None))

    def test_duplicate_listing_id_is_not_duplicated(self):
        listing = scraper_shaped_listing()
        self.assertEqual(insert_listing(listing, self.db), "inserted")
        self.assertEqual(insert_listing(listing, self.db), "unchanged")
        self.assertEqual(self._count_rows(), 1)

    def test_insert_requires_listing_id(self):
        listing = scraper_shaped_listing()
        del listing["listing_id"]
        with self.assertRaises(ValueError):
            insert_listing(listing, self.db)
        self.assertEqual(self._count_rows(), 0)

    def test_non_matching_listing_is_stored(self):
        listing = scraper_shaped_listing(
            listing_id="non-matching", price=450000, living_area_m2=80, bedrooms=2
        )
        self.assertTrue(insert_listing(listing, self.db))
        self.assertEqual(self._count_rows(), 1)
        self.assertEqual(fetch_unnotified_matching_listings(self.db), [])

    def test_filter_returns_only_matching_unnotified(self):
        matching = scraper_shaped_listing(listing_id="matching")
        non_matching = scraper_shaped_listing(
            listing_id="non-matching", price=800000
        )
        insert_listing(matching, self.db)
        insert_listing(non_matching, self.db)

        results = fetch_unnotified_matching_listings(self.db)
        self.assertEqual([r["listing_id"] for r in results], ["matching"])
        self.assertEqual(results[0]["notified"], 0)

    def test_filter_boundaries_inclusive(self):
        lo = scraper_shaped_listing(listing_id="lo", price=550000,
                                    living_area_m2=100, bedrooms=3)
        hi = scraper_shaped_listing(listing_id="hi", price=750000)
        insert_listing(lo, self.db)
        insert_listing(hi, self.db)
        results = {r["listing_id"] for r in fetch_unnotified_matching_listings(self.db)}
        self.assertEqual(results, {"lo", "hi"})

    def test_mark_as_notified_updates_state(self):
        listing = scraper_shaped_listing(listing_id="matching")
        insert_listing(listing, self.db)
        self.assertEqual(len(fetch_unnotified_matching_listings(self.db)), 1)

        mark_as_notified("matching", self.db)
        self.assertEqual(fetch_unnotified_matching_listings(self.db), [])
        with sqlite3.connect(self.db) as conn:
            notified = conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?", ("matching",)
            ).fetchone()[0]
        self.assertEqual(notified, 1)

    def test_new_insert_defaults_notified_and_timestamp(self):
        listing = scraper_shaped_listing()
        insert_listing(listing, self.db)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT notified, first_seen_at FROM listings WHERE listing_id = ?",
                ("80913842",),
            ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertIsNotNone(row[1])

    def test_notified_is_preserved_on_unchanged_rescrape(self):
        listing = scraper_shaped_listing()
        insert_listing(listing, self.db)
        self.assertEqual(self._notified("80913842"), 0)
        self.assertEqual(len(fetch_unnotified_matching_listings(self.db)), 1)

        mark_as_notified("80913842", self.db)
        self.assertEqual(self._notified("80913842"), 1)

        result = insert_listing(scraper_shaped_listing(), self.db)
        self.assertEqual(result, "unchanged")
        self.assertEqual(self._notified("80913842"), 1)
        self.assertEqual(fetch_unnotified_matching_listings(self.db), [])

    def test_notified_is_preserved_on_price_or_status_change(self):
        """Task 1: price or status changes must NOT reset notified."""
        insert_listing(scraper_shaped_listing(), self.db)
        mark_as_notified("80913842", self.db)

        price_result = insert_listing(scraper_shaped_listing(price=620000), self.db)
        self.assertEqual(price_result, "updated_unchanged")
        self.assertEqual(self._notified("80913842"), 1)

        mark_as_notified("80913842", self.db)
        status_result = insert_listing(scraper_shaped_listing(status="verkocht"), self.db)
        self.assertEqual(status_result, "updated_unchanged")
        self.assertEqual(self._notified("80913842"), 1)

    def test_changed_other_fields_keep_notified_intact(self):
        insert_listing(scraper_shaped_listing(), self.db)
        mark_as_notified("80913842", self.db)

        result = insert_listing(
            scraper_shaped_listing(address="Andere Straat 2"), self.db
        )
        self.assertEqual(result, "updated_unchanged")
        self.assertEqual(self._notified("80913842"), 1)
        self.assertEqual(fetch_unnotified_matching_listings(self.db), [])

    # --- Phase 2: configurable FilterConfig filters ---

    def test_default_filters_match_positional_phase1_call(self):
        insert_listing(scraper_shaped_listing(listing_id="matching"), self.db)
        self.assertEqual(
            fetch_unnotified_matching_listings(self.db),
            fetch_unnotified_matching_listings(self.db, filters=DEFAULT_FILTERS),
        )

    def test_custom_price_range_filter(self):
        for listing in [
            scraper_shaped_listing(listing_id="lo", price=550000),
            scraper_shaped_listing(listing_id="mid", price=650000),
            scraper_shaped_listing(listing_id="hi", price=750000),
            scraper_shaped_listing(listing_id="out", price=800000),
        ]:
            insert_listing(listing, self.db)

        filters = FilterConfig(price_min=600000, price_max=700000,
                               bedrooms_min=3, living_area_min=100)
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["mid"])

    def test_custom_bedrooms_min_filter(self):
        for listing in [
            scraper_shaped_listing(listing_id="b2", bedrooms=2),
            scraper_shaped_listing(listing_id="b3", bedrooms=3),
            scraper_shaped_listing(listing_id="b4", bedrooms=4),
        ]:
            insert_listing(listing, self.db)

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=4, living_area_min=100)
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["b4"])

    def test_custom_living_area_min_filter(self):
        for listing in [
            scraper_shaped_listing(listing_id="s80", living_area_m2=80),
            scraper_shaped_listing(listing_id="s120", living_area_m2=120),
        ]:
            insert_listing(listing, self.db)

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=120)
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["s120"])

    def test_property_type_filter(self):
        insert_listing(
            scraper_shaped_listing(listing_id="huis", property_type="huis"), self.db
        )
        insert_listing(
            scraper_shaped_listing(listing_id="app", property_type="appartement"), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               property_type="appartement")
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["app"])

    def test_plot_size_min_filter(self):
        insert_listing(
            scraper_shaped_listing(listing_id="small", plot_size_m2=40), self.db
        )
        insert_listing(
            scraper_shaped_listing(listing_id="big", plot_size_m2=120), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               plot_size_min=50)
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["big"])

    def test_energy_label_min_filter(self):
        for listing in [
            scraper_shaped_listing(listing_id="label-a", energy_label="A"),
            scraper_shaped_listing(listing_id="label-c", energy_label="C"),
            scraper_shaped_listing(listing_id="label-g", energy_label="G"),
        ]:
            insert_listing(listing, self.db)

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               energy_label_min="B")
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["label-a"])

    def test_energy_label_min_is_uppercase_normalized(self):
        insert_listing(
            scraper_shaped_listing(listing_id="label-a", energy_label="A"), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               energy_label_min="a")
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["label-a"])

    def test_unknown_energy_label_min_raises_value_error(self):
        insert_listing(
            scraper_shaped_listing(listing_id="label-a", energy_label="A"), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               energy_label_min="Z")
        with self.assertRaises(ValueError):
            fetch_unnotified_matching_listings(self.db, filters=filters)

    def test_none_preference_disables_that_preference(self):
        insert_listing(scraper_shaped_listing(), self.db)

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               property_type=None, plot_size_min=None,
                               energy_label_min=None)
        results = fetch_unnotified_matching_listings(self.db, filters=filters)
        self.assertEqual([r["listing_id"] for r in results], ["80913842"])

    def test_null_property_type_does_not_pass_property_type_filter(self):
        insert_listing(
            scraper_shaped_listing(listing_id="no-type", property_type=None), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               property_type="appartement")
        self.assertEqual(
            fetch_unnotified_matching_listings(self.db, filters=filters), []
        )

    def test_null_plot_size_does_not_pass_plot_size_filter(self):
        insert_listing(
            scraper_shaped_listing(listing_id="no-plot", plot_size_m2=None), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               plot_size_min=50)
        self.assertEqual(
            fetch_unnotified_matching_listings(self.db, filters=filters), []
        )

    def test_null_energy_label_does_not_pass_energy_label_filter(self):
        insert_listing(
            scraper_shaped_listing(listing_id="no-label", energy_label=None), self.db
        )

        filters = FilterConfig(price_min=550000, price_max=750000,
                               bedrooms_min=3, living_area_min=100,
                               energy_label_min="C")
        self.assertEqual(
            fetch_unnotified_matching_listings(self.db, filters=filters), []
        )


if __name__ == "__main__":
    unittest.main()
