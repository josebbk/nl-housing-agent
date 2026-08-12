"""
Orchestration tests for src/main.py.

These tests verify the scraper -> storage -> filter -> notifier integration
using mocked components and a temporary SQLite database. They do not contact
Funda, Telegram, or any external network.

playwright.sync_api and dotenv are stubbed in sys.modules so the real
src.scraper and src.notifier modules import without third-party dependencies
installed.
"""

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


def run_main(argv, db_path):
    """Run main_module.main() with controlled argv and a temp DB path."""
    with mock.patch.object(sys, "argv", ["src/main.py", "--db-path", db_path] + argv):
        main_module.main()


class MainOrchestrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "funda.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _count_rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def _notified(self, listing_id):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return row[0] if row else None

    # --- Database initialisation ---

    def test_database_is_initialized_and_listings_stored(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True]) as send:
            run_main([], self.db)
        self.assertTrue(Path(self.db).exists())
        self.assertEqual(self._count_rows(), 1)
        send.assert_called_once()

    # --- Scraper output -> storage ---

    def test_scraper_output_is_passed_to_storage(self):
        data = listing_data(listing_id="other-1")
        with mock.patch.object(main_module, "scrape_funda", return_value=[data]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True]):
            run_main([], self.db)
        self.assertEqual(self._count_rows(), 1)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT address, price, bedrooms, living_area_m2 "
                "FROM listings WHERE listing_id = ?",
                ("other-1",),
            ).fetchone()
        self.assertEqual(row, ("Schaarbeekstraat 71", 650000, 3, 110))

    # --- Deduplication ---

    def test_duplicate_listing_is_not_duplicated_or_re_notified(self):
        data = listing_data()
        with mock.patch.object(main_module, "scrape_funda", return_value=[data]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True]) as send:
            run_main([], self.db)
            run_main([], self.db)
        self.assertEqual(self._count_rows(), 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[1][0][0], [])

    # --- Filtering (delegated to storage) ---

    def test_matching_unnotified_listing_is_sent_to_notifier(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True]) as send:
            run_main([], self.db)
        send.assert_called_once()
        sent = send.call_args[0][0]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["listing_id"], "80913842")

    def test_non_matching_listing_is_stored_but_not_notified(self):
        data = listing_data(
            listing_id="non-matching", price=450000, living_area_m2=80, bedrooms=2
        )
        with mock.patch.object(main_module, "scrape_funda", return_value=[data]), \
             mock.patch.object(main_module, "send_notifications", return_value=[]) as send:
            run_main([], self.db)
        self.assertEqual(self._count_rows(), 1)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], [])

    # --- Notification bookkeeping ---

    def test_successful_notification_marks_listing_as_notified(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True]):
            run_main([], self.db)
        self.assertEqual(self._notified("80913842"), 1)

    def test_failed_notification_is_not_marked_and_run_fails(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications", return_value=[False]):
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self._notified("80913842"), 0)

    def test_partial_failures_only_mark_successful_listings(self):
        ok = listing_data(listing_id="ok-listing")
        fail = listing_data(listing_id="fail-listing")
        with mock.patch.object(main_module, "scrape_funda", return_value=[ok, fail]), \
             mock.patch.object(main_module, "send_notifications", return_value=[True, False]):
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self._notified("ok-listing"), 1)
        self.assertEqual(self._notified("fail-listing"), 0)

    def test_notifier_exception_marks_nothing_and_fails_run(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications",
                               side_effect=RuntimeError("no token")):
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self._notified("80913842"), 0)

    # --- Failure handling ---

    def test_scraper_failure_fails_the_run_and_sends_nothing(self):
        with mock.patch.object(main_module, "scrape_funda",
                               side_effect=RuntimeError("blocked")), \
             mock.patch.object(main_module, "send_notifications") as send:
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        send.assert_not_called()

    def test_zero_listing_scrape_is_treated_as_failed_run(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[]), \
             mock.patch.object(main_module, "send_notifications") as send:
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        send.assert_not_called()

    def test_fetch_failure_fails_the_run(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications") as send, \
             mock.patch.object(main_module, "fetch_unnotified_matching_listings",
                               side_effect=sqlite3.Error("database is locked")):
            with self.assertRaises(SystemExit) as ctx:
                run_main([], self.db)
        self.assertEqual(ctx.exception.code, 1)
        send.assert_not_called()

    # --- Dry-run ---

    def test_dry_run_skips_notifications_and_marking(self):
        with mock.patch.object(main_module, "scrape_funda", return_value=[listing_data()]), \
             mock.patch.object(main_module, "send_notifications") as send:
            run_main(["--dry-run"], self.db)
        self.assertEqual(self._count_rows(), 1)
        send.assert_not_called()
        self.assertEqual(self._notified("80913842"), 0)

    # --- Storage robustness ---

    def test_storage_insert_failure_does_not_abort_the_run(self):
        bad = {
            "url": "https://www.funda.nl/detail/koop/amsterdam/x/1/",
            "address": "Missing ID Street 1",
            "neighborhood": "amsterdam",
        }
        with mock.patch.object(main_module, "scrape_funda", return_value=[bad]), \
             mock.patch.object(main_module, "send_notifications", return_value=[]):
            run_main([], self.db)
        self.assertEqual(self._count_rows(), 0)


if __name__ == "__main__":
    unittest.main()
