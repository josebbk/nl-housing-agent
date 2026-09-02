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
import sys
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-off: create a new forum topic and post a small batch "
                    "of matching Diemen listings.",
    )
    parser.add_argument("--filters", default=str(_DEFAULT_FILTERS_PATH),
                        help="Path to the Diemen JSON filter file "
                             "(default: %(default)s).")
    parser.add_argument("--db-path", default=str(_DEFAULT_DB_PATH),
                        help="SQLite database path (default: %(default)s).")
    parser.add_argument("--max-listings", type=int, default=_DEFAULT_MAX_LISTINGS,
                        help="Max listings to post (default: %(default)s).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape, store and select, but do NOT create the "
                             "topic or send any Telegram messages.")
    parser.add_argument("--topic-name", default=TOPIC_NAME,
                        help="Name of the new forum topic (default: %(default)s).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    filters = FilterConfig.from_file(args.filters)
    logger.info("Loaded Diemen filters from %s", args.filters)

    scraped = scrape_and_store(filters, args.db_path)
    batch = select_batch(scraped["listings"], filters, args.max_listings)

    if not batch:
        logger.error("No stored Diemen listings match the filters; "
                     "creating no topic and sending nothing.")
        sys.exit(1)

    if args.dry_run:
        logger.info("DRY-RUN: would create topic '%s' and post %d listing(s).",
                    args.topic_name, len(batch))
        for l in batch:
            logger.info("  DRY-RUN would post: %s | %s | €%s | %s m2 | %s bd",
                        l.get("listing_id"), l.get("address"), l.get("price"),
                        l.get("living_area_m2"), l.get("bedrooms"))
        return

    thread_id = create_forum_topic(args.topic_name)
    if thread_id is None:
        logger.error(
            "Could not create the new forum topic '%s'. No messages were "
            "sent so nothing was misplaced — fix the blocker and re-run.",
            args.topic_name,
        )
        sys.exit(1)

    logger.info("New forum topic '%s' created (message_thread_id=%s).",
                args.topic_name, thread_id)

    sent = post_listings_to_topic(batch, filters, thread_id)
    if sent < len(batch):
        logger.error("Some listings could not be posted (%d/%d).", sent, len(batch))
        sys.exit(1)
    logger.info("Done: %d listing(s) posted to topic '%s'.", sent, args.topic_name)


if __name__ == "__main__":
    main()
