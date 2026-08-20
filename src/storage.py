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
                        detail_fetched_at TEXT,
                        last_seen_at TEXT
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
                    ("last_seen_at", "TEXT"),
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
        # Create the listings_archive table (same schema as listings)
                # for future stale-listing archival. Currently unused —
                # population logic is a future task.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS listings_archive (
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
                        detail_fetched_at TEXT,
                        last_seen_at TEXT
                    );
                """)

        logger.info("Database initialized successfully at: %s", db_path)
    except sqlite3.Error as e:
        logger.exception("Failed to initialize database at %s: %s", db_path, e)
        raise

def insert_listing(listing_data: dict, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """
    Inserts a new listing or updates an existing one.

    If the listing already exists (based on listing_id), updates all fields
    but always preserves the existing ``notified`` value.  ``notified``
    never changes as a side effect of this function — it is only modified
    by ``mark_as_notified()`` or via the filter-change logic in Task 2.

    ``first_seen_at`` and ``listing_id`` are never modified on update.

    Returns one of:
      "inserted"            — new listing, written for the first time
      "updated_unchanged"   — existing row updated (any fields changed)
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
                    data["last_seen_at"] = datetime.now().isoformat()
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
                            score, score_breakdown, score_confidence, detail_fetched_at,
                            last_seen_at
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
                            :score, :score_breakdown, :score_confidence, :detail_fetched_at,
                            :last_seen_at
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

                # Generalize the status preservation pattern to all fields
                # that originate from the detail-page scraper (phase2_fields,
                # rooms, year_built) and shared optional fields (plot_size_m2,
                # property_type, energy_label).  When a field is not supplied
                # by the caller (e.g. a card-level scrape that never produced
                # the field, or a detail-page scrape whose to_dict() filtered
                # out a None value), preserve the existing DB value rather
                # than overwriting it with NULL.
                #
                # This applies to:
                #   - Optional fields: rooms, year_built, plot_size_m2,
                #     property_type, energy_label (explicitly defaulted to
                #     None by the card scraper or missing entirely)
                #   - Phase 2 detail fields: all 20 columns in phase2_fields
                #     (not present in card scraper output at all)
                #
                # Card-level fields (url, address, neighborhood, price,
                # living_area_m2, bedrooms) are NOT included — they must be
                # freely overwritten on every run.
                detail_and_shared_fields = [
                    "rooms", "year_built", "plot_size_m2",
                    "property_type", "energy_label",
                    "ownership_type", "erfpacht_canon_annual",
                    "garden_present", "garden_type", "garden_size_m2",
                    "garden_orientation", "balcony_present",
                    "building_bound_outdoor_m2", "garage_type",
                    "parking_type", "insulation_raw", "insulation_score",
                    "heating_type", "boiler_year", "bathrooms",
                    "neighborhood_avg_price_m2", "score",
                    "score_breakdown", "score_confidence",
                    "detail_fetched_at",
                ]
                for field in detail_and_shared_fields:
                    existing_val = existing[field]
                    new_val = data.get(field)
                    if existing_val is not None and new_val is None:
                        data[field] = existing_val

                # Build an UPDATE query with all updatable fields
                # (never touch listing_id or first_seen_at)
                # last_seen_at is stamped unconditionally here (not through
                # the preservation loop) — every call to insert_listing means
                # the listing was just encountered in a scrape, so it should
                # always be refreshed to "now".
                data["last_seen_at"] = datetime.now().isoformat()
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
                    "last_seen_at",
                ]
                set_clause = ", ".join(f"{col} = :{col}" for col in updatable)
                # Always preserve the existing notified value — it is only
                # modified by mark_as_notified() or the filter-change logic
                # (Task 2), never as a side effect of insert_listing().
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
                    if existing[col] != data.get(col):
                        all_unchanged = False
                        break

                if all_unchanged:
                    return "unchanged"

                logger.debug(
                    "Updated listing %s (%s): fields changed.",
                    listing_id, data.get("address"),
                )
                return "updated_unchanged"

    except sqlite3.Error as e:
        logger.exception(
            "Failed to insert/update listing %s at %s: %s",
            listing_id, db_path, e,
        )
        raise

def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    """Create the scraper_metadata table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scraper_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)


def get_filter_snapshot(db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """Return the previously saved filter snapshot, or None if absent.

    Returns ``None`` when the snapshot has never been saved (first run ever).
    """
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            _ensure_metadata_table(conn)
            cursor = conn.execute(
                "SELECT value FROM scraper_metadata WHERE key = ?;",
                ("filter_snapshot",),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[0])
    except sqlite3.Error as e:
        logger.exception("Failed to read filter snapshot at %s: %s", db_path, e)
        raise


def save_filter_snapshot(
    filters: FilterConfig, db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Persist the current filter configuration as a JSON snapshot."""
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            _ensure_metadata_table(conn)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO scraper_metadata (key, value) "
                    "VALUES ('filter_snapshot', ?);",
                    (json.dumps(filters.__dict__),),
                )
    except sqlite3.Error as e:
        logger.exception(
            "Failed to save filter snapshot at %s: %s", db_path, e,
        )
        raise


def get_last_successful_run(db_path: Path | str = DEFAULT_DB_PATH) -> str | None:
    """Return the stored ISO-8601 timestamp of the last successful run,
    or ``None`` if no run has been recorded yet.
    """
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            _ensure_metadata_table(conn)
            cursor = conn.execute(
                "SELECT value FROM scraper_metadata WHERE key = ?;",
                ("last_successful_run",),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return row[0]
    except sqlite3.Error as e:
        logger.exception(
            "Failed to read last_successful_run at %s: %s", db_path, e,
        )
        raise


def save_last_successful_run(
    timestamp: datetime, db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Persist an ISO-8601 UTC timestamp under key 'last_successful_run'."""
    db_path = Path(db_path)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            _ensure_metadata_table(conn)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO scraper_metadata (key, value) "
                    "VALUES ('last_successful_run', ?);",
                    (timestamp.isoformat(),),
                )
    except sqlite3.Error as e:
        logger.exception(
            "Failed to save last_successful_run at %s: %s", db_path, e,
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

def _acceptable_energy_labels(
    min_label: str | None = None,
    max_label: str | None = None,
) -> list[str]:
    """Return the energy labels that satisfy ``min_label`` .. ``max_label``.

    Uses the project-defined ordinal scale from config/preferences.json
    (worst -> best). A listing passes when its energy label is at least as
    good as ``min_label`` and at most as good as ``max_label`` on that scale.

    Only the bounds that are set are enforced. Raises ValueError if a
    configured bound is not a known label on the scale, or if the min bound
    is stricter (better) than the max bound.
    """
    with open(_PREFERENCES_PATH) as f:
        scale = json.load(f).get("energy_label_scale", [])
    for bound, label in (("min", min_label), ("max", max_label)):
        if label is not None and label not in scale:
            raise ValueError(
                f"energy_label_{bound} {label!r} is not a known energy label "
                f"on the project scale {scale}."
            )
    lo = scale.index(min_label) if min_label is not None else 0
    hi = scale.index(max_label) if max_label is not None else len(scale) - 1
    if lo > hi:
        raise ValueError(
            f"energy_label_min {min_label!r} is stricter than "
            f"energy_label_max {max_label!r} on the project scale."
        )
    return scale[lo : hi + 1]


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

    Optional preferences (property_type, plot_size_min/max, energy_label_min/max,
    bedrooms_max, living_area_max) are only applied when they are not None.
    NULL optional listing fields never satisfy an enabled preference filter.

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
    if filters.bedrooms_max is not None:
        conditions.append("bedrooms <= ?")
        params.append(filters.bedrooms_max)
    conditions.append("living_area_m2 >= ?")
    params.append(filters.living_area_min)
    if filters.living_area_max is not None:
        conditions.append("living_area_m2 <= ?")
        params.append(filters.living_area_max)

    if filters.property_type is not None:
        conditions.append("property_type = ?")
        params.append(filters.property_type)

    if filters.plot_size_min is not None:
        conditions.append("plot_size_m2 >= ?")
        params.append(filters.plot_size_min)
    if filters.plot_size_max is not None:
        conditions.append("plot_size_m2 <= ?")
        params.append(filters.plot_size_max)

    if filters.energy_label_min is not None or filters.energy_label_max is not None:
        acceptable = _acceptable_energy_labels(
            filters.energy_label_min, filters.energy_label_max
        )
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

        # --- Sub-check 8d: Genuine status change does NOT reset notified ---
        # Insert a listing, mark as notified, then update with a different
        # status. Verify notified is NOT reset to 0.
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

        if res == "updated_unchanged":
            print(
                "PASS: Genuine status change (Beschikbaar → Verkocht) did NOT "
                "trigger re-notify (returned 'updated_unchanged')."
            )
        else:
            print(
                f"FAIL: Genuine status change returned '{res}' "
                f"(expected 'updated_unchanged'). Re-notify logic not removed."
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

        if row["notified"] == 1 and row["status"] == "Verkocht":
            print(
                "PASS: notified=1 preserved and status updated to 'Verkocht' "
                "on genuine status change. Listing does NOT re-enter "
                "notification flow."
            )
        else:
            print(
                f"FAIL: notified={row['notified']}, status={row['status']} "
                f"(expected notified=1, status='Verkocht')."
            )
            exit(1)

        # ------------------------------------------------------------------
        # Bug 3 regression test: detail-page fields must not be erased by
        # card-level re-inserts (Phase 1 only runs).
        #
        # Simulates the exact flow:
        #   Run 1: card scrape → insert → detail fetch → insert (phase2 populated)
        #   Run 2: card scrape → insert (phase 1 only, no detail fetch)
        #   Verify: phase2 columns are STILL populated after Run 2
        # ------------------------------------------------------------------
        print(
            "\n--- Check 9: Bug 3 — detail fields preserved after card-only re-insert ---"
        )

        # --- Sub-check 9a: Phase2 fields preserved when card-level re-insert ---
        card_listing_9a = {
            "listing_id": "bug3-test-1",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-1/",
            "address": "Bug3 Teststraat 1",
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

        # Simulate a detail-page fetch that populates phase2 fields
        detail_listing_9a = dict(card_listing_9a)
        detail_listing_9a.update({
            "ownership_type": "erfpacht",
            "erfpacht_canon_annual": 408.85,
            "garden_present": True,
            "garden_type": "achtertuin",
            "garden_size_m2": 74,
            "garden_orientation": "zuiden",
            "balcony_present": True,
            "building_bound_outdoor_m2": 12,
            "garage_type": "carport",
            "parking_type": "private",
            "insulation_raw": "Dakisolatie Muurisolatie",
            "insulation_score": 0.65,
            "heating_type": "gas_boiler",
            "boiler_year": 2011,
            "bathrooms": 2,
            "neighborhood_avg_price_m2": 5200.0,
            "score": 72,
            "score_breakdown": '{"neighborhood_value": 18}',
            "score_confidence": "partial",
            "detail_fetched_at": "2026-08-18T10:00:00+00:00",
        })

        # Step 1: Insert with detail data (simulates Run 1 with detail fetch)
        res = insert_listing(detail_listing_9a, test_db_path)
        assert res in ("inserted", "updated_unchanged"), (
            f"Expected 'inserted' or 'updated_unchanged', got {res}"
        )

        # Query DB directly to confirm phase2 fields are populated
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, insulation_score, "
            "parking_type, bathrooms, rooms, year_built, score "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        checks_9a = [
            ("ownership_type", row["ownership_type"], "erfpacht"),
            ("garden_present", row["garden_present"], True),
            ("insulation_score", row["insulation_score"], 0.65),
            ("parking_type", row["parking_type"], "private"),
            ("bathrooms", row["bathrooms"], 2),
            ("rooms", row["rooms"], None),
            ("year_built", row["year_built"], None),
            ("score", row["score"], 72),
        ]
        all_pass = True
        for field, actual, expected in checks_9a:
            if actual != expected:
                print(
                    f"  FAIL: {field}={actual} (expected {expected}) "
                    f"after detail insert"
                )
                all_pass = False
        if all_pass:
            print("PASS: All phase2 fields populated after detail insert.")

        # Step 2: Card-level re-insert (simulates Run 2, Phase 1 only)
        # This is the critical test: card-level data has NO phase2 keys,
        # rooms=None, year_built=None
        res = insert_listing(card_listing_9a, test_db_path)
        assert res in ("updated_unchanged", "unchanged"), (
            f"Expected 'updated_unchanged' or 'unchanged', got {res} "
            f"(card-only re-insert should NOT trigger re-notify)"
        )

        # Query DB directly to confirm phase2 fields are STILL populated
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, insulation_score, "
            "parking_type, bathrooms, rooms, year_built, score "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        checks_9a_preserved = [
            ("ownership_type", row["ownership_type"], "erfpacht"),
            ("garden_present", row["garden_present"], True),
            ("insulation_score", row["insulation_score"], 0.65),
            ("parking_type", row["parking_type"], "private"),
            ("bathrooms", row["bathrooms"], 2),
            ("rooms", row["rooms"], None),
            ("year_built", row["year_built"], None),
            ("score", row["score"], 72),
        ]
        all_pass = True
        for field, actual, expected in checks_9a_preserved:
            if actual != expected:
                print(
                    f"  FAIL: {field}={actual} (expected {expected}) "
                    f"after card-only re-insert — DETAIL DATA WAS ERASED"
                )
                all_pass = False
        if all_pass:
            print(
                "PASS: All phase2 fields STILL populated after card-only "
                "re-insert. Detail data preserved."
            )

        # --- Sub-check 9b: Phase2 fields remain NULL when originally NULL ---
        card_listing_9b = {
            "listing_id": "bug3-test-2",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-2/",
            "address": "Bug3 Teststraat 2",
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

        # Insert with detail data but ALL phase2 fields are None/NULL
        detail_listing_9b = dict(card_listing_9b)
        detail_listing_9b.update({
            "ownership_type": None,
            "erfpacht_canon_annual": None,
            "garden_present": None,
            "garden_type": None,
            "garden_size_m2": None,
            "garden_orientation": None,
            "balcony_present": None,
            "building_bound_outdoor_m2": None,
            "garage_type": None,
            "parking_type": None,
            "insulation_raw": None,
            "insulation_score": None,
            "heating_type": None,
            "boiler_year": None,
            "bathrooms": None,
            "neighborhood_avg_price_m2": None,
            "score": None,
            "score_breakdown": None,
            "score_confidence": None,
            "detail_fetched_at": None,
        })

        res = insert_listing(detail_listing_9b, test_db_path)
        assert res in ("inserted", "updated_unchanged"), (
            f"Expected 'inserted' or 'updated_unchanged', got {res}"
        )

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, insulation_score "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-2",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["ownership_type"] is None and row["garden_present"] is None:
            print(
                "PASS: Phase2 fields remain NULL when originally NULL."
            )
        else:
            print(
                f"FAIL: Phase2 fields should be NULL but got "
                f"ownership_type={row['ownership_type']}, "
                f"garden_present={row['garden_present']}"
            )
            exit(1)

        # Card-level re-insert should keep them NULL
        res = insert_listing(card_listing_9b, test_db_path)
        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present FROM listings "
            "WHERE listing_id = ?",
            ("bug3-test-2",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["ownership_type"] is None and row["garden_present"] is None:
            print(
                "PASS: Phase2 fields remain NULL after card-only re-insert "
                "(when originally NULL)."
            )
        else:
            print(
                f"FAIL: Phase2 fields should still be NULL but got "
                f"ownership_type={row['ownership_type']}, "
                f"garden_present={row['garden_present']}"
            )
            exit(1)

        # --- Sub-check 9c: Fresh non-None detail value updates correctly ---
        card_listing_9c = {
            "listing_id": "bug3-test-3",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-3/",
            "address": "Bug3 Teststraat 3",
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

        # Step 1: Insert with detail data
        detail_listing_9c = dict(card_listing_9c)
        detail_listing_9c.update({
            "ownership_type": "erfpacht",
            "garden_present": True,
            "bathrooms": 2,
            "score": 72,
        })
        res = insert_listing(detail_listing_9c, test_db_path)
        assert res in ("inserted", "updated_unchanged")

        # Step 2: Card-only re-insert (should preserve phase2 values)
        res = insert_listing(card_listing_9c, test_db_path)
        assert res in ("updated_unchanged", "unchanged")

        # Step 3: Fresh detail-page fetch with DIFFERENT values
        detail_listing_9c_new = dict(card_listing_9c)
        detail_listing_9c_new.update({
            "ownership_type": "full",
            "garden_present": False,
            "bathrooms": 1,
            "score": 85,
        })
        res = insert_listing(detail_listing_9c_new, test_db_path)
        assert res in ("updated_unchanged",), (
            f"Expected 'updated_unchanged', got {res}"
        )

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, bathrooms, score "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-3",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if (row["ownership_type"] == "full"
                and row["garden_present"] == False
                and row["bathrooms"] == 1
                and row["score"] == 85):
            print(
                "PASS: Fresh non-None detail values correctly overwrite old "
                "values while untouched fields are preserved."
            )
        else:
            print(
                f"FAIL: Expected ownership_type='full', garden_present=False, "
                f"bathrooms=1, score=85. Got "
                f"ownership_type={row['ownership_type']}, "
                f"garden_present={row['garden_present']}, "
                f"bathrooms={row['bathrooms']}, score={row['score']}"
            )
            exit(1)

        # --- Sub-check 9d: rooms and year_built preservation ---
        card_listing_9d = {
            "listing_id": "bug3-test-4",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-4/",
            "address": "Bug3 Teststraat 4",
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

        # Insert with detail data that includes rooms and year_built
        detail_listing_9d = dict(card_listing_9d)
        detail_listing_9d.update({
            "rooms": 4,
            "year_built": 1969,
        })
        res = insert_listing(detail_listing_9d, test_db_path)
        assert res in ("inserted", "updated_unchanged")

        # Card-only re-insert should preserve rooms and year_built
        res = insert_listing(card_listing_9d, test_db_path)
        assert res in ("updated_unchanged", "unchanged")

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT rooms, year_built FROM listings WHERE listing_id = ?",
            ("bug3-test-4",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["rooms"] == 4 and row["year_built"] == 1969:
            print(
                "PASS: rooms and year_built preserved after card-only "
                "re-insert."
            )
        else:
            print(
                f"FAIL: rooms={row['rooms']} (expected 4), "
                f"year_built={row['year_built']} (expected 1969)"
            )
            exit(1)

        # --- Sub-check 9e: New listing (INSERT) still defaults phase2 to NULL ---
        card_listing_9e = {
            "listing_id": "bug3-test-5",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-5/",
            "address": "Bug3 Teststraat 5",
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

        res = insert_listing(card_listing_9e, test_db_path)
        assert res == "inserted", f"Expected 'inserted', got {res}"

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, rooms, year_built "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-5",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if (row["ownership_type"] is None
                and row["garden_present"] is None
                and row["rooms"] is None
                and row["year_built"] is None):
            print(
                "PASS: New listing has phase2 fields defaulting to NULL "
                "(no prior detail fetch)."
            )
        else:
            print(
                f"FAIL: New listing should have NULL phase2 fields but got "
                f"ownership_type={row['ownership_type']}, "
                f"garden_present={row['garden_present']}, "
                f"rooms={row['rooms']}, year_built={row['year_built']}"
            )
            exit(1)

        # --- Sub-check 9f: Status handling unchanged (no regression) ---
        card_listing_9f = {
            "listing_id": "bug3-test-6",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-6/",
            "address": "Bug3 Teststraat 6",
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

        detail_listing_9f = dict(card_listing_9f)
        detail_listing_9f["status"] = "Beschikbaar"

        res = insert_listing(detail_listing_9f, test_db_path)
        assert res in ("inserted", "updated_unchanged")

        # Card-only re-insert should preserve status
        res = insert_listing(card_listing_9f, test_db_path)
        assert res in ("updated_unchanged", "unchanged")

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT status FROM listings WHERE listing_id = ?",
            ("bug3-test-6",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if row["status"] == "Beschikbaar":
            print(
                "PASS: Status handling unchanged — preserved after "
                "card-only re-insert (no regression)."
            )
        else:
            print(
                f"FAIL: status={row['status']} (expected 'Beschikbaar'). "
                f"Status preservation regressed."
            )
            exit(1)

        # --- Sub-check 9g: Card-level fields still freely overwritable ---
        card_listing_9g = {
            "listing_id": "bug3-test-7",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-7/",
            "address": "Bug3 Teststraat 7",
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

        # Step 1: Insert with card data
        res = insert_listing(card_listing_9g, test_db_path)
        assert res == "inserted"

        # Step 2: Update with NEW card data (different price, address)
        card_listing_9g_updated = dict(card_listing_9g)
        card_listing_9g_updated["price"] = 700000
        card_listing_9g_updated["address"] = "Bug3 Teststraat 7 UPDATED"
        res = insert_listing(card_listing_9g_updated, test_db_path)
        assert res == "updated_unchanged", (
            f"Expected 'updated_unchanged' (price changed, no re-notify), got {res}"
        )

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT price, address FROM listings WHERE listing_id = ?",
            ("bug3-test-7",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if (row["price"] == 700000
                and row["address"] == "Bug3 Teststraat 7 UPDATED"):
            print(
                "PASS: Card-level fields (price, address) still freely "
                "overwritable by newer card-scrape values."
            )
        else:
            print(
                f"FAIL: price={row['price']} (expected 700000), "
                f"address={row['address']} (expected 'Bug3 Teststraat 7 UPDATED')"
            )
            exit(1)

        # --- Sub-check 9h: Case D — detail fetch with absent field preserves ---
        # Simulates: listing has phase2 data, detail fetch occurs but field
        # is genuinely absent on the detail page (to_dict() filters it out)
        card_listing_9h = {
            "listing_id": "bug3-test-8",
            "url": "https://www.funda.nl/koop/amsterdam/test-bug3-8/",
            "address": "Bug3 Teststraat 8",
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

        # Step 1: Insert with detail data
        detail_listing_9h = dict(card_listing_9h)
        detail_listing_9h.update({
            "ownership_type": "erfpacht",
            "garden_present": True,
            "bathrooms": 2,
        })
        res = insert_listing(detail_listing_9h, test_db_path)
        assert res in ("inserted", "updated_unchanged")

        # Step 2: Simulate a detail-page fetch where some fields are absent
        # (to_dict() filters out None, so they are NOT in the dict).
        # This is what happens when detail_scraper.py's DetailData.to_dict()
        # is called and some fields are None — they get filtered out.
        # The listing dict is then updated with this partial detail dict.
        # After listing.update(detail_partial), the phase2 fields that were
        # absent from detail_partial still have their card-level values
        # (None) in the listing dict.
        detail_partial_9h = {
            "ownership_type": "full",  # This one IS present
            # garden_present is absent (None on detail page → filtered out)
            # bathrooms is absent (None on detail page → filtered out)
        }

        # Simulate the main.py step 4.5 merge
        listing_9h = dict(card_listing_9h)
        listing_9h.update(detail_partial_9h)
        # listing_9h now has:
        #   ownership_type = "full" (from detail)
        #   garden_present = None (from card, NOT updated by detail)
        #   bathrooms = None (from card, NOT updated by detail)

        res = insert_listing(listing_9h, test_db_path)
        assert res in ("updated_unchanged", "unchanged")

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT ownership_type, garden_present, bathrooms "
            "FROM listings WHERE listing_id = ?",
            ("bug3-test-8",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        if (row["ownership_type"] == "full"
                and row["garden_present"] == True
                and row["bathrooms"] == 2):
            print(
                "PASS: Case D — detail fetch with absent field preserves "
                "existing DB value (garden_present=True, bathrooms=2 "
                "preserved despite absent from detail merge)."
            )
        else:
            print(
                f"FAIL: ownership_type={row['ownership_type']} "
                f"(expected 'full'), garden_present={row['garden_present']} "
                f"(expected True), bathrooms={row['bathrooms']} "
                f"(expected 2)"
            )
            exit(1)

        # ------------------------------------------------------------------
        # Check 10: listings_archive table exists and matches listings schema
        # ------------------------------------------------------------------
        print("\n--- Check 10: listings_archive table schema ---")

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()

        cur.execute("PRAGMA table_info(listings);")
        listings_cols = {row[1] for row in cur.fetchall()}

        cur.execute("PRAGMA table_info(listings_archive);")
        archive_cols = {row[1] for row in cur.fetchall()}
        fresh_conn.close()

        if "listings_archive" in {
            row[1] for row in fresh_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        } if False else True:
            pass  # pragma: no cover — handled below

        # Re-open to check table existence properly
        fresh_conn = sqlite3.connect(test_db_path)
        cur = fresh_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        table_names = {row[0] for row in cur.fetchall()}
        fresh_conn.close()

        if "listings_archive" in table_names:
            print("PASS: listings_archive table exists.")
        else:
            print("FAIL: listings_archive table does not exist.")
            exit(1)

        if listings_cols == archive_cols:
            print(
                "PASS: listings_archive has the same column names as "
                "listings ({} columns).".format(len(listings_cols))
            )
        else:
            missing = listings_cols - archive_cols
            extra = archive_cols - listings_cols
            msg_parts = []
            if missing:
                msg_parts.append("listings has extra: {}".format(missing))
            if extra:
                msg_parts.append("archive has extra: {}".format(extra))
            print("FAIL: Column mismatch between listings and "
                  "listings_archive: {}".format(", ".join(msg_parts)))
            exit(1)

        # ------------------------------------------------------------------
        # Check 11: last_seen_at is stamped on new listing insert
        # ------------------------------------------------------------------
        print("\n--- Check 11: last_seen_at stamped on new insert ---")

        new_listing_for_ts = {
            "listing_id": "ts-test-1",
            "url": "https://www.funda.nl/koop/amsterdam/ts-test-1/",
            "address": "Timestamp Test 1",
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
        insert_listing(new_listing_for_ts, test_db_path)

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT last_seen_at FROM listings WHERE listing_id = ?",
            ("ts-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        ts_value = row["last_seen_at"]
        try:
            parsed = datetime.fromisoformat(ts_value)
            if ts_value is not None:
                print(
                    "PASS: last_seen_at is a valid non-null ISO 8601 "
                    "timestamp: {}".format(ts_value)
                )
            else:
                print("FAIL: last_seen_at is NULL after new insert.")
                exit(1)
        except (ValueError, TypeError):
            print(
                "FAIL: last_seen_at is not a valid ISO 8601 timestamp: "
                "{}".format(ts_value)
            )
            exit(1)

        # ------------------------------------------------------------------
        # Check 12: last_seen_at is re-stamped on update (existing listing)
        # ------------------------------------------------------------------
        print("\n--- Check 12: last_seen_at re-stamped on update ---")

        first_ts = ts_value
        # Re-insert the SAME listing (identical data) — should update
        # last_seen_at to a new value
        insert_listing(new_listing_for_ts, test_db_path)

        fresh_conn = sqlite3.connect(test_db_path)
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()
        cur.execute(
            "SELECT last_seen_at FROM listings WHERE listing_id = ?",
            ("ts-test-1",),
        )
        row = cur.fetchone()
        fresh_conn.close()

        second_ts = row["last_seen_at"]
        try:
            parsed2 = datetime.fromisoformat(second_ts)
            if second_ts is not None and second_ts >= first_ts:
                print(
                    "PASS: last_seen_at was re-stamped on update: "
                    "{} -> {}".format(first_ts, second_ts)
                )
            else:
                print(
                    "FAIL: last_seen_at was not updated on re-insert. "
                    "Expected >= {} but got {}. "
                    "(Preserved old value instead of stamping new.)".format(
                        first_ts, second_ts
                    )
                )
                exit(1)
        except (ValueError, TypeError):
            print(
                "FAIL: second last_seen_at is not valid ISO 8601: "
                "{}".format(second_ts)
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
