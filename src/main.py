"""Orchestrate scraper → storage → filter → notification into the Phase 1 scrape flow."""

import argparse
import json
import logging
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import FilterConfig
from .scraper import scrape_funda
from .detail_scraper import fetch_listing_details
from .scoring import score_listing, load_preferences
from . import storage
from .storage import (
    init_db,
    insert_listing,
    fetch_unnotified_matching_listings,
    mark_as_notified,
)
from .notifier import send_notifications, send_failure_alert, send_listing_notification

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
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill scores for listings with score IS NULL using the "
             "current 9-criterion scoring system. Fetches detail pages, "
             "scores, and updates the DB. Uses the same anti-bot pacing as "
             "a normal run.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Populate the database with full real data (scrape, store, "
             "score) without sending any Telegram notifications. All "
             "matching listings are marked notified=1 so a subsequent "
             "normal run only notifies genuinely new/changed listings. "
             "Use for initial population or after a DB reset.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run
    db_path = args.db_path
    do_backfill = args.backfill
    do_seed = args.seed

    # Load search filters from the human-editable filter file
    # (config/filters.json). This is the single source of truth for filter
    # values, shared by the scraper call and the storage matching query below.
    filters = FilterConfig.from_file()

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

    if do_backfill:
        _run_backfill(db_path, filters, dry_run, run_start)
        return

    if do_seed:
        _run_seed(db_path, filters, run_start)
        return

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
            # Merge card-scraped data (price, living_area_m2, bedrooms, etc.)
            # into the detail dict so scoring has access to all fields.
            detail.update(listing)
            result = score_listing(detail, preferences, filters)

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


def _run_backfill(db_path: str, filters: FilterConfig, dry_run: bool, run_start: datetime) -> None:
    """One-time backfill: score listings with score IS NULL.

    Queries listings that pass the active Phase 1 filters and have
    score IS NULL. For each, fetches the detail page, scores with the
    current 9-criterion system, and updates the DB row.

    Notifications are threshold-gated at 80 (from
    config/preferences.json -> notification_score_threshold):
      - score >= 80: send Telegram notification, set notified = 1
      - score < 80: do NOT notify, but set notified = 1 so these don't
        re-enter the notification flow later
    """
    db_path = Path(db_path)

    logger.info("=" * 60)
    logger.info("BACKFILL STARTED at %s", run_start.isoformat())
    logger.info("Dry-run mode: %s", dry_run)
    logger.info("Database: %s", db_path)

    try:
        init_db(db_path)
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        sys.exit(1)

    preferences = load_preferences()
    threshold = preferences.get("notification_score_threshold", 80)
    logger.info("Notification score threshold: %d", threshold)

    # Query ALL listings that pass Phase 1 filters (not just unnotified).
    # We need to score listings regardless of their notified status — the
    # backfill may need to score already-notified listings that lack scores.
    # We reuse the filter conditions from storage.py directly here.
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        cursor = db.cursor()

        conditions = ["price >= ?"]
        params = [filters.price_min]
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
            acceptable = storage._acceptable_energy_labels(filters.energy_label_min)
            placeholders = ", ".join("?" for _ in acceptable)
            conditions.append(f"UPPER(energy_label) IN ({placeholders})")
            params.extend(acceptable)

        query = "SELECT * FROM listings WHERE {} AND score IS NULL;".format(
            " AND ".join(conditions)
        )
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        null_score_listings = [dict(row) for row in rows]
        db.close()
    except Exception as exc:
        logger.error("Failed to fetch listings for backfill: %s", exc, exc_info=True)
        sys.exit(1)
    logger.info("Listings found with score IS NULL: %d", len(null_score_listings))

    if not null_score_listings:
        logger.info("No listings need backfilling. Exiting.")
        return

    backfilled = 0
    notified_count = 0
    sub_80_count = 0
    failures = 0
    scores_log = []

    for listing in null_score_listings:
        listing_id = listing.get("listing_id", "?")
        address = listing.get("address", "?")
        try:
            detail = fetch_listing_details(listing["url"])
            # Merge card-scraped data into the detail dict for scoring
            detail.update(listing)
            result = score_listing(detail, preferences, filters)

            # Merge detail fields + score into the listing dict for storage
            listing.update(detail)
            if result.score is not None:
                listing["score"] = result.score
            if result.breakdown:
                listing["score_breakdown"] = json.dumps(result.breakdown)
            listing["score_confidence"] = result.confidence

            # Persist detail fields + score to the database row
            insert_listing(listing, db_path)

            backfilled += 1
            scores_log.append((listing_id, address, result.score, result.confidence))
            logger.info(
                "Backfilled listing %s (%s): score=%d/100 (%s)",
                listing_id, address,
                result.score if result.score is not None else 0,
                result.confidence,
            )

            # Threshold-gated notification
            if result.score is not None and result.score >= threshold:
                if not dry_run:
                    try:
                        success = send_listing_notification(listing)
                        if success:
                            mark_as_notified(listing_id, db_path)
                            notified_count += 1
                        else:
                            logger.error(
                                "Notification failed for listing %s (score=%d); "
                                "notified=1 set but not re-sent.",
                                listing_id, result.score,
                            )
                            # Still mark as notified so it doesn't re-enter flow
                            mark_as_notified(listing_id, db_path)
                            notified_count += 1
                    except Exception as exc:
                        logger.error(
                            "Notification error for listing %s: %s",
                            listing_id, exc, exc_info=True,
                        )
                        mark_as_notified(listing_id, db_path)
                        notified_count += 1
                else:
                    logger.info(
                        "DRY-RUN: would notify listing %s (score=%d >= %d)",
                        listing_id, result.score, threshold,
                    )
                    notified_count += 1
            else:
                # score < threshold or no score -- still mark notified=1
                # so this listing doesn't re-enter the notification flow
                # through an unrelated trigger (e.g. future price change).
                if result.score is not None:
                    sub_80_count += 1
                    logger.info(
                        "Backfilled listing %s (%s): score=%d/100 (< %d threshold), "
                        "notified=1 set but NO notification sent.",
                        listing_id, address, result.score, threshold,
                    )
                mark_as_notified(listing_id, db_path)

        except Exception as exc:
            failures += 1
            logger.warning(
                "Backfill failed for listing %s (%s): %s",
                listing_id, address, exc,
            )

    # Summary
    logger.info("-" * 40)
    logger.info("Backfill summary:")
    logger.info("  Listings with score IS NULL: %d", len(null_score_listings))
    logger.info("  Successfully backfilled:     %d", backfilled)
    logger.info("  Crossed threshold (>= %d):   %d", threshold, notified_count)
    logger.info("  Below threshold (< %d):      %d", threshold, sub_80_count)
    logger.info("  Failures:                    %d", failures)
    logger.info("-" * 40)

    # Log individual scores
    for listing_id, address, score, confidence in scores_log:
        logger.info("  %s (%s): %d/100 (%s)", listing_id, address, score, confidence)

    logger.info("Backfill completed at %s", datetime.now(timezone.utc).isoformat())


def _run_seed(db_path: str, filters: FilterConfig, run_start: datetime) -> None:
    """Seed run: full LIVE pipeline without notifications.

    Populates the database with real scraped data, scores all matching
    listings, and marks them as notified=1 so a subsequent normal run
    only notifies genuinely new/changed listings.

    This is a real data run — same scraping, same storage, same scoring
    as a normal live run. The only difference is that no Telegram
    notifications are sent and all matching listings are pre-marked
    notified.
    """
    db_path = Path(db_path)

    logger.info("=" * 60)
    logger.info("SEED RUN STARTED at %s", run_start.isoformat())
    logger.info("Database: %s", db_path)
    logger.info(
        "Seed mode: full pipeline, no notifications, "
        "all matching listings marked notified=1"
    )

    try:
        init_db(db_path)
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        sys.exit(1)

    # --- 1. Scrape ---
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
        stats = {"listings_scraped": len(listings)}
    except Exception as exc:
        logger.error("Scraping failed: %s", exc, exc_info=True)
        sys.exit(1)

    if not listings:
        logger.error(
            "Scrape returned 0 listings. This may indicate an Akamai/Funda "
            "block, a reCAPTCHA challenge, or a page-structure change. "
            "Treating the run as failed."
        )
        sys.exit(1)

    logger.info("Scraped %d listings", stats["listings_scraped"])

    # --- 2. Insert listings into DB ---
    new_count = 0
    updated_count = 0
    required_failures = 0

    for listing in listings:
        try:
            result = insert_listing(listing, db_path)
            if result == "inserted":
                new_count += 1
            elif result == "updated_renotify":
                updated_count += 1
            elif result == "updated_unchanged":
                updated_count += 1
            elif result == "unchanged":
                if not listing.get("url") or not listing.get("address") or \
                   not listing.get("neighborhood") or not listing.get("price") or \
                   not listing.get("living_area_m2") or not listing.get("bedrooms"):
                    required_failures += 1
        except Exception as exc:
            logger.error(
                "Failed to insert listing %s: %s",
                listing.get("listing_id", "?"),
                exc,
                exc_info=True,
            )

    if required_failures > 0:
        logger.error(
            "%d listing(s) discarded due to missing required fields.",
            required_failures,
        )
        sys.exit(1)

    logger.info("Inserted %d new, updated %d existing", new_count, updated_count)

    # --- 3. Fetch unnotified matching listings ---
    try:
        matching = fetch_unnotified_matching_listings(db_path, filters=filters)
    except Exception as exc:
        logger.error("Failed to fetch matching listings: %s", exc, exc_info=True)
        sys.exit(1)

    logger.info("Found %d matching listings (unnotified)", len(matching))

    # --- 4. Detail-page fetch + scoring ---
    preferences = load_preferences()
    scored_listings = []
    scored_count = 0
    score_failures = 0

    for listing in matching:
        try:
            detail = fetch_listing_details(listing["url"])
            detail.update(listing)
            result = score_listing(detail, preferences, filters)

            listing.update(detail)
            if result.score is not None:
                listing["score"] = result.score
            if result.breakdown:
                listing["score_breakdown"] = json.dumps(result.breakdown)
            listing["score_confidence"] = result.confidence

            insert_listing(listing, db_path)
            scored_listings.append(listing)
            scored_count += 1
            logger.info(
                "Scored listing %s: %d/100 (%s)",
                listing.get("listing_id"),
                result.score if result.score is not None else 0,
                result.confidence,
            )
        except Exception as exc:
            logger.warning(
                "Detail fetch/scoring failed for listing %s: %s",
                listing.get("listing_id", "?"),
                exc,
            )
            score_failures += 1

    logger.info(
        "Scored %d listings, %d failures", scored_count, score_failures
    )

    # --- 5. Mark all matching listings as notified (no Telegram) ---
    notified_count = 0
    for listing in scored_listings:
        try:
            mark_as_notified(listing["listing_id"], db_path)
            notified_count += 1
        except Exception as exc:
            logger.error(
                "Failed to mark %s as notified: %s",
                listing.get("listing_id", "?"),
                exc,
                exc_info=True,
            )

    # --- 6. Seed summary ---
    run_end = datetime.now(timezone.utc)
    duration = (run_end - run_start).total_seconds()

    logger.info("=" * 60)
    logger.info("SEED RUN COMPLETE")
    logger.info("-" * 40)
    logger.info("  Start:          %s", run_start.isoformat())
    logger.info("  End:            %s", run_end.isoformat())
    logger.info("  Duration:       %.1fs", duration)
    logger.info("  Scraped:        %d", stats["listings_scraped"])
    logger.info("  New inserted:   %d", new_count)
    logger.info("  Updated:        %d", updated_count)
    logger.info("  Matching:       %d", len(matching))
    logger.info("  Scored:         %d", scored_count)
    logger.info("  Marked notified: %d", notified_count)
    logger.info("  Score failures: %d", score_failures)
    logger.info("-" * 40)
    logger.info(
        "SEED RUN — All %d matching listing(s) marked notified=1 "
        "without sending Telegram. A subsequent normal run will only "
        "notify for listings that are genuinely new or changed after this point.",
        notified_count,
    )
    logger.info("Seed run completed at %s", run_end.isoformat())
    logger.info("=" * 60)


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
