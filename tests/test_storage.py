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
        }
        self.assertEqual(expected, cols)

    def test_insert_repr_scraper_shaped_listing(self):
        listing = scraper_shaped_listing()
        self.assertTrue(insert_listing(listing, self.db))
        self.assertEqual(self._count_rows(), 1)
        self.assertTrue(listing_exists("80913842", self.db))

    def test_nullable_card_level_fields_are_stored_not_dropped(self):
        listing = scraper_shaped_listing(listing_id="null-fields", price=None,
                                         living_area_m2=None, bedrooms=None,
                                         property_type=None, energy_label=None)
        self.assertTrue(insert_listing(listing, self.db))
        self.assertEqual(self._count_rows(), 1)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT price, living_area_m2, rooms, property_type, status, "
                "year_built, energy_label FROM listings WHERE listing_id = ?",
                ("null-fields",),
            ).fetchone()
        self.assertEqual(row, (None, None, None, None, None, None, None))

    def test_duplicate_listing_id_is_not_duplicated(self):
        listing = scraper_shaped_listing()
        self.assertTrue(insert_listing(listing, self.db))
        self.assertFalse(insert_listing(listing, self.db))
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


if __name__ == "__main__":
    unittest.main()
