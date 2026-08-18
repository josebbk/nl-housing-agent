"""
Telegram notification sender for the Amsterdam housing scraper.

Loads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env via python-dotenv.
Sends formatted messages to a Telegram group chat when matching listings
are found.

Does NOT import storage.py or scraper.py — orchestration belongs in main.py.
"""

import json
import logging
import os
from pathlib import Path
from urllib import request, error

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

_TELEGRAM_API_BASE = "https://api.telegram.org"
_SEND_MSG_PATH = "/bot{token}/sendMessage"
_MESSAGE_DELAY = 1.1  # seconds between messages (Telegram rate limit safety)

# Display-name mapping for scoring criteria.
# Raw keys come from scoring.py / config/preferences.json weights;
# display names are presentation-only and must not alter criterion keys
# used by scoring.py or storage.py.
_CRITERION_LABELS: dict[str, str] = {
    "neighborhood_value": "Neighborhood",
    "construction_condition": "Condition",
    "ownership": "Ownership",
    "energy_label": "Energy",
    "living_area": "Living area",
    "parking": "Parking",
    "rooms": "Rooms",
    "bathrooms": "Bathrooms",
    "garden": "Garden",
}


def _criterion_label(key: str) -> str:
    """Return the human-readable label for a criterion key."""
    return _CRITERION_LABELS.get(key, key)


def _load_env() -> None:
    """Load .env from the project root if present."""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
        logger.debug("Loaded .env from %s", _ENV_PATH)
    else:
        logger.warning(".env file not found at %s", _ENV_PATH)


def _get_token() -> str:
    """Return the Telegram bot token from environment."""
    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set in the environment. "
            "Ensure .env contains a valid token."
        )
    return token


def _get_chat_id() -> str:
    """Return the Telegram chat ID from environment."""
    _load_env()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not set in the environment. "
            "Ensure .env contains a valid group chat ID."
        )
    return chat_id


def _format_listing_message(listing: dict) -> str:
    """Format a listing dict into a clean Telegram HTML message.

    New notification format (Task 3):

    - Header: address, price, living area, bedrooms, property type, neighborhood
    - Score section with confidence flag and best/weakest criteria
    - Full score breakdown with N/A for missing criteria
    - Listing URL

    All values are dynamically populated from the listing dict's existing
    ``score``, ``score_breakdown`` (JSON list of {criterion,
    points_earned, points_possible, matched}), and
    ``score_confidence`` already produced by ``scoring.py``.
    """
    address = listing.get("address", "N/A")
    price = listing.get("price")
    price_text = f"{price:,.0f}" if price is not None else "N/A"
    living_area = listing.get("living_area_m2")
    area_text = f"{living_area} m²" if living_area is not None else "N/A"
    bedrooms = listing.get("bedrooms")
    bed_text = str(bedrooms) if bedrooms is not None else "N/A"
    property_type = listing.get("property_type", "") or ""
    neighborhood = listing.get("neighborhood", "") or ""
    url = listing.get("url", "")

    # --- Header ---
    parts: list[str] = []
    parts.append(f"\U0001f3e0 <b>{address}</b>")
    parts.append(f"\u20ac{price_text} \u00b7 {area_text} \u00b7 {bed_text} bedrooms")
    if property_type or neighborhood:
        type_and_area = " \u00b7 ".join(
            part for part in [property_type, neighborhood] if part
        )
        parts.append(type_and_area)
    parts.append("")  # blank line before score section

    # --- Score section ---
    score = listing.get("score")
    confidence = listing.get("score_confidence", "")
    score_breakdown = listing.get("score_breakdown")

    if score is None or confidence == "no_data":
        parts.append("Score: unavailable")
    else:
        parts.append(f"\u2b50 <b>{score}/100</b>")

        # Adjusted line — only when confidence is "partial"
        if confidence == "partial" and score_breakdown:
            try:
                breakdown_data = json.loads(score_breakdown)
                missing = [
                    _criterion_label(item.get("criterion", "unknown"))
                    for item in breakdown_data
                    if not item.get("matched", True)
                ]
                if missing:
                    parts.append(
                        f"\u26a0\ufe0f Adjusted \u00b7 "
                        f"{', '.join(missing)} data unavailable"
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # Best / Weakest — sorted by points_earned (absolute score, not ratio)
        # so the listing's strongest/weakest criteria are shown by their
        # contribution to the total score.
        if score_breakdown:
            try:
                breakdown_data = json.loads(score_breakdown)
                matched = [
                    item for item in breakdown_data if item.get("matched", True)
                ]
                unmatched = [
                    item for item in breakdown_data if not item.get("matched", True)
                ]

                if matched:
                    # Sort descending by points_earned for best, ascending for weakest
                    sorted_desc = sorted(
                        matched, key=lambda x: x.get("points_earned", 0), reverse=True
                    )
                    sorted_asc = sorted(
                        matched, key=lambda x: x.get("points_earned", 0)
                    )

                    best = sorted_desc[:3]
                    weakest = sorted_asc[:3]

                    # If ≤6 criteria matched, best and weakest may overlap.
                    # With ≤3 matched criteria, show only Best.
                    # With 4-6 matched criteria, show Best (top 3) and Weakest
                    # (bottom 3), allowing overlap at the boundary.
                    if len(matched) <= 3:
                        parts.append("\U0001f7e2 Best")
                        for item in best:
                            label = _criterion_label(item.get("criterion", "?"))
                            earned = item.get("points_earned", 0)
                            possible = item.get("points_possible", 0)
                            parts.append(
                                f"\u2022 {label} \u2014 {earned}/{possible}"
                            )
                    else:
                        parts.append("\U0001f7e2 Best")
                        for item in best:
                            label = _criterion_label(item.get("criterion", "?"))
                            earned = item.get("points_earned", 0)
                            possible = item.get("points_possible", 0)
                            parts.append(
                                f"\u2022 {label} \u2014 {earned}/{possible}"
                            )
                        parts.append("")
                        parts.append("\U0001f534 Weakest")
                        for item in weakest:
                            label = _criterion_label(item.get("criterion", "?"))
                            earned = item.get("points_earned", 0)
                            possible = item.get("points_possible", 0)
                            parts.append(
                                f"\u2022 {label} \u2014 {earned}/{possible}"
                            )
                    parts.append("")

            except (json.JSONDecodeError, TypeError):
                pass

        # Full score breakdown — every criterion defined in preferences,
        # two per line, with N/A for unmatched criteria.
        if score_breakdown:
            try:
                breakdown_data = json.loads(score_breakdown)
                # Build a lookup by criterion key for O(1) access
                breakdown_map = {
                    item.get("criterion", ""): item for item in breakdown_data
                }
                # Iterate over all criterion keys (stable order from preferences)
                all_criteria = list(breakdown_map.keys())
                pairs: list[list[str]] = []
                current_pair: list[str] = []
                for key in all_criteria:
                    item = breakdown_map[key]
                    label = _criterion_label(key)
                    if item.get("matched", True):
                        earned = item.get("points_earned", 0)
                        possible = item.get("points_possible", 0)
                        current_pair.append(f"{label} {earned}/{possible}")
                    else:
                        current_pair.append(f"{label} N/A")
                    if len(current_pair) == 2:
                        pairs.append(current_pair)
                        current_pair = []
                if current_pair:
                    pairs.append(current_pair)

                parts.append("\U0001f4ca Full score breakdown")
                for pair in pairs:
                    parts.append(" \u00b7 ".join(pair))
                parts.append("")

            except (json.JSONDecodeError, TypeError):
                pass

    # --- URL ---
    if url:
        parts.append(f'\U0001f517 <a href="{url}">View on Funda</a>')

    return "\n".join(parts)


def _send_message(token: str, chat_id: str, message: str) -> bool:
    """Send a single message via the Telegram Bot API.

    Returns True on success, False on failure.
    Never logs the token value.
    """
    url = f"{_TELEGRAM_API_BASE}{_SEND_MSG_PATH.format(token=token)}"

    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    req = request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                msg_id = body.get("result", {}).get("message_id")
                logger.info("Message sent successfully (msg_id=%s).", msg_id)
                return True
            else:
                logger.error("Telegram API returned error: %s", body.get("description", "unknown"))
                return False
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            logger.error(
                "Authentication failed (HTTP %d). Check TELEGRAM_BOT_TOKEN or that the bot is in the target group.",
                exc.code,
            )
        else:
            logger.error("Telegram API HTTP error %d: %s", exc.code, exc.read().decode()[:200])
        return False
    except error.URLError as exc:
        logger.error("Failed to send Telegram message (network error): %s", exc.reason)
        return False
    except Exception as exc:
        logger.error("Unexpected error sending Telegram message: %s", exc)
        return False


def _get_failure_topic_id() -> str:
    """Return the Telegram failure-alert topic ID from environment."""
    _load_env()
    topic_id = os.environ.get("TELEGRAM_FAILURE_TOPIC_ID", "")
    if not topic_id:
        logger.warning("TELEGRAM_FAILURE_TOPIC_ID is not set; failure alerts will not include a thread_id.")
    return topic_id


def send_failure_alert(message: str) -> bool:
    """Send a plain-text failure alert to a dedicated Telegram topic.

    Uses TELEGRAM_FAILURE_TOPIC_ID as message_thread_id so the message
    lands in the failure-alerts topic instead of General.

    This function is intentionally lightweight and never raises — any
    internal failure is logged and False is returned.

    Parameters
    ----------
    message : str
        Plain-text alert message (no HTML formatting).

    Returns
    -------
    bool
        True if the message was sent successfully, False otherwise.
    """
    try:
        token = _get_token()
        chat_id = _get_chat_id()
        topic_id = _get_failure_topic_id()

        payload_data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        if topic_id:
            payload_data["message_thread_id"] = topic_id

        url = f"{_TELEGRAM_API_BASE}{_SEND_MSG_PATH.format(token=token)}"
        payload = json.dumps(payload_data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = request.Request(url, data=payload, headers=headers, method="POST")

        with request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                msg_id = body.get("result", {}).get("message_id")
                logger.info("Failure alert sent successfully (msg_id=%s).", msg_id)
                return True
            else:
                logger.error("Telegram API returned error sending failure alert: %s", body.get("description", "unknown"))
                return False
    except Exception as exc:
        logger.error("Failed to send failure alert: %s", exc)
        return False


def send_listing_notification(listing: dict) -> bool:
    """Send a Telegram notification for a single listing.

    Parameters
    ----------
    listing : dict
        A listing dict with the same structure produced by scraper.py
        (listing_id, url, address, price, living_area_m2, bedrooms, etc.).

    Returns
    -------
    bool
        True if the message was sent successfully, False otherwise.
    """
    token = _get_token()
    chat_id = _get_chat_id()
    message = _format_listing_message(listing)

    logger.info("Sending notification for listing %s (%s)", listing.get("listing_id"), listing.get("address"))
    return _send_message(token, chat_id, message)


def send_notifications(listings: list[dict], delay: float = _MESSAGE_DELAY) -> list[bool]:
    """Send a Telegram notification for each listing in the list.

    Sends messages sequentially with a small delay between them to avoid
    Telegram rate limits.

    Parameters
    ----------
    listings : list[dict]
        List of listing dicts (same structure as scraper.py output).
    delay : float
        Seconds to wait between messages (default 1.1s).

    Returns
    -------
    list[bool]
        One success/failure result per listing, in order.
    """
    if not listings:
        logger.info("No listings to notify.")
        return []

    results = []
    for i, listing in enumerate(listings):
        success = send_listing_notification(listing)
        results.append(success)

        if i < len(listings) - 1 and delay > 0:
            import time
            time.sleep(delay)

    sent = sum(1 for r in results if r)
    failed = len(results) - sent
    logger.info(
        "Notification batch complete: %d sent, %d failed out of %d total.",
        sent, failed, len(results),
    )
    return results


# ---------------------------------------------------------------------------
# CLI entry point — test mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print()
    print("=" * 60)
    print("NOTIFIER TEST MODE")
    print("=" * 60)
    print()
    print("WARNING: This will send a REAL message to the Telegram group")
    print("configured in .env. Make sure this is intentional before proceeding.")
    print()
    print("Sample listing data:")
    sample = {
        "listing_id": "test-00000000",
        "url": "https://www.funda.nl/koop/amsterdam/huis-test-straat/12345678/",
        "address": "Teststraat 42, Amsterdam",
        "neighborhood": "De Pijp",
        "price": 650000,
        "living_area_m2": 115,
        "plot_size_m2": None,
        "rooms": 4,
        "bedrooms": 3,
        "property_type": "huis",
        "year_built": None,
        "energy_label": None,
        "status": None,
    }
    for k, v in sample.items():
        print(f"  {k}: {v}")
    print()

    success = send_listing_notification(sample)

    print()
    if success:
        print("SUCCESS: Test message was sent to the Telegram group.")
    else:
        print("FAILURE: Could not send the test message. Check logs above.")
    print("=" * 60)
    print()