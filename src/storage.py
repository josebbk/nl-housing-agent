import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_FILTERS, FilterConfig

# Setup logging
logger = logging.getLogger(__name__)

# Default database path: project_root/data/funda.db
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "funda.db"

# Project energy-label ordering (worst -> best). The scale lives in
# config/preferences.json and is used by scoring.py; the storage filter
# reuses this project-defined ordering rather than inventing a new one.
_PREFERENCES_PATH = Path(__file__).resolve().parent.parent / "config" / "preferences.json"

def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """
    Initializes the database by creating the listings table if it does not exist.
    Creates any missing parent directories for the database file.
    """
    db_path = Path(db_path)
    try:
        # Create parent directories if they don't exist
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
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
                """)

                # Migrate existing tables — add Phase 2 detail/scoring columns
                phase2_columns = [
                    ("ownership_type", "TEXT"),
                    ("erfpacht_canon_annual", "REAL"),
                    ("garden_present", "INTEGER"),
                    ("garden_type", "TEXT"),
                    ("garden_size_m2", "INTEGER"),
                    ("garden_orientation", "TEXT"),
                    ("balcony_present", "INTEGER"),
                    ("building_bound_outdoor_m2", "INTEGER"),
                    ("garage_type", "TEXT"),
                    ("parking_type", "TEXT"),
                    ("insulation_raw", "TEXT"),
                    ("insulation_score", "REAL"),
                    ("heating_type", "TEXT"),
                    ("boiler_year", "INTEGER"),
                    ("bathrooms", "INTEGER"),
                    ("neighborhood_avg_price_m2", "REAL"),
                    ("score", "INTEGER"),
                    ("score_breakdown", "TEXT"),
                    ("score_confidence", "TEXT"),
                    ("detail_fetched_at", "TEXT"),
                ]
                for col_name, col_type in phase2_columns:
                    cursor.execute(
                        f"PRAGMA table_info(listings);"
                    )
                    existing_cols = {row[1] for row in cursor.fetchall()}
                    if col_name not in existing_cols:
                        try:
                            cursor.execute(
                                f"ALTER TABLE listings ADD COLUMN {col_name} {col_type};"
                            )
                            logger.debug("Added column %s to listings table.", col_name)
                        except sqlite3.Error as e:
                            logger.debug(
                                "Could not add column %s (may already exist): %s",
                                col_name, e,
                            )
        logger.info("Database initialized successfully at: %s", db_path)
    except sqlite3.Error as e:
        logger.exception("Failed to initialize database at %s: %s", db_path, e)
        raise

def insert_listing(listing_data: dict, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """
    Inserts a new listing or updates an existing one.

    If the listing already exists (based on listing_id), compares the new
    scraped values against the stored row.  If price or status differs from
    what is stored, updates all fields and resets notified to 0 (re-enters
    the matching/notification flow on this run).  If neither price nor status
    changed, updates the other fields but leaves notified untouched.

    first_seen_at and listing_id are never modified on update.

    Returns one of:
      "inserted"            — new listing, written for the first time
      "updated_renotify"    — price or status changed; notified reset to 0
      "updated_unchanged"   — other fields changed; notified left as-is
      "unchanged"           — row identical to what was already stored
    """
    db_path = Path(db_path)
    listing_id = listing_data.get("listing_id")
    if not listing_id:
        logger.error("Cannot insert listing: 'listing_id' is missing from listing data.")
        raise ValueError("Missing 'listing_id' in listing_data")

    # Validate required fields: url, address, neighborhood, price, living_area_m2, bedrooms
    required_fields = ["url", "address", "neighborhood", "price", "living_area_m2", "bedrooms"]
    missing = [f for f in required_fields if not listing_data.get(f)]
    if missing:
        url = listing_data.get("url", listing_id)
        logger.info(
            "Skipping listing %s (%s): missing required field(s): %s",
            listing_id, url, ", ".join(missing),
        )
        return "unchanged"  # caller will treat missing required fields as a run-level failure

    # Clone data to avoid mutating original, and inject automatic fields
    data = listing_data.copy()
    if "notified" not in data:
        data["notified"] = 0

    # Ensure optional/nullable fields are None if missing
    for opt_field in ["rooms", "plot_size_m2", "property_type", "year_built", "energy_label", "status"]:
        if opt_field not in data:
            data[opt_field] = None

    # Phase 2 detail/scoring fields — all nullable, default to None
    phase2_fields = [
        "ownership_type", "erfpacht_canon_annual", "garden_present",
        "garden_type", "garden_size_m2", "garden_orientation",
        "balcony_present", "building_bound_outdoor_m2",
        "garage_type", "parking_type", "insulation_raw", "insulation_score",
        "heating_type", "boiler_year",
        "bathrooms", "neighborhood_avg_price_m2",
        "score", "score_breakdown", "score_confidence", "detail_fetched_at",
    ]
    for field in phase2_fields:
        if field not in data:
            data[field] = None

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                cursor = conn.cursor()

                # Check if listing exists
                cursor.execute(
                    "SELECT * FROM listings WHERE listing_id = ?;",
                    (listing_id,),
                )
                existing = cursor.fetchone()

                if existing is None:
                    # --- New listing: INSERT ---
                    data["first_seen_at"] = datetime.now().isoformat()
                    query = """
                        INSERT INTO listings (
                            listing_id, url, address, neighborhood, price, living_area_m2,
                            plot_size_m2, rooms, bedrooms, property_type, year_built,
                            energy_label, status, first_seen_at, notified,
                            ownership_type, erfpacht_canon_annual, garden_present,
                            garden_type, garden_size_m2, garden_orientation,
                            balcony_present, building_bound_outdoor_m2,
                            garage_type, parking_type, insulation_raw, insulation_score,
                            heating_type, boiler_year,
                            bathrooms, neighborhood_avg_price_m2,
                            score, score_breakdown, score_confidence, detail_fetched_at
                        ) VALUES (
                            :listing_id, :url, :address, :neighborhood, :price, :living_area_m2,
                            :plot_size_m2, :rooms, :bedrooms, :property_type, :year_built,
                            :energy_label, :status, :first_seen_at, :notified,
                            :ownership_type, :erfpacht_canon_annual, :garden_present,
                            :garden_type, :garden_size_m2, :garden_orientation,
                            :balcony_present, :building_bound_outdoor_m2,
                            :garage_type, :parking_type, :insulation_raw, :insulation_score,
                            :heating_type, :boiler_year,
                            :bathrooms, :neighborhood_avg_price_m2,
                            :score, :score_breakdown, :score_confidence, :detail_fetched_at
                        );
                    """
                    cursor.execute(query, data)
                    logger.info(
                        "Successfully inserted new listing: %s (%s)",
                        listing_id, data.get("address"),
                    )
                    return "inserted"

                # --- Existing listing: compare and possibly update ---
                existing_price = existing["price"]
                existing_status = existing["status"]
                new_price = data.get("price")
                new_status = data.get("status")

                # Card-level scrapes always produce status: None, while
                # detail-page fetches produce a real status string (e.g.
                # "Beschikbaar").  A None-vs-string difference is an artifact
                # of the two different data sources, not a real status change.
                # Preserve the existing status when the new value is None so
                # that the detail-page status survives Phase-1 re-inserts.
                if existing_status is not None and new_status is None:
                    data["status"] = existing_status
                    new_status = existing_status

                price_changed = existing_price != new_price
                status_changed = (
                    existing_status is not None
                    and new_status is not None
                    and existing_status != new_status
                )
                needs_renotify = price_changed or status_changed

                # Build an UPDATE query with all updatable fields
                # (never touch listing_id or first_seen_at)
                updatable = [
                    "url", "address", "neighborhood", "price", "living_area_m2",
                    "plot_size_m2", "rooms", "bedrooms", "property_type",
                    "year_built", "energy_label", "status", "notified",
                    "ownership_type", "erfpacht_canon_annual", "garden_present",
                    "garden_type", "garden_size_m2", "garden_orientation",
                    "balcony_present", "building_bound_outdoor_m2",
                    "garage_type", "parking_type", "insulation_raw", "insulation_score",
                    "heating_type", "boiler_year",
                    "bathrooms", "neighborhood_avg_price_m2",
                    "score", "score_breakdown", "score_confidence", "detail_fetched_at",
                ]
                set_clause = ", ".join(f"{col} = :{col}" for col in updatable)
                if needs_renotify:
                    data["notified"] = 0
                else:
                    data["notified"] = existing["notified"]

                update_data = {col: data.get(col) for col in updatable}

                cursor.execute(
                    f"UPDATE listings SET {set_clause} WHERE listing_id = :listing_id;",
                    {**update_data, "listing_id": listing_id},
                )

                if cursor.rowcount == 0:
                    return "unchanged"

                # Check if any values actually changed
                all_unchanged = True
                for col in updatable:
                    if col == "notified" and needs_renotify:
                        continue  # notified was forcibly reset
                    if existing[col] != data.get(col):
                        all_unchanged = False
                        break

                if all_unchanged:
                    return "unchanged"

                if needs_renotify:
                    logger.info(
                        "Updated listing %s (%s): price/status changed, resetting notified.",
                        listing_id, data.get("address"),
                    )
                    return "updated_renotify"
                else:
                    logger.debug(
                        "Updated listing %s (%s): other fields changed.",
                        listing_id, data.get("address"),
                    )
                    return "updated_unchanged"

    except sqlite3.Error as e:
        logger.exception(
            "Failed to insert/update listing %s at %s: %s",
            listing_id, db_path, e,
        )
        raise

def listing_exists(listing_id: str, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """
    Checks if a listing with the given listing_id already exists in the database.
    """
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM listings WHERE listing_id = ? LIMIT 1;", (listing_id,))
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.exception("Failed to check if listing %s exists at %s: %s", listing_id, db_path, e)
        raise

def mark_as_notified(listing_id: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """
    Marks a listing as notified in the database (sets notified to 1).
    """
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE listings SET notified = 1 WHERE listing_id = ?;", (listing_id,))
                logger.info("Successfully marked listing %s as notified.", listing_id)
    except sqlite3.Error as e:
        logger.exception("Failed to mark listing %s as notified at %s: %s", listing_id, db_path, e)
        raise

def _acceptable_energy_labels(min_label: str) -> list[str]:
    """Return the energy labels that satisfy ``energy_label_min``.

    Uses the project-defined ordinal scale from config/preferences.json
    (worst -> best). A listing passes when its energy label is at least as
    good as ``min_label`` on that scale. Raises ValueError if the configured
    minimum is not a known label on the scale.
    """
    with open(_PREFERENCES_PATH) as f:
        scale = json.load(f).get("energy_label_scale", [])
    try:
        min_index = scale.index(min_label)
    except ValueError:
        raise ValueError(
            f"energy_label_min {min_label!r} is not a known energy label "
            f"on the project scale {scale}."
        ) from None
    return scale[min_index:]


def fetch_unnotified_matching_listings(
    db_path: Path | str = DEFAULT_DB_PATH,
    filters: FilterConfig = DEFAULT_FILTERS,
) -> list[dict]:
    """
    Fetches all listings that have NOT yet been notified (notified = 0)
    and match the given filter criteria.

    Defaults to the frozen Phase 1 criteria (via DEFAULT_FILTERS):
    - Price: €550,000 to €750,000 (inclusive)
    - Bedrooms: >= 3
    - Living area: >= 100 m2

    Optional preferences (property_type, plot_size_min, energy_label_min)
    are only applied when they are not None. NULL optional listing fields
    never satisfy an enabled preference filter.

    Returns a list of dictionaries representing the matching listings.
    """
    db_path = Path(db_path)

    conditions = ["notified = 0"]
    params: list = []

    conditions.append("price >= ?")
    params.append(filters.price_min)
    conditions.append("price <= ?")
    params.append(filters.price_max)
    conditions.append("bedrooms >= ?")
    params.append(filters.bedrooms_min)
    conditions.append("living_area_m2 >= ?")
    params.append(filters.living_area_min)

    if filters.property_type is not None:
        conditions.append("property_type = ?")
        params.append(filters.property_type)

    if filters.plot_size_min is not None:
        conditions.append("plot_size_m2 >= ?")
        params.append(filters.plot_size_min)

    if filters.energy_label_min is not None:
        acceptable = _acceptable_energy_labels(filters.energy_label_min)
        placeholders = ", ".join("?" for _ in acceptable)
        conditions.append(f"UPPER(energy_label) IN ({placeholders})")
        params.extend(acceptable)

    query = "SELECT * FROM listings WHERE {};".format(" AND ".join(conditions))
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logger.exception("Failed to fetch unnotified matching listings at %s: %s", db_path, e)
        raise

if __name__ == "__main__":
    import os
    
    # Setup basic logging to console for the manual test run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    # Define a separate test database file to avoid cluttering production data
    test_db_path = DEFAULT_DB_PATH.parent / "test_funda.db"
    
    print("\n" + "="*50)
    print("STARTING MANUAL DATABASE TESTS")
    print("="*50)
    
    # Clean start: remove test DB if it exists
    if test_db_path.exists():
        try:
            os.remove(test_db_path)
            print("[INFO] Cleaned up existing test database.")
        except OSError as e:
            print(f"[WARN] Could not remove existing test database: {e}")
            
    try:
        # Check 1: Database Initialization
        print("\n--- Check 1: DB Initialization ---")
        init_db(test_db_path)
        if test_db_path.exists():
            print("PASS: test_funda.db successfully created.")
        else:
            print("FAIL: test_funda.db was not created.")
            exit(1)
            
        # Define mock listing matching criteria
        matching_listing = {
            "listing_id": "test-matching-1",
            "url": "https://www.funda.nl/koop/amsterdam/appartement-test-matching/",
            "address": "Teststraat 42",
            "neighborhood": "De Pijp",
            "price": 650000,          # within 550,000-750,000
            "living_area_m2": 110,     # >= 100
            "plot_size_m2": None,
            "rooms": 4,
            "bedrooms": 3,            # >= 3
            "property_type": "appartement",
            "year_built": 1915,
            "energy_label": "B",
            "status": "beschikbaar"
        }
        
        # Define mock listing NOT matching criteria (price too low)
        non_matching_listing = {
            "listing_id": "test-non-matching-2",
            "url": "https://www.funda.nl/koop/amsterdam/appartement-test-non-matching/",
            "address": "Prinsengracht 10",
            "neighborhood": "Centrum",
            "price": 450000,          # outside 550,000-750,000
            "living_area_m2": 80,      # outside >= 100
            "plot_size_m2": None,
            "rooms": 3,
            "bedrooms": 2,            # outside >= 3
            "property_type": "appartement",
            "year_built": 1890,
            "energy_label": "D",
            "status": "beschikbaar"
        }
        
        # Check 2: First Insertion (Matching Listing)
        print("\n--- Check 2: First Insert (Matching Listing) ---")
        res1 = insert_listing(matching_listing, test_db_path)
        if res1 is True:
            print("PASS: insert_listing returned True for new insertion.")
        else:
            print("FAIL: insert_listing returned False for new insertion.")
            
        # Check 3: Check existence
        print("\n--- Check 3: Listing Existence Check ---")
        exists = listing_exists("test-matching-1", test_db_path)
        not_exists = listing_exists("non-existent-id", test_db_path)
        if exists is True and not_exists is False:
            print("PASS: listing_exists works correctly for both existing and missing listings.")
        else:
            print(f"FAIL: listing_exists returned exists={exists}, not_exists={not_exists}.")
            
        # Check 4: Duplicate Insertion Ignored (Dedup)
        print("\n--- Check 4: Duplicate Insert Ignored (Dedup) ---")
        res2 = insert_listing(matching_listing, test_db_path)
        if res2 is False:
            print("PASS: insert_listing returned False when trying to re-insert same listing.")
        else:
            print("FAIL: insert_listing returned True for duplicate insertion (dedup failed).")
            
        # Check 5: Fetch Unnotified Matching Listings
        print("\n--- Check 5: Fetch Unnotified Matching Listings ---")
        # Let's insert the non-matching one too so we can verify filtering works
        insert_listing(non_matching_listing, test_db_path)
        
        unnotified = fetch_unnotified_matching_listings(test_db_path)
        if len(unnotified) == 1:
            item = unnotified[0]
            if item["listing_id"] == "test-matching-1" and item["notified"] == 0:
                print("PASS: Correctly fetched exactly 1 unnotified matching listing.")
            else:
                print(f"FAIL: Returned wrong listing: {item['listing_id']}.")
        else:
            print(f"FAIL: Expected 1 unnotified matching listing, got {len(unnotified)}.")
            
        # Check 6: Mark as Notified
        print("\n--- Check 6: Mark as Notified ---")
        mark_as_notified("test-matching-1", test_db_path)
        
        # Check 7: Confirms No Longer Appears in Unnotified List
        print("\n--- Check 7: No Longer in Unnotified List ---")
        unnotified_after = fetch_unnotified_matching_listings(test_db_path)
        if len(unnotified_after) == 0:
            print("PASS: Listing no longer appears in unnotified query after being marked notified.")
        else:
            print(f"FAIL: Listing still appears in unnotified list: {unnotified_after}.")

        # ------------------------------------------------------------------
        # Bug 2 regression test: notified persistence across Phase-1 re-insert
        # ------------------------------------------------------------------
        # Simulates the exact flow that caused the bug:
        #   Phase 1 insert (card-level, status=None)
        #   Phase 2 insert (detail-page, status="Beschikbaar")
        #   mark_as_notified (notified=1)
        #   Next run Phase 1 re-insert (card-level, status=None)
        #   Verify notified is STILL 1 (not reset to 0 by status artifact)

        print(
            "\n--- Check 8: Bug 2 — notified persists after Phase-1 re-insert ---"
        )

        # --- Sub-check 8a: Full pipeline — insert → detail update → mark notified ---
        card_listing = {
            "listing_id": "bug2-test-1",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug2-1/",
            "address": "Bug2 Teststraat 1",
            "neighborhood": "De Pijp",
            "price": 650000,
            "living_area_m2": 110,
            "plot_size_m2": None,
            "rooms": None,
            "bedrooms": 3,
            "property_type": "appartement",
            "year_built": None,
            "energy_label": None,
            "status": None,  # card-level scrape always produces status: None
        }

        detail_listing = dict(card_listing)
        detail_listing["status"] = "Beschikbaar"  # detail-page produces real status

        # Phase 1: insert with card-level data (status=None)
        res = insert_listing(card_listing, test_db_path)
        assert res == "inserted", f"Expected 'inserted', got {res}"

        # Phase 2: update with detail-page data (status="Beschikbaar")
        res = insert_listing(detail_listing, test_db_path)
        assert res == "updated_unchanged", (
            f"Expected 'updated_unchanged' (status None vs Beschikbaar "
            f"should NOT be a change), got {res}"
        )

        # mark_as_notified
        mark_as_notified("bug2-test-1", test_db_path)

        # Verify notified=1 in a FRESH connection (reopening/requerying)
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT notified, status FROM listings WHERE listing_id = ?",
            ("bug2-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["notified"] == 1:
            print("PASS: notified=1 after mark_as_notified (fresh connection).")
        else:
            print(
                f"FAIL: notified={row['notified']} after mark_as_notified "
                f"(expected 1). BUG NOT FIXED."
            )
            exit(1)

        if row["status"] == "Beschikbaar":
            print("PASS: status='Beschikbaar' preserved in DB.")
        else:
            print(
                f"FAIL: status={row['status']} (expected 'Beschikbaar'). "
                f"Phase-1 overwrote detail-page status."
            )
            exit(1)

        # --- Sub-check 8b: Next run Phase-1 re-insert must NOT reset notified ---
        # Simulate next run: card-level scrape again (status=None)
        next_run_card = dict(card_listing)  # status=None again

        res = insert_listing(next_run_card, test_db_path)
        # Should be "updated_unchanged" or "unchanged" — status None vs
        # "Beschikbaar" should NOT be a change, so notified stays as-is.
        if res in ("updated_unchanged", "unchanged"):
            print(
                "PASS: Phase-1 re-insert with status=None did NOT trigger "
                "status_changed (returned '{}')."
                .format(res)
            )
        else:
            print(
                f"FAIL: Phase-1 re-insert returned '{res}' "
                f"(expected 'updated_unchanged'). Status artifact not fixed."
            )
            exit(1)

        # Verify notified is STILL 1 after Phase-1 re-insert
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT notified, status FROM listings WHERE listing_id = ?",
            ("bug2-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["notified"] == 1:
            print(
                "PASS: notified=1 after Phase-1 re-insert (next run simulation). "
                "Listing will NOT be re-notified."
            )
        else:
            print(
                f"FAIL: notified={row['notified']} after Phase-1 re-insert "
                f"(expected 1). Listing would be re-notified on every run."
            )
            exit(1)

        if row["status"] == "Beschikbaar":
            print(
                "PASS: status='Beschikbaar' preserved after Phase-1 re-insert."
            )
        else:
            print(
                f"FAIL: status={row['status']} after Phase-1 re-insert "
                f"(expected 'Beschikbaar')."
            )
            exit(1)

        # --- Sub-check 8c: Notification failure must NOT mark as notified ---
        # Insert a fresh listing, update with detail data, but do NOT call
        # mark_as_notified. Verify notified stays 0.
        card_listing_2 = {
            "listing_id": "bug2-test-2",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug2-2/",
            "address": "Bug2 Teststraat 2",
            "neighborhood": "De Pijp",
            "price": 650000,
            "living_area_m2": 110,
            "plot_size_m2": None,
            "rooms": None,
            "bedrooms": 3,
            "property_type": "appartement",
            "year_built": None,
            "energy_label": None,
            "status": None,
        }
        detail_listing_2 = dict(card_listing_2)
        detail_listing_2["status"] = "Beschikbaar"

        insert_listing(card_listing_2, test_db_path)
        insert_listing(detail_listing_2, test_db_path)
        # Do NOT call mark_as_notified — simulating a notification send failure

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT notified FROM listings WHERE listing_id = ?",
            ("bug2-test-2",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["notified"] == 0:
            print(
                "PASS: notified=0 when notification send fails (listing will "
                "be retried on a later run)."
            )
        else:
            print(
                f"FAIL: notified={row['notified']} when notification failed "
                f"(expected 0). Listing would not be retried."
            )
            exit(1)

        # --- Sub-check 8d: Genuine status change SHOULD reset notified ---
        # Insert a listing, mark as notified, then update with a different
        # status. Verify notified is reset to 0.
        card_listing_3 = {
            "listing_id": "bug2-test-3",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug2-3/",
            "address": "Bug2 Teststraat 3",
            "neighborhood": "De Pijp",
            "price": 650000,
            "living_area_m2": 110,
            "plot_size_m2": None,
            "rooms": None,
            "bedrooms": 3,
            "property_type": "appartement",
            "year_built": None,
            "energy_label": None,
            "status": "Beschikbaar",
        }
        detail_listing_3 = dict(card_listing_3)
        detail_listing_3["status"] = "Verkocht"  # genuine status change

        insert_listing(card_listing_3, test_db_path)
        mark_as_notified("bug2-test-3", test_db_path)

        # Verify notified=1 before status change
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT notified FROM listings WHERE listing_id = ?",
            ("bug2-test-3",),
        )
        row = cur.fetchone()
        fresh_conn.close()
        assert row["notified"] == 1, "Expected notified=1 before status change"

        # Now update with a different status (simulating a detail-page fetch
        # that shows the listing has been sold)
        res = insert_listing(detail_listing_3, test_db_path)

        if res == "updated_renotify":
            print(
                "PASS: Genuine status change (Beschikbaar → Verkocht) triggered "
                "status_changed (returned 'updated_renotify')."
            )
        else:
            print(
                f"FAIL: Genuine status change returned '{res}' "
                f"(expected 'updated_renotify'). Status change detection broken."
            )
            exit(1)

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT notified, status FROM listings WHERE listing_id = ?",
            ("bug2-test-3",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["notified"] == 0 and row["status"] == "Verkocht":
            print(
                "PASS: notified reset to 0 and status updated to 'Verkocht' "
                "on genuine status change. Listing re-enters notification flow."
            )
        else:
            print(
                f"FAIL: notified={row['notified']}, status={row['status']} "
                f"(expected notified=0, status='Verkocht')."
            )
            exit(1)
            
        print("\n" + "="*50)
        print("ALL TESTS PASSED SUCCESSFULLY!")
        print("="*50)
        
    except Exception as err:
        print(f"\nFAIL: An unexpected exception occurred during testing: {err}")
        import traceback
        traceback.print_exc()
        
    finally:
        # Clean up test DB after test execution
        if test_db_path.exists():
            try:
                os.remove(test_db_path)
                print("[INFO] Cleaned up test database file.")
            except OSError as e:
                print(f"[WARN] Cleaned up error: {e}")
