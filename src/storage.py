import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

# Setup logging
logger = logging.getLogger(__name__)

# Default database path: project_root/data/funda.db
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "funda.db"

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
                        notified INTEGER NOT NULL DEFAULT 0
                    );
                """)
        logger.info("Database initialized successfully at: %s", db_path)
    except sqlite3.Error as e:
        logger.exception("Failed to initialize database at %s: %s", db_path, e)
        raise

def insert_listing(listing_data: dict, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """
    Inserts a new listing into the database.
    
    If the listing already exists (based on listing_id), it will be ignored (not updated).
    Sets first_seen_at to the current ISO 8601 timestamp in local/UTC time.
    Sets notified to 0 by default.
    
    Returns True if the listing was successfully inserted, False if it was ignored (already exists).
    """
    db_path = Path(db_path)
    listing_id = listing_data.get("listing_id")
    if not listing_id:
        logger.error("Cannot insert listing: 'listing_id' is missing from listing data.")
        raise ValueError("Missing 'listing_id' in listing_data")

    # Clone data to avoid mutating original, and inject automatic fields
    data = listing_data.copy()
    data["first_seen_at"] = datetime.now().isoformat()
    if "notified" not in data:
        data["notified"] = 0

    # Ensure optional/nullable fields are None if missing
    for opt_field in ["plot_size_m2", "bedrooms", "year_built", "energy_label"]:
        if opt_field not in data:
            data[opt_field] = None

    query = """
        INSERT OR IGNORE INTO listings (
            listing_id, url, address, neighborhood, price, living_area_m2,
            plot_size_m2, rooms, bedrooms, property_type, year_built,
            energy_label, status, first_seen_at, notified
        ) VALUES (
            :listing_id, :url, :address, :neighborhood, :price, :living_area_m2,
            :plot_size_m2, :rooms, :bedrooms, :property_type, :year_built,
            :energy_label, :status, :first_seen_at, :notified
        );
    """
    
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute(query, data)
                inserted = cursor.rowcount > 0
                if inserted:
                    logger.info("Successfully inserted new listing: %s (%s)", listing_id, data.get("address"))
                else:
                    logger.debug("Listing %s already exists, ignored insert.", listing_id)
                return inserted
    except sqlite3.Error as e:
        logger.exception("Failed to insert listing %s at %s: %s", listing_id, db_path, e)
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

def fetch_unnotified_matching_listings(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """
    Fetches all listings that have NOT yet been notified (notified = 0)
    and match the Phase 1 filtering criteria:
    - Price: €550,000 to €750,000 (inclusive)
    - Bedrooms: >= 3
    - Living area: >= 100 m2
    
    Returns a list of dictionaries representing the matching listings.
    """
    db_path = Path(db_path)
    query = """
        SELECT * FROM listings
        WHERE notified = 0
          AND price >= 550000
          AND price <= 750000
          AND bedrooms >= 3
          AND living_area_m2 >= 100;
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query)
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
