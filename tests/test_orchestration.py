"""
Orchestration wiring/order tests for src/main.py.

Complements tests/test_main.py (component integration) by pinning the
orchestration contract itself:

* the pipeline order: configuration -> scrape -> persist -> match;
* the scraper receives ONLY search-level parameters (storage-level
  preference filters never leak into the Funda URL);
* scan-mode -> publication-date/paging mapping stays deterministic
  (full scan: no publication filter + 5-page cap; delta scan:
  3-day publication filter + 15-page ceiling);
* full-scan gating suppresses newly inserted low-scored listings while
  high-scored ones notify normally (Task 2 behaviour).

All components are mocked; nothing touches Funda or Telegram.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


playwright = _make_module("playwright")
playwright.sync_api = _make_module(
    "playwright.sync_api",
    sync_playwright=mock.MagicMock(),
    Page=mock.MagicMock(),
    Browser=mock.MagicMock(),
)
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", playwright.sync_api)

sys.modules.setdefault(
    "dotenv", _make_module("dotenv", load_dotenv=mock.MagicMock())
)

from src import main as main_module  # noqa: E402
from src import storage  # noqa: E402
from src.config import FilterConfig  # noqa: E402
from src.scoring import ScoreResult  # noqa: E402


def listing_data(listing_id="80913842", **overrides):
    """A dict matching the shape produced by scraper.py."""
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


class OrchestrationPipelineTestCase(unittest.TestCase):
    """Pipeline order and component-wiring guarantees."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "funda.db")
        # Snapshot + recent successful run => deterministic DELTA scans,
        # so gating is off unless a test deliberately uses a fresh DB.
        storage.save_filter_snapshot(FilterConfig.from_file(), self.db)
        storage.save_last_successful_run(datetime.now(timezone.utc), self.db)

    def _run_main(self, argv=()):
        with mock.patch.object(sys, "argv", ["src/main.py", "--db-path", self.db, *argv]):
            main_module.main()

    def _notified(self, listing_id):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return row[0] if row else None

    # --- Pipeline order ---

    def test_pipeline_order_is_config_scrape_persist_match(self):
        events = []

        real_insert = main_module.insert_listing

        def recording_insert(listing, db_path):
            events.append("persist")
            return real_insert(listing, db_path)

        real_from_file = main_module.FilterConfig.from_file

        def recording_from_file(*args, **kwargs):
            events.append("config")
            return real_from_file(*args, **kwargs)

        def fake_scrape(**kwargs):
            events.append("scrape")
            return [listing_data()]

        def fake_fetch(db_path, filters=None):
            events.append("match")
            return []

        with mock.patch.object(main_module.FilterConfig, "from_file",
                               side_effect=recording_from_file), \
             mock.patch.object(main_module, "insert_listing", recording_insert), \
             mock.patch.object(main_module, "scrape_funda", side_effect=fake_scrape), \
             mock.patch.object(main_module, "fetch_unnotified_matching_listings",
                               side_effect=fake_fetch), \
             mock.patch.object(main_module, "send_notifications", return_value=[]):
            self._run_main()

        self.assertEqual(events, ["config", "scrape", "persist", "match"])

    # --- Scraper receives exactly the search-level parameters ---

    def test_scraper_receives_exactly_search_level_parameters(self):
        captured = {}

        def fake_scrape(**kwargs):
            captured.update(kwargs)
            return [listing_data()]

        with mock.patch.object(main_module, "scrape_funda", side_effect=fake_scrape), \
             mock.patch.object(main_module, "send_notifications", return_value=[]):
            self._run_main()

        expected_keys = {
            "area",
            "offering_type",
            "price_min", "price_max",
            "floor_area_min", "floor_area_max",
            "bedrooms_min", "bedrooms_max",
            "rooms_min", "rooms_max",
            "radius_km",
            "construction_type",
            "publication_date_days", "max_pages",
        }
        self.assertEqual(set(captured), expected_keys)

    # --- Scan-mode -> publication/paging mapping is deterministic ---

    def test_delta_scan_uses_3day_publication_and_15page_ceiling(self):
        captured = {}

        def fake_scrape(**kwargs):
            captured.update(kwargs)
            return [listing_data()]

        with mock.patch.object(main_module, "scrape_funda", side_effect=fake_scrape), \
             mock.patch.object(main_module, "send_notifications", return_value=[]):
            self._run_main()

        self.assertEqual(captured["publication_date_days"], 3)
        self.assertEqual(captured["max_pages"], 15)

    def test_full_scan_after_filter_change_uses_no_publication_filter(self):
        # Fresh DB: no snapshot => first-run-after-filter-change => full scan.
        os.remove(self.db)
        storage.init_db(self.db)
        captured = {}

        def fake_scrape(**kwargs):
            captured.update(kwargs)
            return [listing_data()]

        with mock.patch.object(main_module, "scrape_funda", side_effect=fake_scrape), \
             mock.patch.object(main_module, "fetch_listing_details", return_value={}), \
             mock.patch.object(main_module, "send_notifications", return_value=[]):
            self._run_main()

        self.assertIsNone(captured["publication_date_days"])
        self.assertEqual(captured["max_pages"], 5)


class FullScanGatingTestCase(unittest.TestCase):
    """Full-scan gate wiring through main() (discovered-unittest port of the
    manual tests/test_task2_gating.py scenario A)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "funda.db")

    def _run_main(self):
        with mock.patch.object(sys, "argv", ["src/main.py", "--db-path", self.db]):
            main_module.main()

    def _notified(self, listing_id):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return row[0] if row else None

    def test_full_scan_gate_notifies_only_high_scored_new_listings(self):
        low = listing_data("orch-gate-low", price=640000)
        high = listing_data("orch-gate-high", price=710000)
        scores = {"orch-gate-low": 65, "orch-gate-high": 75}

        def fake_score(detail, *args, **kwargs):
            score = scores.get(detail.get("listing_id"), 50)
            return ScoreResult(
                score=score,
                breakdown=[{"criterion": "neighborhood_value",
                            "points_earned": score // 2,
                            "points_possible": 21, "matched": True}],
                confidence="partial",
                missing_criteria=["construction_condition"],
            )

        with mock.patch.object(main_module, "scrape_funda", return_value=[low, high]), \
             mock.patch.object(main_module, "fetch_listing_details", return_value={}), \
             mock.patch.object(main_module, "score_listing", side_effect=fake_score), \
             mock.patch.object(main_module, "send_notifications",
                               return_value=[True]) as send:
            self._run_main()

        sent_ids = [l["listing_id"] for l in send.call_args[0][0]]
        self.assertEqual(sent_ids, ["orch-gate-high"])
        # Suppressed listing must still be marked notified so it does not
        # re-enter the notification flow later.
        self.assertEqual(self._notified("orch-gate-low"), 1)
        self.assertEqual(self._notified("orch-gate-high"), 1)


if __name__ == "__main__":
    unittest.main()
