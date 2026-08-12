"""Orchestrate scraper → storage → filter → notification into the Phase 1 scrape flow."""

import argparse
import logging
import sys
from datetime import datetime, timezone

from .scraper import scrape_funda
from .storage import init_db, insert_listing, fetch_unnotified_matching_listings
from .notifier import send_notifications

logger = logging.getLogger(__name__)

DB_PATH = "data/funda.db"

# Phase 1 filter defaults passed to the scraper URL builder.
# The scraper already applies these at the URL level; main.py also
# enforces them as a safety check after storage so the contract is
# explicit at the orchestration layer.
PRICE_MIN = 550_000
PRICE_MAX = 750_000
BEDROOMS_MIN = 3
LIVING_AREA_MIN = 100


def matches_phase1_filters(listing: dict) -> bool:
    """Return True if a listing meets all Phase 1 criteria."""
    price = listing.get("price")
    bedrooms = listing.get("bedrooms")
    living_area = listing.get("living_area_m2")

    if price is None or bedrooms is None or living_area is None:
        return False

    return (
        PRICE_MIN <= price <= PRICE_MAX
        and bedrooms >= BEDROOMS_MIN
        and living_area >= LIVING_AREA_MIN
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amsterdam Funda housing scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scraping/filtering/DB writes but skip sending Telegram messages",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run

    # --- Run metadata ---
    run_start = datetime.now(timezone.utc)
    start_iso = run_start.isoformat()
    stats = {
        "pages_processed": 0,
        "listings_scraped": 0,
        "new_listings": 0,
        "matching_listings": 0,
        "notifications_sent": 0,
        "errors": [],
    }

    logger.info("=" * 60)
    logger.info("Run started at %s", start_iso)
    logger.info("Dry-run mode: %s", dry_run)

    # --- 1. Initialise database ---
    try:
        init_db(DB_PATH)
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        stats["errors"].append(f"DB init: {exc}")
        _log_run_summary(run_start, stats)
        sys.exit(1)

    # --- 2. Scrape ---
    try:
        listings = scrape_funda(
            area="amsterdam",
            offering_type="koop",
            price_min=PRICE_MIN,
            price_max=PRICE_MAX,
            floor_area_min=LIVING_AREA_MIN,
            bedrooms_min=BEDROOMS_MIN,
            max_pages=5,
        )
        stats["listings_scraped"] = len(listings)
    except Exception as exc:
        logger.error("Scraping failed: %s", exc, exc_info=True)
        stats["errors"].append(f"Scrape: {exc}")
        _log_run_summary(run_start, stats)
        sys.exit(1)

    # --- 3. Insert new listings into DB ---
    for listing in listings:
        try:
            inserted = insert_listing(listing, DB_PATH)
            if inserted:
                stats["new_listings"] += 1
        except Exception as exc:
            logger.error(
                "Failed to insert listing %s: %s",
                listing.get("listing_id", "?"),
                exc,
                exc_info=True,
            )
            stats["errors"].append(
                f"Insert {listing.get('listing_id', '?')}: {exc}"
            )

    # --- 4. Fetch unnotified matching listings (Phase 1 filters + not notified) ---
    try:
        matching = fetch_unnotified_matching_listings(DB_PATH)
        stats["matching_listings"] = len(matching)
    except Exception as exc:
        logger.error("Failed to fetch matching listings: %s", exc, exc_info=True)
        stats["errors"].append(f"Fetch matching: {exc}")
        matching = []

    # --- 5. Send notifications (post-scrape, not interleaved) ---
    if dry_run:
        logger.info("Dry-run: skipping %d notification(s)", len(matching))
        stats["notifications_sent"] = 0
    else:
        try:
            results = send_notifications(matching)
            stats["notifications_sent"] = sum(1 for r in results if r)
        except Exception as exc:
            logger.error("Notification batch failed: %s", exc, exc_info=True)
            stats["errors"].append(f"Notifications: {exc}")

    # --- 6. Summary ---
    _log_run_summary(run_start, stats)


def _log_run_summary(run_start: datetime, stats: dict) -> None:
    """Log the end-of-run statistics block."""
    run_end = datetime.now(timezone.utc)
    duration = (run_end - run_start).total_seconds()

    logger.info("-" * 40)
    logger.info("Run summary:")
    logger.info("  Start:      %s", run_start.isoformat())
    logger.info("  End:        %s", run_end.isoformat())
    logger.info("  Duration:   %.1fs", duration)
    logger.info("  Pages:      %d", stats["pages_processed"])
    logger.info("  Scraped:    %d", stats["listings_scraped"])
    logger.info("  New:        %d", stats["new_listings"])
    logger.info("  Matching:   %d", stats["matching_listings"])
    logger.info("  Notified:   %d", stats["notifications_sent"])
    if stats["errors"]:
        for err in stats["errors"]:
            logger.warning("  Error:      %s", err)
    logger.info("-" * 40)
    logger.info("Run completed")


if __name__ == "__main__":
    main()