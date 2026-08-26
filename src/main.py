"""Orchestrate scraper → storage → filter → notification into the Phase 1 scrape flow.

main() composes small, ordered orchestration stages so the run pipeline is
visible at a glance:

    configuration -> scan mode -> scrape -> persist -> match
                  -> score/gate -> notify -> finalise

Each stage helper owns one concern; cross-cutting policies (failure alerts,
exit codes, stats bookkeeping, logging) stay visible in the run functions.
Business logic stays in its owning component: filters in config.py /
storage.py, scraping in scraper.py, scoring in scoring.py.
"""

import argparse
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONSTRUCTION_PERIOD_MAP, FilterConfig, RetentionConfig
from .scraper import scrape_funda
from .detail_scraper import fetch_listing_details
from .scoring import score_listing, load_preferences
from . import storage
from .storage import (
    init_db,
    insert_listing,
    fetch_unnotified_matching_listings,
    mark_as_notified,
    get_filter_snapshot,
    save_filter_snapshot,
    get_last_successful_run,
    save_last_successful_run,
    archive_stale_listings,
)
from .notifier import send_notifications, send_failure_alert, send_listing_notification

logger = logging.getLogger(__name__)

# Default database path, anchored to the project root so it does not depend on
# the process working directory.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "funda.db"

# Task 2 — full-scan notification gating threshold: newly inserted listings
# scoring below this are NOT notified but ARE marked notified=1 so they do
# not linger and get notified later purely because they were skipped.
GATING_THRESHOLD = 70


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


# ---------------------------------------------------------------------------
# Orchestration stage helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanMode:
    """Scan-mode decision for one run (full scan vs delta scan)."""

    run_is_full_scan: bool
    first_run_after_filter_change: bool
    stale_fallback: bool


@dataclass(frozen=True)
class InsertResult:
    """Outcome of persisting one batch of scraped listings."""

    newly_inserted_ids: frozenset
    new_count: int
    updated_count: int
    skipped_count: int
    required_field_failures: int
    errors: tuple


def _load_configuration() -> tuple:
    """Load search filters and retention policy from the human-editable
    config files (config/filters.json, config/retention.json).

    This is the single source of truth for filter values, shared by the
    scraper call and the storage matching query. Raises ValueError when a
    file is missing or invalid — intentionally aborting the run before
    anything is scraped or written.
    """
    filters = FilterConfig.from_file()

    # Load retention / stale-listing archival policy.
    retention = RetentionConfig.from_file()
    return filters, retention


def _determine_scan_mode(
    db_path: str, filters: FilterConfig, run_start: datetime,
) -> ScanMode:
    """Detect whether this run is a FULL or DELTA scan.

    Combines first-run-after-filter-change detection and staleness fallback
    into a single run_is_full_scan decision.
    """
    prev_snapshot = get_filter_snapshot(db_path)
    current_snapshot = filters.__dict__
    is_first_run_after_filter_change = (
        prev_snapshot is None or current_snapshot != prev_snapshot
    )

    last_successful_run_str = get_last_successful_run(db_path)
    is_stale_fallback = True
    if last_successful_run_str is not None:
        try:
            parsed_last = datetime.fromisoformat(last_successful_run_str)
            if parsed_last.tzinfo is None:
                parsed_last = parsed_last.replace(tzinfo=timezone.utc)
            is_stale_fallback = (
                run_start - parsed_last > timedelta(days=3)
            )
        except (ValueError, TypeError):
            is_stale_fallback = True

    scan_mode = ScanMode(
        run_is_full_scan=(
            is_first_run_after_filter_change or is_stale_fallback
        ),
        first_run_after_filter_change=is_first_run_after_filter_change,
        stale_fallback=is_stale_fallback,
    )

    # Determine the reason for a full scan (for logging)
    if scan_mode.run_is_full_scan:
        reasons = []
        if scan_mode.first_run_after_filter_change:
            reasons.append("filter changed")
        if scan_mode.stale_fallback:
            reasons.append("stale fallback (>3 days since last successful run)")
        reason_str = " and ".join(reasons)
        logger.info(
            "SCAN MODE: FULL SCAN — %s", reason_str
        )
    else:
        logger.info("SCAN MODE: DELTA SCAN — filters unchanged, last run within 3 days")

    if scan_mode.first_run_after_filter_change:
        logger.info(
            "First run after filter change detected. "
            "New listings will be score-gated at 70 on this run only."
        )

    return scan_mode


def _resolve_scan_parameters(scan_mode: ScanMode) -> tuple:
    """Map the scan mode onto scraper publication/paging parameters.

    Full scan: no publication filter, 5-page cap (existing behaviour).
    Delta scan: 3-day publication filter, 15-page safety ceiling.
    Returns (publication_date_days, max_pages).
    """
    if scan_mode.run_is_full_scan:
        return None, 5
    return 3, 15


def _setup_logging() -> None:
    """Attach the shared file + console log handlers."""
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


def _insert_listings_into_storage(listings: list, db_path: str) -> InsertResult:
    """Persist scraped listings via storage.insert_listing, classifying each
    row as inserted / updated_unchanged / unchanged / skipped.

    Shared by the standard scan and the seed run; callers apply their own
    failure policy to required_field_failures and errors.
    """
    newly_inserted_ids = set()
    new_count = updated_count = skipped_count = required_field_failures = 0
    errors = []

    for listing in listings:
        try:
            result = insert_listing(listing, db_path)
            if result == "inserted":
                new_count += 1
                newly_inserted_ids.add(listing["listing_id"])
            elif result == "updated_unchanged":
                updated_count += 1
            elif result == "unchanged":
                # Could be missing required fields or truly identical
                if not listing.get("listing_id"):
                    # Already logged as error in storage.py
                    pass
                elif not listing.get("url") or not listing.get("address") or \
                     not listing.get("neighborhood") or not listing.get("price") or \
                     not listing.get("living_area_m2") or not listing.get("bedrooms"):
                    required_field_failures += 1
                else:
                    skipped_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            logger.error(
                "Failed to insert listing %s: %s",
                listing.get("listing_id", "?"),
                exc,
                exc_info=True,
            )
            errors.append(f"Insert {listing.get('listing_id', '?')}: {exc}")

    return InsertResult(
        newly_inserted_ids=frozenset(newly_inserted_ids),
        new_count=new_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        required_field_failures=required_field_failures,
        errors=tuple(errors),
    )


def _score_and_persist_listing(
    listing: dict, preferences: dict, filters: FilterConfig, db_path: str,
):
    """Detail-page fetch + scoring + persistence for ONE matched listing.

    Shared core of the standard scan, seed, and backfill loops. Merges the
    scraped detail fields into ``listing`` BEFORE persisting so scoring has
    access to all fields (price, living_area_m2, ownership_type, ...).
    Returns ``(listing, ScoreResult)``; raises on failure so callers keep
    their own per-run failure policy.
    """
    detail = fetch_listing_details(listing["url"])
    # Must do listing.update(detail) BEFORE detail.update(listing)-style
    # merges because listing (from DB row) has phase2 columns that are NULL.
    listing.update(detail)
    result = score_listing(listing, preferences, filters)

    # Persist detail fields + score to the database row
    if result.score is not None:
        listing["score"] = result.score
    if result.breakdown:
        listing["score_breakdown"] = json.dumps(result.breakdown)
    listing["score_confidence"] = result.confidence

    insert_listing(listing, db_path)
    return listing, result


def _apply_full_scan_gate(
    scored_listings: list,
    newly_inserted_ids: set,
    scan_mode: ScanMode,
    db_path: str,
    stats: dict,
) -> list:
    """Task 2 — full-scan notification gating.

    On a full scan, newly inserted listings that score below
    GATING_THRESHOLD are NOT notified but are marked notified=1 anyway.
    Returns the listings that passed the gate (to be notified normally).
    """
    gate_passed = []
    for listing in scored_listings:
        listing_id = listing.get("listing_id", "?")
        if (scan_mode.run_is_full_scan
                and listing_id in newly_inserted_ids
                and listing.get("score") is not None
                and listing["score"] < GATING_THRESHOLD):
            # Score below threshold — suppress notification but mark notified
            stats["newly_suppressed"] += 1
            logger.info(
                "First-run gate: suppressing notification for newly "
                "inserted listing %s (score=%d < %d); "
                "marking notified=1.",
                listing_id, listing["score"], GATING_THRESHOLD,
            )
            try:
                mark_as_notified(listing_id, db_path)
            except Exception as exc:
                logger.error(
                    "Failed to mark %s as notified after gating: %s",
                    listing_id, exc, exc_info=True,
                )
                stats["errors"].append(f"Mark {listing_id}: {exc}")
        else:
            gate_passed.append(listing)
    return gate_passed


def _finalise_run(
    filters: FilterConfig,
    retention: RetentionConfig,
    db_path: str,
    run_start: datetime,
    stats: dict,
) -> None:
    """Post-notification housekeeping and accounting (steps 6–8)."""
    # --- 6. Summary ---
    # Persist the current filter snapshot so the next run can detect changes.
    try:
        save_filter_snapshot(filters, db_path)
    except Exception as exc:
        logger.error("Failed to save filter snapshot: %s", exc, exc_info=True)

    # --- 6.5. Stale-listing archival ---
    # Best-effort housekeeping: move listings whose last_seen_at is older
    # than retention.stale_days into listings_archive.  Non-fatal — a
    # failure here is logged but does NOT abort the run or trigger the
    # failure-alert path.  Runs identically in dry-run and normal mode.
    try:
        stats["listings_archived"] = archive_stale_listings(db_path, retention)
    except Exception as exc:
        logger.error("Failed to archive stale listings: %s", exc, exc_info=True)
        stats["listings_archived"] = 0

    # --- 7. Summary ---
    _log_run_summary(run_start, stats)

    # --- 8. Persist last_successful_run (only on genuine success) ---
    # Save the completion timestamp only if no notifications failed.
    # Failure paths (_send_failure_alert_and_exit) never write this timestamp.
    if stats["notifications_failed"] == 0:
        try:
            save_last_successful_run(datetime.now(timezone.utc), db_path)
        except Exception as exc:
            logger.error("Failed to save last_successful_run: %s", exc, exc_info=True)

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


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run
    db_path = args.db_path
    do_backfill = args.backfill
    do_seed = args.seed

    # --- 1. Configuration ---
    filters, retention = _load_configuration()

    # --- Run start timestamp (computed ONCE at top, before any "now" reference) ---
    run_start = datetime.now(timezone.utc)
    start_iso = run_start.isoformat()

    # --- 2. Scan mode (drives publication-date filter and paging) ---
    scan_mode = _determine_scan_mode(db_path, filters, run_start)

    # --- 3. Logging setup ---
    _setup_logging()

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
        "matching_listings": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "skipped_listings": 0,
        "required_field_failures": 0,
        "errors": [],
        # Scan-mode tracking
        "run_is_full_scan": scan_mode.run_is_full_scan,
        "is_first_run_after_filter_change": scan_mode.first_run_after_filter_change,
        "is_stale_fallback": scan_mode.stale_fallback,
        "newly_suppressed": 0,
        "newly_notified": 0,
        "listings_archived": 0,
    }

    logger.info("=" * 60)
    logger.info("Run started at %s", start_iso)
    logger.info("Dry-run mode: %s", dry_run)
    logger.info("Database: %s", db_path)

    # --- 4. Initialise database ---
    try:
        init_db(db_path)
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        stats["errors"].append(f"DB init: {exc}")
        _send_failure_alert_and_exit(run_start, stats)

    # --- 5. Scrape (scan-mode parameters from configuration + scan mode) ---
    scan_publication_date_days, scan_max_pages = _resolve_scan_parameters(scan_mode)

    # transaction_type is a search-level filter: it maps to Funda's offering
    # type (koop = for sale, huur = rent). When unset it defaults to "koop",
    # preserving the Phase 1 for-sale behavior.
    offering_type = filters.transaction_type or "koop"
    construction_periods = (
        [CONSTRUCTION_PERIOD_MAP[k] for k in filters.construction_periods]
        if filters.construction_periods is not None
        else None
    )
    try:
        listings = scrape_funda(
            area="amsterdam",
            offering_type=offering_type,
            price_min=filters.price_min,
            price_max=filters.price_max,
            floor_area_min=filters.living_area_min,
            floor_area_max=filters.living_area_max,
            bedrooms_min=filters.bedrooms_min,
            bedrooms_max=filters.bedrooms_max,
            rooms_min=filters.rooms_min,
            rooms_max=filters.rooms_max,
            radius_km=filters.radius_km,
            construction_type=filters.construction_type,
            energy_labels=filters.energy_labels,
            construction_periods=construction_periods,
            garden=filters.garden,
            garden_size_min=filters.garden_size_min,
            availability=filters.availability,
            sort=filters.sort,
            publication_date_days=scan_publication_date_days,
            max_pages=scan_max_pages,
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

    # --- 6. Insert new listings into DB (persistence) ---
    insert_result = _insert_listings_into_storage(listings, db_path)
    newly_inserted_ids = insert_result.newly_inserted_ids
    stats["new_listings"] = insert_result.new_count
    stats["updated_listings"] = insert_result.updated_count
    stats["skipped_listings"] = insert_result.skipped_count
    stats["errors"].extend(insert_result.errors)

    # Required-field failures mean the scraper broke (extraction is unreliable)
    # Treat the run as failed so the cron failure-alert mechanism triggers.
    if insert_result.required_field_failures > 0:
        logger.error(
            "%d listing(s) discarded due to missing required fields. "
            "The scraper extraction is likely broken — treating run as failed.",
            insert_result.required_field_failures,
        )
        stats["errors"].append(
            f"{insert_result.required_field_failures} required-field extraction failures"
        )
        _send_failure_alert_and_exit(run_start, stats)

    # --- 7. Fetch unnotified matching listings (filters applied in storage) ---
    try:
        matching = fetch_unnotified_matching_listings(db_path, filters=filters)
        stats["matching_listings"] = len(matching)
    except Exception as exc:
        logger.error("Failed to fetch matching listings: %s", exc, exc_info=True)
        stats["errors"].append(f"Fetch matching: {exc}")
        _send_failure_alert_and_exit(run_start, stats)

    # --- 8. Detail-page fetch + scoring (Phase 2) ---
    # Only listings that are new/updated AND already pass Phase 1 filters
    # get a detail-page fetch — this bounds the extra request volume.
    preferences = load_preferences()
    scored_listings = []
    for listing in matching:
        listing_id = listing.get("listing_id", "?")
        try:
            listing, result = _score_and_persist_listing(
                listing, preferences, filters, db_path
            )
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
                listing_id,
                exc,
            )
            scored_listings.append(listing)

    # --- 9. Send notifications; mark each listing notified only on success ---
    if dry_run:
        logger.info("Dry-run: skipping %d notification(s)", len(scored_listings))
    else:
        # Separate listings into gate-passed and gate-suppressed
        # Gating applies whenever run_is_full_scan is True (filter change
        # or staleness fallback), not only on first-run-after-filter-change.
        gate_passed = _apply_full_scan_gate(
            scored_listings, newly_inserted_ids, scan_mode, db_path, stats
        )

        try:
            results = send_notifications(gate_passed)
        except Exception as exc:
            logger.error("Notification batch failed: %s", exc, exc_info=True)
            stats["errors"].append(f"Notifications: {exc}")
            results = [False] * len(gate_passed)

        for listing, success in zip(gate_passed, results):
            listing_id = listing.get("listing_id", "?")
            if success:
                try:
                    mark_as_notified(listing_id, db_path)
                    stats["notifications_sent"] += 1
                    stats["newly_notified"] += 1
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

    # --- 10. Finalise: snapshot, archival, summary, success bookkeeping ---
    _finalise_run(filters, retention, db_path, run_start, stats)


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
            listing, result = _score_and_persist_listing(
                listing, preferences, filters, db_path
            )

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
    offering_type = filters.transaction_type or "koop"
    construction_periods = (
        [CONSTRUCTION_PERIOD_MAP[k] for k in filters.construction_periods]
        if filters.construction_periods is not None
        else None
    )
    try:
        listings = scrape_funda(
            area="amsterdam",
            offering_type=offering_type,
            price_min=filters.price_min,
            price_max=filters.price_max,
            floor_area_min=filters.living_area_min,
            floor_area_max=filters.living_area_max,
            bedrooms_min=filters.bedrooms_min,
            bedrooms_max=filters.bedrooms_max,
            rooms_min=filters.rooms_min,
            rooms_max=filters.rooms_max,
            radius_km=filters.radius_km,
            construction_type=filters.construction_type,
            energy_labels=filters.energy_labels,
            construction_periods=construction_periods,
            garden=filters.garden,
            garden_size_min=filters.garden_size_min,
            availability=filters.availability,
            sort=filters.sort,
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

    # --- 2. Insert listings into DB (shared persistence stage) ---
    insert_result = _insert_listings_into_storage(listings, db_path)
    new_count = insert_result.new_count
    updated_count = insert_result.updated_count
    required_failures = insert_result.required_field_failures

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
        listing_id = listing.get("listing_id", "?")
        try:
            listing, result = _score_and_persist_listing(
                listing, preferences, filters, db_path
            )
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
                listing_id,
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
    logger.info("  Skipped:        %d", stats["skipped_listings"])
    logger.info("  Matching:       %d", stats["matching_listings"])
    logger.info("  Notified:       %d", stats["notifications_sent"])
    logger.info("  Notify failed:  %d", stats["notifications_failed"])
    logger.info("  Archived:       %d", stats.get("listings_archived", 0))

    # Scan-mode info
    if stats.get("run_is_full_scan"):
        reasons = []
        if stats.get("is_first_run_after_filter_change"):
            reasons.append("filter changed")
        if stats.get("is_stale_fallback"):
            reasons.append("stale fallback")
        reason_str = " and ".join(reasons) if reasons else "full scan"
        logger.info("  Scan mode:      FULL (%s)", reason_str)
    else:
        logger.info("  Scan mode:      DELTA (3-day publication filter)")

    # Task 2 / scan-mode gating info — applies whenever run_is_full_scan is True
    if stats.get("run_is_full_scan"):
        logger.info("  Full-run gate:  ENABLED")
        logger.info(
            "  Newly suppressed (<70):  %d",
            stats.get("newly_suppressed", 0),
        )
        logger.info(
            "  Newly notified (>=70):   %d",
            stats.get("newly_notified", 0),
        )

    if stats["errors"]:
        for err in stats["errors"]:
            logger.warning("  Error:          %s", err)
    logger.info("-" * 40)
    logger.info("Run completed")


if __name__ == "__main__":
    main()
