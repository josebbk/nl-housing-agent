"""One-off operational flow: Funda Diemen search -> store -> new Telegram
forum topic -> post a small batch of matching house listings.

This module exists to satisfy an owner-requested task: create a new forum
topic in the existing Telegram supergroup ("Diemen — Funda Matches") and post
a small sample of existing Funda listings that match the owner's uploaded
``Diemen.json`` filters.

It is deliberately NOT part of the scheduled notification pipeline. It:

  * never touches ``config/filters.json`` (the global, scheduled search
    config) — the Diemen criteria live only in the supplied ``Diemen.json``;
  * never runs ``main.main()`` or the cron flow;
  * does not mark any listing as notified, so the normal pipeline's
    deduplication is unaffected;
  * reuses the existing scraper, storage and rich-notification code.

It reuses, unchanged, the project's existing building blocks:

  * ``scrape_funda`` (scraper.py) to discover Diemen/Duivendrecht houses with
    the Diemen filters (mirroring main.py's filter -> scrape mapping);
  * ``insert_listing`` (storage.py) to persist them (card-level) for the
    record and for future analysis;
  * ``fetch_listing_details`` (detail_scraper.py) + ``score_listing`` to
    enrich the small selected batch with metrics, photos and description;
  * ``create_forum_topic`` + ``send_listing_notification`` (notifier.py) to
    create ONE new topic and post each selected listing there (rich HTML
    message + best-effort property photos).

Run manually (NOT scheduled):

    python -m src.diemen_topic                 # default Diemen.json
    python -m src.diemen_topic --filters Diemen.json --max-listings 4
    python -m src.diemen_topic --dry-run       # scrape+select but no Telegram
"""

import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import FilterConfig
from .scraper import scrape_funda
from .detail_scraper import fetch_listing_details
from .scoring import score_listing, load_preferences
from . import storage
from .storage import insert_listing, init_db
from .notifier import create_forum_topic, send_listing_notification

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_FILTERS_PATH = _PROJECT_ROOT / "Diemen.json"
_DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "funda.db"

# Default topic name for the new forum topic.
TOPIC_NAME = "Diemen — Funda Matches"

# Municipality slugs the Diemen selected_area covers. The DB's ``neighborhood``
# column stores the municipality slug from the listing URL (e.g. "diemen" or
# "duivendrecht"); it does NOT store the Funda district slug (wijk-...), so
# the area criterion can only be verified to this granularity.
_DIEMEN_AREA_SLUGS = frozenset({"diemen", "duivendrecht"})

# Number of listings posted by default (owner asked for "several", a small
# sample — never the whole set).
_DEFAULT_MAX_LISTINGS = 4

# The Diemen forum topic created for this purpose (verified live).
# Seed/live posts go ONLY here; the general chat / old flow target is never
# touched by this module.
DIEMEN_TOPIC_ID = "870"

# Dedicated, Diemen-only "already sent to the Diemen topic" ledger, entirely
# separate from the global ``notified`` flag (which belongs to the main.py
# pipeline). This is what lets the seed + live flow dedup without ever
# disturbing (or re-reading) the old notification state.
_DIEMEN_SENT_TABLE = "diemen_sent"


def area_is_diemen(neighborhood: str | None) -> bool:
    """True when the stored neighborhood slug is covered by Diemen.json.

    Only the municipality slug is verifiable from stored data; the Funda
    district slug (wijk-diemen-*) is not stored in the DB.
    """
    if not neighborhood:
        return False
    return neighborhood.strip().lower() in _DIEMEN_AREA_SLUGS


def _matches_filters(listing: dict, filters: FilterConfig) -> bool:
    """Verify the numeric/object criteria that stored card data reliably
    supports: price, bedrooms, living area, and municipality area.

    NULL stored values never satisfy an enabled bound (mirrors the storage
    matching semantics). ``object_type`` is verified at the search level by
    Funda itself (the search URL carries object_type=house), so it is not
    re-checked against the stored ``property_type`` Dutch slug (huis/...),
    which would be a token mismatch.
    """
    if not area_is_diemen(listing.get("neighborhood")):
        return False

    price = listing.get("price")
    bedrooms = listing.get("bedrooms")
    living_area = listing.get("living_area_m2")

    if filters.price_min is not None and (price is None or price < filters.price_min):
        return False
    if filters.price_max is not None and (price is None or price > filters.price_max):
        return False
    if filters.bedrooms_min is not None and (bedrooms is None or bedrooms < filters.bedrooms_min):
        return False
    if filters.bedrooms_max is not None and (bedrooms is None or bedrooms > filters.bedrooms_max):
        return False
    if filters.living_area_min is not None and (
        living_area is None or living_area < filters.living_area_min
    ):
        return False
    if filters.living_area_max is not None and (
        living_area is None or living_area > filters.living_area_max
    ):
        return False
    return True


def select_batch(
    listings: list[dict], filters: FilterConfig, limit: int = _DEFAULT_MAX_LISTINGS,
) -> list[dict]:
    """Return a small, deterministic batch of Diemen listings that match the
    filters, in the order they were scraped (Funda's publish-date-desc).

    Only the first ``limit`` matching listings are returned. An empty list is
    returned when none match.
    """
    selected: list[dict] = []
    for listing in listings:
        if _matches_filters(listing, filters):
            selected.append(listing)
        if len(selected) >= limit:
            break
    logger.info("Selected %d matching Diemen listing(s) for the topic.", len(selected))
    return selected


def _scrape_kwargs(filters: FilterConfig) -> dict:
    """Map a FilterConfig onto scrape_funda arguments (mirrors main.py)."""
    return dict(
        area=filters.selected_area,
        offering_type=filters.transaction_type or "koop",
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
        garden=filters.garden,
        garden_size_min=filters.garden_size_min,
        availability=filters.availability,
        sort=filters.sort,
        object_type=filters.object_type,
        plot_area_min=filters.plot_size_min,
        plot_area_max=filters.plot_size_max,
        bathrooms_min=filters.bathrooms_min,
        bathrooms_max=filters.bathrooms_max,
        garage_capacity_min=filters.garage_capacity_min,
        garage_capacity_max=filters.garage_capacity_max,
        exterior_space_type=filters.exterior_space_type,
        exterior_space_garden_orientation=filters.exterior_space_garden_orientation,
        zoning=filters.zoning,
        parking_facility=filters.parking_facility,
        garage_type=filters.garage_type,
        accessibility=filters.accessibility,
        amenities=filters.amenities,
        max_pages=5,
    )


def scrape_and_store(
    filters: FilterConfig, db_path: Path | str = _DEFAULT_DB_PATH,
) -> dict:
    """Scrape Diemen with the given filters and persist (card-level) rows.

    Returns a dict with the scraped listing dicts and the insert counts, so
    callers can inspect what was discovered vs stored. Raises on a failed or
    empty scrape so the operator notices a Funda block/anti-bot breakage
    rather than silently producing a tiny sample.
    """
    db_path = Path(db_path)
    init_db(db_path)

    logger.info("Scraping Funda with Diemen filters: area=%s", filters.selected_area)
    listings = scrape_funda(**_scrape_kwargs(filters))
    logger.info("Scraped %d Diemen listing(s).", len(listings))

    if not listings:
        raise RuntimeError(
            "Scrape returned 0 listings — possible Funda block, CAPTCHA, or "
            "page-structure change. Not creating a topic."
        )

    new_count = 0
    for listing in listings:
        try:
            result = insert_listing(listing, db_path)
            if result == "inserted":
                new_count += 1
        except Exception as exc:
            logger.error("Failed to store listing %s: %s",
                         listing.get("listing_id", "?"), exc)

    logger.info("Stored %d new Diemen listing(s); %d seen before.",
                new_count, len(listings) - new_count)
    return {"listings": listings, "new_count": new_count}


def _enrich(listing: dict, filters: FilterConfig) -> dict:
    """Fetch the detail page and score a selected listing, merging the richer
    fields (photos, description, metrics, score) back into the dict.
    Falls back to the card-level dict on any failure (never aborts the batch).
    """
    try:
        detail = fetch_listing_details(listing["url"])
        listing.update(detail)
        result = score_listing(listing, preferences=load_preferences(), filter_config=filters)
        if result.score is not None:
            listing["score"] = result.score
        listing["score_confidence"] = result.confidence
        if result.breakdown:
            import json
            listing["score_breakdown"] = json.dumps(result.breakdown)
    except Exception as exc:
        logger.warning("Detail fetch/scoring failed for %s: %s — posting card-level.",
                       listing.get("listing_id", "?"), exc)
    return listing


def post_listings_to_topic(
    listings: list[dict], filters: FilterConfig, thread_id: str,
) -> dict:
    """Send each selected listing (enriched) to the given forum topic.

    Reuses ``send_listing_notification`` with the new topic's thread_id, so
    the existing rich-message + best-effort-photo delivery is preserved.
    Returns per-listing success booleans.
    """
    results = []
    for listing in listings:
        enriched = _enrich(listing, filters)
        ok = send_listing_notification(enriched, thread_id=thread_id)
        results.append({"listing_id": listing.get("listing_id"),
                        "address": listing.get("address"), "success": ok})
        if not ok:
            logger.error("Notification for listing %s failed.",
                         listing.get("listing_id"))
    sent = sum(1 for r in results if r["success"])
    logger.info("Posted %d/%d listing(s) to topic %s.", sent, len(results), thread_id)
    return sent


# ---------------------------------------------------------------------------
# Isolated Diemen seed + live ledger (separate from the main.py notified flag)
# ---------------------------------------------------------------------------

def _init_diemen_sent(db_path: Path | str) -> None:
    """Idempotently create the Diemen-only sent-ledger table.

    This ledger is fully separate from the global ``notified`` column used by
    the main.py pipeline, so the Diemen seed/live flow never resets, re-reads,
    or re-interprets the old notification state.
    """
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {_DIEMEN_SENT_TABLE} ("
            "listing_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL);"
        )


def _diemen_sent_ids(db_path: Path | str) -> set:
    if not Path(db_path).exists():
        return set()
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute(
            f"SELECT listing_id FROM {_DIEMEN_SENT_TABLE}").fetchall()
    return {r[0] for r in rows}


def _mark_diemen_sent(listing_id: str, db_path: Path | str) -> None:
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            f"INSERT OR IGNORE INTO {_DIEMEN_SENT_TABLE} (listing_id, sent_at) "
            "VALUES (?, ?)",
            (listing_id, datetime.now(timezone.utc).isoformat()),
        )


def _fetch_seedable(listings: list[dict], filters: FilterConfig) -> list[dict]:
    """Return only listings that genuinely match the Diemen filters."""
    return [l for l in listings if _matches_filters(l, filters)]


def load_seed_candidates(
    filters: FilterConfig, db_path: Path | str,
) -> list[dict]:
    """Load existing matching Diemen listings from the DB (source of truth),
    excluding none — the caller applies the already-sent filter. Returns the
    card-level dicts that pass the Diemen filter checks.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT listing_id, url, address, neighborhood, price, "
            "living_area_m2, bedrooms, property_type, notified "
            "FROM listings"
        ).fetchall()
    return _fetch_seedable([dict(r) for r in rows], filters)


def select_for_seed(
    candidates: list[dict], already_sent: set, requested: int,
) -> list[dict]:
    """Select the seed batch: existing matching candidates that have not yet
    been sent to the Diemen topic, up to ``requested`` (ordered as given).

    Never fabricates: returns at most ``len(candidates)`` and never invents
    listings. If fewer than ``requested`` remain, only those are returned.
    """
    selected = []
    for listing in candidates:
        if listing.get("listing_id") in already_sent:
            continue
        selected.append(listing)
        if len(selected) >= requested:
            break
    return selected


def apply_seed(
    candidates: list[dict], filters: FilterConfig, db_path: Path | str,
    requested: int, topic_id: str, dry_run: bool = False,
    sender=None,
) -> int:
    """Seed the Diemen topic with matching, not-yet-seeded listings.

    Posts each candidate to ``topic_id`` via the current rich notification
    (reusing ``send_listing_notification``), records it in the Diemen-only
    ledger, and never touches the global ``notified`` flag nor any other
    topic. ``sender`` is injectable for tests (default: send_listing_notification).
    Returns the number actually posted.
    """
    already = _diemen_sent_ids(db_path)
    batch = select_for_seed(candidates, already, requested)
    _init_diemen_sent(db_path)
    sender = sender or send_listing_notification
    posted = 0
    for listing in batch:
        listing_id = listing.get("listing_id", "?")
        if dry_run:
            logger.info("DRY-RUN would seed: %s | %s", listing_id,
                        listing.get("address"))
            continue
        enriched = _enrich(listing, filters)
        try:
            ok = sender(enriched, topic_id)
        except Exception as exc:
            logger.error("Seeding %s failed: %s", listing_id, exc)
            ok = False
        if ok:
            _mark_diemen_sent(listing_id, db_path)
            posted += 1
            logger.info("Seeded %s -> topic %s (diemen_sent).", listing_id, topic_id)
        else:
            logger.error("Seed notification for %s failed; not marked sent.",
                         listing_id)
    logger.info("Seed complete: posted %d/%d to topic %s.",
                posted, len(batch), topic_id)
    return posted


def apply_live(
    filters: FilterConfig, db_path: Path | str, topic_id: str,
    dry_run: bool = False, sender=None, scraper=None,
) -> int:
    """Live mode: scrape Diemen with the Diemen filters and post only NEW
    matching listings (not already in the Diemen ledger, not already in the
    DB) to the Diemen topic only.

    Fully isolated from main.py: never marks the global ``notified`` flag,
    never posts to the general chat / other topics, and never reads the old
    notification state. ``scraper``/``sender`` injectable for tests.
    Returns the number posted (new matches only).
    """
    _init_diemen_sent(db_path)
    scraper = scraper or scrape_funda
    sender = sender or send_listing_notification
    kwargs = _scrape_kwargs(filters)
    scraped = scraper(**kwargs)

    # Only listings that match the Diemen filters and are not already in the
    # ledger (and were not already in the DB) may be posted.
    known_ids = {
        r[0] for r in _existing_listing_ids(db_path)
    } | _diemen_sent_ids(db_path)

    posted = 0
    for listing in scraped:
        listing_id = listing.get("listing_id", "?")
        if not _matches_filters(listing, filters):
            logger.info("Live: skipping non-matching %s", listing_id)
            continue
        if listing_id in known_ids:
            logger.info("Live: skipping already-seen/sent %s", listing_id)
            continue
        if dry_run:
            logger.info("DRY-RUN would live-post: %s | %s", listing_id,
                        listing.get("address"))
            continue
        enriched = _enrich(listing, filters)
        try:
            ok = sender(enriched, topic_id)
        except Exception as exc:
            logger.error("Live post %s failed: %s", listing_id, exc)
            ok = False
        if ok:
            _mark_diemen_sent(listing_id, db_path)
            posted += 1
            logger.info("Live: posted %s -> topic %s (diemen_sent).",
                        listing_id, topic_id)
        else:
            logger.error("Live notification for %s failed; not marked sent.",
                         listing_id)
    logger.info("Live complete: posted %d new listing(s) to topic %s.",
                posted, topic_id)
    return posted


def _existing_listing_ids(db_path: Path | str) -> list:
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as con:
        return con.execute("SELECT listing_id FROM listings").fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated Diemen flow: seed existing matches into the "
                    "Diemen topic, or run live to post new matches there.",
    )
    parser.add_argument("--mode", choices=("seed", "live"), default="seed",
                        help="seed = post existing matching Diemen listings; "
                             "live = post NEW matching listings (default: seed).")
    parser.add_argument("--filters", default=str(_DEFAULT_FILTERS_PATH),
                        help="Path to the Diemen JSON filter file "
                             "(default: %(default)s).")
    parser.add_argument("--db-path", default=str(_DEFAULT_DB_PATH),
                        help="SQLite database path (default: %(default)s).")
    parser.add_argument("--max-listings", type=int, default=_DEFAULT_MAX_LISTINGS,
                        help="Max listings to post (default: %(default)s).")
    parser.add_argument("--topic-id", default=DIEMEN_TOPIC_ID,
                        help="Diemen forum topic message_thread_id "
                             "(default: %(default)s).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape/select only; do NOT post any Telegram "
                             "messages.")
    parser.add_argument("--topic-name", default=TOPIC_NAME,
                        help="Name of the forum topic (for create/seed "
                             "information; default: %(default)s).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    filters = FilterConfig.from_file(args.filters)
    logger.info("Loaded Diemen filters from %s", args.filters)

    # The Diemen topic is a fixed, isolated target; the module never posts to
    # the general chat or any other topic.
    topic_id = args.topic_id

    if args.mode == "live":
        posted = apply_live(filters, args.db_path, topic_id,
                            dry_run=args.dry_run)
        logger.info("Live: posted %d new listing(s) to Diemen topic %s.",
                    posted, topic_id)
    else:
        # seed mode: use existing DB matches as the source of truth.
        candidates = load_seed_candidates(filters, args.db_path)
        if args.dry_run:
            already = _diemen_sent_ids(args.db_path)
            batch = select_for_seed(candidates, already, args.max_listings)
            logger.info("DRY-RUN would seed %d listing(s) to topic %s:",
                        len(batch), topic_id)
            for l in batch:
                logger.info("  would seed: %s | %s | €%s | %s m2 | %s bd",
                            l.get("listing_id"), l.get("address"),
                            l.get("price"), l.get("living_area_m2"),
                            l.get("bedrooms"))
            return
        posted = apply_seed(candidates, filters, args.db_path,
                            args.max_listings, topic_id)
        logger.info("Seed: posted %d listing(s) to Diemen topic %s.",
                    posted, topic_id)


if __name__ == "__main__":
    main()
