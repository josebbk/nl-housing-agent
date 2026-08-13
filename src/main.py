"""Orchestrate scraper → storage → filter → notification into the Phase 1 scrape flow."""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .scraper import scrape_funda
from .storage import (
    init_db,
    insert_listing,
    fetch_unnotified_matching_listings,
    mark_as_notified,
)
from .notifier import send_notifications, send_failure_alert

logger = logging.getLogger(__name__)

# Default database path, anchored to the project root so it does not depend on
# the process working directory.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "funda.db"

# Phase 1 filter values. These configure the Funda search URL built by
# scraper.py. The authoritative filter evaluation lives in storage.py's
# fetch_unnotified_matching_listings(); there is no second filter
# implementation here.
PRICE_MIN = 550_000
PRICE_MAX = 750_000
BEDROOMS_MIN = 3
LIVING_AREA_MIN = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amsterdam Funda housing scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scraping/filtering/DB writes but skip sending Telegram messages",
    )
    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help="Path to the SQLite database (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run
    db_path = args.db_path

    # --- Logging setup ---
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "scraper.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # --- Run metadata ---
    run_start = datetime.now(timezone.utc)
    start_iso = run_start.isoformat()
    stats = {
        "listings_scraped": 0,
        "new_listings": 0,
        "matching_listings": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "skipped_listings": 0,
        "errors": [],
    }

    logger.info("=" * 60)
    logger.info("Run started at %s", start_iso)
    logger.info("Dry-run mode: %s", dry_run)
    logger.info("Database: %s", db_path)

    # --- 1. Initialise database ---
    try:
        init_db(db_path)
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        stats["errors"].append(f"DB init: {exc}")
        _send_failure_alert_and_exit(run_start, stats)

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
        _send_failure_alert_and_exit(run_start, stats)

    # A real Amsterdam search with these filters never legitimately returns zero
    # listings. A zero-result scrape usually means Funda served an anti-bot
    # interstitial (Akamai/reCAPTCHA) or the page structure changed, so fail the
    # run loudly instead of reporting a successful empty run.
    if not listings:
        logger.error(
            "Scrape returned 0 listings. This may indicate an Akamai/Funda "
            "block, a reCAPTCHA challenge, or a page-structure change. "
            "Treating the run as failed."
        )
        stats["errors"].append("Scrape returned 0 listings (possible block)")
        _send_failure_alert_and_exit(run_start, stats)

    # --- 3. Insert new listings into DB ---
    for listing in listings:
        try:
            inserted = insert_listing(listing, db_path)
            if inserted:
                stats["new_listings"] += 1
            elif not listing.get("listing_id"):
                # Already logged as error in storage.py
                pass
            else:
                stats["skipped_listings"] += 1
        except Exception as exc:
            logger.error(
                "Failed to insert listing %s: %s",
                listing.get("listing_id", "?"),
                exc,
                exc_info=True,
            )
            stats["errors"].append(f"Insert {listing.get('listing_id', '?')}: {exc}")

    # --- 4. Fetch unnotified matching listings (filters applied in storage) ---
    try:
        matching = fetch_unnotified_matching_listings(db_path)
        stats["matching_listings"] = len(matching)
    except Exception as exc:
        logger.error("Failed to fetch matching listings: %s", exc, exc_info=True)
        stats["errors"].append(f"Fetch matching: {exc}")
        _send_failure_alert_and_exit(run_start, stats)

    # --- 5. Send notifications; mark each listing notified only on success ---
    if dry_run:
        logger.info("Dry-run: skipping %d notification(s)", len(matching))
    else:
        try:
            results = send_notifications(matching)
        except Exception as exc:
            logger.error("Notification batch failed: %s", exc, exc_info=True)
            stats["errors"].append(f"Notifications: {exc}")
            results = [False] * len(matching)

        for listing, success in zip(matching, results):
            listing_id = listing.get("listing_id", "?")
            if success:
                try:
                    mark_as_notified(listing_id, db_path)
                    stats["notifications_sent"] += 1
                except Exception as exc:
                    logger.error(
                        "Notification sent but failed to mark %s as notified: %s",
                        listing_id,
                        exc,
                        exc_info=True,
                    )
                    stats["errors"].append(f"Mark {listing_id}: {exc}")
            else:
                stats["notifications_failed"] += 1
                logger.error(
                    "Notification failed for listing %s; it stays unnotified "
                    "and will be retried on a later run.",
                    listing_id,
                )

    # --- 6. Summary ---
    _log_run_summary(run_start, stats)

    # A run is failed when a notification could not be delivered; affected
    # listings remain unnotified so a later run retries them safely.
    if stats["notifications_failed"] > 0:
        alert_msg = (
            f"<b>⚠️ Funda scraper run failed:</b> "
            f"{stats['notifications_failed']} notification(s) could not be delivered. "
            f"Check logs/cron.log and logs/scraper.log."
        )
        try:
            send_failure_alert(alert_msg)
        except Exception as exc:
            logger.error("Failed to send failure alert: %s", exc)
        sys.exit(1)


def _send_failure_alert_and_exit(run_start: datetime, stats: dict) -> None:
    """Send a Telegram failure alert, log the summary, then exit with code 1.

    The alert send is wrapped in try/except so it never crashes the script.
    """
    reason = "; ".join(stats["errors"]) if stats["errors"] else "unknown"
    alert_msg = f"<b>⚠️ Funda scraper run failed:</b> {reason}. Check logs/cron.log and logs/scraper.log."
    try:
        send_failure_alert(alert_msg)
    except Exception as exc:
        logger.error("Failed to send failure alert: %s", exc)

    _log_run_summary(run_start, stats)
    sys.exit(1)


def _log_run_summary(run_start: datetime, stats: dict) -> None:
    """Log the end-of-run statistics block."""
    run_end = datetime.now(timezone.utc)
    duration = (run_end - run_start).total_seconds()

    logger.info("-" * 40)
    logger.info("Run summary:")
    logger.info("  Start:          %s", run_start.isoformat())
    logger.info("  End:            %s", run_end.isoformat())
    logger.info("  Duration:       %.1fs", duration)
    logger.info("  Scraped:        %d", stats["listings_scraped"])
    logger.info("  New:            %d", stats["new_listings"])
    logger.info("  Skipped:        %d", stats["skipped_listings"])
    logger.info("  Matching:       %d", stats["matching_listings"])
    logger.info("  Notified:       %d", stats["notifications_sent"])
    logger.info("  Notify failed:  %d", stats["notifications_failed"])
    if stats["errors"]:
        for err in stats["errors"]:
            logger.warning("  Error:          %s", err)
    logger.info("-" * 40)
    logger.info("Run completed")


if __name__ == "__main__":
    main()
