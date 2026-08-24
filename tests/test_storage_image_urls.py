"""
Migration + persistence tests for the image_urls column
(src/storage.py).

Covers the runtime schema migration for legacy databases that predate
the rich-photo feature:

1. fresh database creates image_urls schema;
2. legacy database without image_urls migrates successfully;
3. migration is idempotent (runs twice without error);
4-6. existing rows/listing data/notified/scores remain intact;
7-9. insert / update / NULL handling for image_urls;
10. card-level partial updates do not erase stored image URLs;
11. detail-scraper output shape can be persisted;
12. notifier consumes the persisted representation.

All databases are temporary; no real database is touched.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


sys.modules.setdefault(
    "dotenv", _make_module("dotenv", load_dotenv=lambda *a, **k: None)
)

from src.storage import (  # noqa: E402
    init_db,
    insert_listing,
    fetch_unnotified_matching_listings,
    mark_as_notified,
    _encode_image_urls,
    _decode_image_urls,
)
from src.config import FilterConfig  # noqa: E402


def listing_data(listing_id="80913842", **overrides):
    """Dict matching scraper.py card output."""
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


LEGACY_IMAGE_URLS = [
    "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1440",
    "https://cloud.funda.nl/valentina_media/230/205/631.jpg?options=width=1440",
    "https://cloud.funda.nl/valentina_media/230/205/733.jpg?options=width=1440",
]

# Legacy CREATE TABLE matching the pre-image_urls production schema
# (as found on the runtime data/funda.db: ends at detail_fetched_at).
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    address TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    price INTEGER,
    living_area_m2 INTEGER,
    plot_size_m2 INTEGER,
    rooms INTEGER,
    bedrooms INTEGER,
    property_type TEXT,
    year_built INTEGER,
    energy_label TEXT,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    ownership_type TEXT,
    erfpacht_canon_annual REAL,
    garden_present INTEGER,
    garden_type TEXT,
    garden_size_m2 INTEGER,
    garden_orientation TEXT,
    balcony_present INTEGER,
    building_bound_outdoor_m2 INTEGER,
    garage_type TEXT,
    parking_type TEXT,
    insulation_raw TEXT,
    insulation_score REAL,
    heating_type TEXT,
    boiler_year INTEGER,
    bathrooms INTEGER,
    neighborhood_avg_price_m2 REAL,
    score INTEGER,
    score_breakdown TEXT,
    score_confidence TEXT,
    detail_fetched_at TEXT
);
"""


def _columns(db_path, table="listings"):
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(
            f"PRAGMA table_info({table})").fetchall()]


class TestFreshDatabaseSchema(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "funda.db")

    def test_fresh_db_creates_image_urls_column(self):
        init_db(self.db)
        cols = _columns(self.db)
        self.assertIn("image_urls", cols)

    def test_archive_table_mirrors_listings_columns(self):
        init_db(self.db)
        self.assertEqual(_columns(self.db), _columns(self.db, "listings_archive"))

    def test_migration_is_idempotent(self):
        init_db(self.db)
        cols_first = _columns(self.db)
        init_db(self.db)          # second run must not raise or duplicate
        init_db(self.db)          # third run for good measure
        self.assertEqual(cols_first, _columns(self.db))
        self.assertEqual(_columns(self.db).count("image_urls"), 1)


class TestLegacyMigration(unittest.TestCase):
    """Legacy DB (pre-image_urls, even pre-last_seen_at) -> migrated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "legacy.db")

        # Build a legacy database with one realistic scored row.
        conn = sqlite3.connect(self.db)
        conn.execute(_LEGACY_SCHEMA)
        conn.execute("""
            INSERT INTO listings (
                listing_id, url, address, neighborhood, price,
                living_area_m2, bedrooms, property_type, energy_label,
                first_seen_at, notified, ownership_type,
                erfpacht_canon_annual, bathrooms, score,
                score_breakdown, score_confidence, detail_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "44480057",
            "https://www.funda.nl/detail/koop/amsterdam/huis-x/44480057/",
            "Hilversumstraat 60", "amsterdam", 650000,
            115, 3, "huis", "B",
            "2026-08-01T10:00:00", 1, "erfpacht",
            408.85, 2, 82,
            '{"ownership": 12}', "full", "2026-08-01T10:05:00",
        ))
        conn.commit()
        conn.close()

    def _legacy_row(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT * FROM listings WHERE listing_id = ?", ("44480057",)
        ).fetchone())
        conn.close()
        return row

    def test_legacy_db_gains_image_urls_on_init(self):
        self.assertNotIn("image_urls", _columns(self.db))
        init_db(self.db)
        self.assertIn("image_urls", _columns(self.db))

    def test_migration_preserves_existing_row_data(self):
        before = self._legacy_row()
        init_db(self.db)
        after = self._legacy_row()
        for field in (
            "listing_id", "price", "bedrooms", "living_area_m2",
            "notified", "score", "score_breakdown", "score_confidence",
            "ownership_type", "erfpacht_canon_annual",
            "address", "url", "first_seen_at", "detail_fetched_at",
        ):
            self.assertEqual(before[field], after[field], field)
        # new column is NULL for the existing row
        self.assertIsNone(after["image_urls"])

    def test_double_migration_keeps_data_intact(self):
        init_db(self.db)
        init_db(self.db)
        after = self._legacy_row()
        self.assertEqual(after["listing_id"], "44480057")
        self.assertEqual(after["notified"], 1)
        self.assertEqual(after["score"], 82)
        self.assertEqual(_columns(self.db).count("image_urls"), 1)


class TestImageUrlsPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "funda.db")
        init_db(self.db)

    def _stored(self, listing_id, field="image_urls"):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                f"SELECT {field} FROM listings WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()[0]

    def test_insert_persists_image_urls_as_json_text(self):
        listing = listing_data(image_urls=list(LEGACY_IMAGE_URLS))
        self.assertEqual(insert_listing(listing, self.db), "inserted")
        stored = self._stored("80913842")
        self.assertIsInstance(stored, str)
        self.assertEqual(json.loads(stored), LEGACY_IMAGE_URLS)

    def test_null_and_empty_image_urls_stay_null(self):
        insert_listing(listing_data(listing_id="null-case"), self.db)
        self.assertIsNone(self._stored("null-case"))
        insert_listing(listing_data(listing_id="empty-case", image_urls=[]),
                       self.db)
        self.assertIsNone(self._stored("empty-case"))

    def test_update_replaces_image_urls(self):
        insert_listing(listing_data(image_urls=LEGACY_IMAGE_URLS[:2]), self.db)
        insert_listing(listing_data(image_urls=LEGACY_IMAGE_URLS), self.db)
        self.assertEqual(json.loads(self._stored("80913842")),
                         LEGACY_IMAGE_URLS)

    def test_card_level_update_does_not_erase_stored_urls(self):
        """Detail fetch stores URLs; later card-only scrape keeps them."""
        detail = listing_data(image_urls=LEGACY_IMAGE_URLS)
        insert_listing(detail, self.db)
        card = listing_data()   # no image_urls key at all
        result = insert_listing(card, self.db)
        self.assertIn(result, ("updated_unchanged", "unchanged"))
        self.assertEqual(json.loads(self._stored("80913842")),
                         LEGACY_IMAGE_URLS)

    def test_detail_scraper_output_shape_roundtrip(self):
        """The exact list produced by detail_scraper survives a roundtrip."""
        canonical = [
            "https://cloud.funda.nl/valentina_media/a/1.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/a/2.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/a/3.jpg?options=width=1440",
        ]
        insert_listing(listing_data(image_urls=canonical), self.db)
        matches = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["image_urls"], canonical)

    def test_fetch_decodes_json_and_handles_null(self):
        insert_listing(listing_data(listing_id="with-urls",
                                    image_urls=LEGACY_IMAGE_URLS), self.db)
        insert_listing(listing_data(listing_id="without-urls"), self.db)
        matches = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        by_id = {m["listing_id"]: m["image_urls"] for m in matches}
        self.assertEqual(by_id.get("with-urls"), LEGACY_IMAGE_URLS)
        self.assertIsNone(by_id.get("without-urls"))

    def test_notified_flow_with_images(self):
        insert_listing(listing_data(image_urls=LEGACY_IMAGE_URLS), self.db)
        matches = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        self.assertEqual(len(matches), 1)
        mark_as_notified("80913842", self.db)
        matches_after = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        self.assertEqual(matches_after, [])


class TestEncodeDecodeHelpers(unittest.TestCase):
    def test_encode_variants(self):
        urls = ["https://x/1.jpg"]
        self.assertEqual(json.loads(_encode_image_urls(urls)), urls)
        self.assertIsNone(_encode_image_urls(None))
        self.assertIsNone(_encode_image_urls([]))
        self.assertIsNone(_encode_image_urls(["", "   ", 42]))
        # already-encoded string is normalised
        self.assertEqual(json.loads(_encode_image_urls(json.dumps(urls))), urls)
        self.assertIsNone(_encode_image_urls("not-json"))

    def test_decode_variants(self):
        self.assertEqual(
            _decode_image_urls(json.dumps(["https://x/1.jpg"])),
            ["https://x/1.jpg"])
        self.assertIsNone(_decode_image_urls(None))
        self.assertIsNone(_decode_image_urls(""))
        self.assertIsNone(_decode_image_urls("garbage{"))
        self.assertIsNone(_decode_image_urls('{"not":"a-list"}'))


if __name__ == "__main__":
    unittest.main()
