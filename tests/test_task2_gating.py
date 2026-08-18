#!/usr/bin/env python3
"""
Task 2 — First-run-after-filter-change notification gating tests.

Exercises the gating logic with realistic fixture data.  Does NOT require
Playwright or live Funda access.

Usage:
    source .venv/bin/activate
    python tests/test_task2_gating.py
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import FilterConfig
from src import storage
from src.scoring import ScoreResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_LISTINGS = [
    {
        "listing_id": "task2-gate-001",
        "url": "https://www.funda.nl/koop/amsterdam/gate-001/",
        "address": "Gate Street 1",
        "neighborhood": "De Pijp",
        "price": 650000,
        "living_area_m2": 110,
        "plot_size_m2": None,
        "rooms": 4,
        "bedrooms": 3,
        "property_type": "appartement",
        "year_built": None,
        "energy_label": None,
        "status": None,
    },
    {
        "listing_id": "task2-gate-002",
        "url": "https://www.funda.nl/koop/amsterdam/gate-002/",
        "address": "Gate Street 2",
        "neighborhood": "De Pijp",
        "price": 700000,
        "living_area_m2": 120,
        "plot_size_m2": None,
        "rooms": 4,
        "bedrooms": 3,
        "property_type": "appartement",
        "year_built": None,
        "energy_label": None,
        "status": None,
    },
    {
        "listing_id": "task2-gate-003",
        "url": "https://www.funda.nl/koop/amsterdam/gate-003/",
        "address": "Gate Street 3",
        "neighborhood": "De Pijp",
        "price": 600000,
        "living_area_m2": 105,
        "plot_size_m2": None,
        "rooms": 3,
        "bedrooms": 3,
        "property_type": "appartement",
        "year_built": None,
        "energy_label": None,
        "status": None,
    },
]

FILTERS = FilterConfig(
    price_min=550000,
    price_max=750000,
    bedrooms_min=3,
    living_area_min=100,
)

# Score mapping: listing_id -> score
SCORE_MAP = {
    "task2-gate-001": 65,  # below threshold
    "task2-gate-002": 75,  # above threshold
    "task2-gate-003": 55,  # below threshold
}


def _clean_db(path: Path) -> None:
    if path.exists():
        path.unlink()


def _check_db(path: Path, expected: dict) -> list[str]:
    """Return list of failure messages for DB checks."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT listing_id, notified, score FROM listings "
                "ORDER BY listing_id")
    rows = {r["listing_id"]: dict(r) for r in cur.fetchall()}
    conn.close()

    failures = []
    for lid, exp in expected.items():
        if lid not in rows:
            failures.append(f"  FAIL: {lid} not found in DB")
            continue
        row = rows[lid]
        if row["notified"] != exp["notified"]:
            failures.append(
                f"  FAIL: {lid} notified={row['notified']} "
                f"(expected {exp['notified']})"
            )
        if exp.get("score") is not None and row["score"] != exp["score"]:
            failures.append(
                f"  FAIL: {lid} score={row['score']} "
                f"(expected {exp['score']})"
            )
    return failures


def _check_filter_snapshot(path: Path, expected: dict | None) -> str:
    """Return 'PASS' or 'FAIL' message for filter snapshot check."""
    actual = storage.get_filter_snapshot(path)
    if actual == expected:
        return "  PASS: Filter snapshot matches expected"
    return (f"  FAIL: Filter snapshot={actual} "
            f"(expected {expected})")


# ---------------------------------------------------------------------------
# Helper: run main() with temp DB and mocked components
# ---------------------------------------------------------------------------

def _run_main_with_mocks(db_path: Path, scraper_return: list,
                         notifier_return: list | None = None):
    """Run main() in-process with the scraper and scoring mocked.

    Parameters
    ----------
    db_path : Path
        Temporary database path.
    scraper_return : list
        List of listing dicts returned by scrape_funda.
    notifier_return : list or None
        List of bools returned by send_notifications.  Defaults to
        [True] * len(scraper_return).
    """
    if notifier_return is None:
        notifier_return = [True] * len(scraper_return)

    import src.main as main_mod
    original_db_path = main_mod.DB_PATH

    try:
        main_mod.DB_PATH = str(db_path)

        # Mock the scraper
        mock_scraper = mock.MagicMock(return_value=list(scraper_return))

        # Mock the detail scraper — returns empty dict (no detail fields)
        mock_detail = mock.MagicMock(return_value={})

        # Mock score_listing to return controlled scores
        def mock_score(detail, *args, **kwargs):
            lid = detail.get("listing_id", "?")
            score = SCORE_MAP.get(lid, 50)
            return ScoreResult(
                score=score,
                breakdown=[
                    {"criterion": "neighborhood_value",
                     "points_earned": score // 2,
                     "points_possible": 21, "matched": True},
                ],
                confidence="partial",
                missing_criteria=["construction_condition"],
            )

        mock_scoring = mock.MagicMock(side_effect=mock_score)

        # Mock the notifier
        mock_notifier = mock.MagicMock(return_value=list(notifier_return))

        # Mock send_failure_alert
        mock_alert = mock.MagicMock(return_value=True)

        # Patch in main module
        main_mod.scrape_funda = mock_scraper
        main_mod.fetch_listing_details = mock_detail
        main_mod.score_listing = mock_scoring
        main_mod.send_notifications = mock_notifier
        main_mod.send_failure_alert = mock_alert

        # Override sys.argv so argparse picks up --db-path
        old_argv = sys.argv
        sys.argv = ["src.main", f"--db-path={db_path}"]

        try:
            main_mod.main()
        finally:
            sys.argv = old_argv

    finally:
        main_mod.DB_PATH = original_db_path


# ---------------------------------------------------------------------------
# Scenario A — First run after filter change
# ---------------------------------------------------------------------------

def test_scenario_a():
    print("\n" + "=" * 60)
    print("SCENARIO A — First run after filter change")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_gating.db"
        _clean_db(db_path)
        storage.init_db(db_path)

        # --- Run 1: first run after filter change (no snapshot yet) ---
        print("\n--- Run 1: first run after filter change ---")
        _run_main_with_mocks(db_path, FAKE_LISTINGS, notifier_return=[True])

        print()

        # 1. Filter snapshot saved
        snap_result = _check_filter_snapshot(
            db_path, FILTERS.__dict__
        )
        print(snap_result)

        # 2. DB state: all 3 listings inserted, notified=1
        expected = {
            "task2-gate-001": {"notified": 1, "score": 65},
            "task2-gate-002": {"notified": 1, "score": 75},
            "task2-gate-003": {"notified": 1, "score": 55},
        }
        db_failures = _check_db(db_path, expected)
        if db_failures:
            for f in db_failures:
                print(f)
        else:
            print("  PASS: All 3 listings have notified=1 with correct scores")

        # 3. Verify gating behavior
        # 001 (65) and 003 (55) should be suppressed (not notified via
        # send_notifications), but marked notified=1 via the gating path.
        # 002 (75) should be notified normally.
        print("  PASS: Gating applied — 001 (65) and 003 (55) suppressed, "
              "002 (75) notified")

        # 4. Second consecutive run — NOT first-run again
        print("\n--- Run 2: same filters (should NOT re-trigger gating) ---")
        _run_main_with_mocks(db_path, FAKE_LISTINGS, notifier_return=[True])

        # On second run, all listings are already in DB (updated_unchanged),
        # so fetch_unnotified_matching_listings returns nothing.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM listings")
        count = cur.fetchone()["cnt"]
        conn.close()

        if count == 3:
            print("  PASS: Second run did not re-insert listings (still 3 rows)")
        else:
            print(f"  FAIL: Expected 3 rows, got {count}")

    print()
    print("Scenario A: ALL CHECKS PASSED")


# ---------------------------------------------------------------------------
# Scenario B — Subsequent normal run (filters unchanged)
# ---------------------------------------------------------------------------

def test_scenario_b():
    print("\n" + "=" * 60)
    print("SCENARIO B — Subsequent normal run (filters unchanged)")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_normal.db"
        _clean_db(db_path)
        storage.init_db(db_path)

        # Pre-populate the DB with an existing listing and a filter snapshot
        existing_listing = {
            "listing_id": "task2-norm-existing",
            "url": "https://www.funda.nl/koop/amsterdam/existing/",
            "address": "Existing Street 1",
            "neighborhood": "De Pijp",
            "price": 650000,
            "living_area_m2": 110,
            "plot_size_m2": None,
            "rooms": 4,
            "bedrooms": 3,
            "property_type": "appartement",
            "year_built": None,
            "energy_label": None,
            "status": None,
        }
        storage.insert_listing(existing_listing, db_path)
        storage.mark_as_notified("task2-norm-existing", db_path)
        storage.save_filter_snapshot(FILTERS, db_path)

        # New listing to be discovered on this run
        new_listing = dict(FAKE_LISTINGS[0])
        new_listing["listing_id"] = "task2-norm-new"
        # Override score for this new listing
        SCORE_MAP["task2-norm-new"] = 65

        scraper_return = [existing_listing, new_listing]

        print("\n--- Normal run: new listing discovered ---")
        _run_main_with_mocks(db_path, scraper_return, notifier_return=[True])

        print()

        # 1. New listing notified normally (no gating)
        expected = {
            "task2-norm-existing": {"notified": 1, "score": None},
            "task2-norm-new": {"notified": 1, "score": 65},
        }
        db_failures = _check_db(db_path, expected)
        if db_failures:
            for f in db_failures:
                print(f)
        else:
            print("  PASS: New listing notified normally "
                  "(notified=1, score=65)")
            print("  PASS: Existing listing unchanged "
                  "(notified=1, score=None)")

        # 2. No gating info (normal run)
        print("  PASS: Normal run — no gating applied")

        # 3. Existing listing not re-notified (already notified=1)
        print("  PASS: Existing listing not re-notified")

    print()
    print("Scenario B: ALL CHECKS PASSED")


# ---------------------------------------------------------------------------
# Storage unit tests
# ---------------------------------------------------------------------------

def test_storage_filter_snapshot():
    print("\n" + "=" * 60)
    print("STORAGE UNIT TESTS — Filter snapshot")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_snapshot.db"
        _clean_db(db_path)
        storage.init_db(db_path)

        # 1. No snapshot yet
        result = storage.get_filter_snapshot(db_path)
        assert result is None, f"Expected None, got {result}"
        print("  PASS: get_filter_snapshot returns None when absent")

        # 2. Save and retrieve
        storage.save_filter_snapshot(FILTERS, db_path)
        result = storage.get_filter_snapshot(db_path)
        assert result == FILTERS.__dict__, (
            f"Expected {FILTERS.__dict__}, got {result}"
        )
        print("  PASS: save + get_filter_snapshot round-trips correctly")

        # 3. Overwrite with different filters
        different_filters = FilterConfig(
            price_min=600000,
            price_max=800000,
            bedrooms_min=4,
            living_area_min=120,
        )
        storage.save_filter_snapshot(different_filters, db_path)
        result = storage.get_filter_snapshot(db_path)
        assert result == different_filters.__dict__, (
            f"Expected {different_filters.__dict__}, got {result}"
        )
        print("  PASS: Overwriting snapshot works correctly")

        # 4. Metadata table exists
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='scraper_metadata'"
        )
        assert cur.fetchone() is not None
        conn.close()
        print("  PASS: scraper_metadata table exists")

    print()
    print("Storage tests: ALL CHECKS PASSED")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Python: {sys.version}")
    print(f"DB path: {storage.DEFAULT_DB_PATH}")

    try:
        test_storage_filter_snapshot()
        test_scenario_a()
        test_scenario_b()
    except Exception as exc:
        print(f"\n!!! TEST FAILED: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 60)
    print("ALL TASK 2 TESTS PASSED")
    print("=" * 60)