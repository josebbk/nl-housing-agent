"""Orchestrate scraper → storage → filter → notification into the Phase 1 scrape flow."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import FilterConfig
from .scraper import scrape_funda
from .detail_scraper import fetch_listing_details
from .scoring import score_listing, load_preferences
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

    # Load search filters from the environment (FUNDA_* vars via .env).
    # This is the single source of truth for filter values, shared by the
    # scraper call and the storage matching query below.
    filters = FilterConfig.from_env()

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
        "updated_listings": 0,
        "re_notified_listings": 0,
        "matching_listings": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "skipped_listings": 0,
        "required_field_failures": 0,
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
            price_min=filters.price_min,
            price_max=filters.price_max,
            floor_area_min=filters.living_area_min,
            bedrooms_min=filters.bedrooms_min,
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
            result = insert_listing(listing, db_path)
            if result == "inserted":
                stats["new_listings"] += 1
            elif result == "updated_renotify":
                stats["re_notified_listings"] += 1
                stats["updated_listings"] += 1
            elif result == "updated_unchanged":
                stats["updated_listings"] += 1
            elif result == "unchanged":
                # Could be missing required fields or truly identical
                if not listing.get("listing_id"):
                    # Already logged as error in storage.py
                    pass
                elif not listing.get("url") or not listing.get("address") or \
                     not listing.get("neighborhood") or not listing.get("price") or \
                     not listing.get("living_area_m2") or not listing.get("bedrooms"):
                    stats["required_field_failures"] += 1
                else:
                    stats["skipped_listings"] += 1
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

    # Required-field failures mean the scraper broke (extraction is unreliable)
    # Treat the run as failed so the cron failure-alert mechanism triggers.
    if stats["required_field_failures"] > 0:
        logger.error(
            "%d listing(s) discarded due to missing required fields. "
            "The scraper extraction is likely broken — treating run as failed.",
            stats["required_field_failures"],
        )
        stats["errors"].append(
            f"{stats['required_field_failures']} required-field extraction failures"
        )
        _send_failure_alert_and_exit(run_start, stats)

    # --- 4. Fetch unnotified matching listings (filters applied in storage) ---
    try:
        matching = fetch_unnotified_matching_listings(db_path, filters=filters)
        stats["matching_listings"] = len(matching)
    except Exception as exc:
        logger.error("Failed to fetch matching listings: %s", exc, exc_info=True)
        stats["errors"].append(f"Fetch matching: {exc}")
        _send_failure_alert_and_exit(run_start, stats)

    # --- 4.5. Detail-page fetch + scoring (Phase 2) ---
    # Only listings that are new/updated AND already pass Phase 1 filters
    # get a detail-page fetch — this bounds the extra request volume.
    preferences = load_preferences()
    scored_listings = []
    for listing in matching:
        try:
            detail = fetch_listing_details(listing["url"])
            result = score_listing(detail, preferences)

            # Merge detail fields + score into the listing dict for storage
            listing.update(detail)
            if result.score is not None:
                listing["score"] = result.score
            if result.breakdown:
                listing["score_breakdown"] = json.dumps(result.breakdown)
            listing["score_confidence"] = result.confidence

            # Persist detail fields + score to the database row
            insert_listing(listing, db_path)

            scored_listings.append(listing)
            logger.info(
                "Scored listing %s: %d/100 (%s)",
                listing.get("listing_id"),
                result.score if result.score is not None else 0,
                result.confidence,
            )
        except Exception as exc:
            logger.warning(
                "Detail fetch/scoring failed for listing %s: %s — "
                "falling back to unscored notification",
                listing.get("listing_id", "?"),
                exc,
            )
            scored_listings.append(listing)

    # --- 5. Send notifications; mark each listing notified only on success ---
    if dry_run:
        logger.info("Dry-run: skipping %d notification(s)", len(scored_listings))
        final_listings = []
    else:
        final_listings = scored_listings
        try:
            results = send_notifications(scored_listings)
        except Exception as exc:
            logger.error("Notification batch failed: %s", exc, exc_info=True)
            stats["errors"].append(f"Notifications: {exc}")
            results = [False] * len(scored_listings)

        for listing, success in zip(scored_listings, results):
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
    logger.info("  Updated:        %d", stats["updated_listings"])
    logger.info("  Re-notified:    %d", stats["re_notified_listings"])
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
