"""
Telegram notification sender for the Amsterdam housing scraper.

Loads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env via python-dotenv.
Sends formatted messages to a Telegram group chat when matching listings
are found.

Each listing is delivered as ONE coherent Telegram notification: the rich
HTML text (header metrics, score breakdown, key facts, Funda URL) is sent
together with up to 3 property photos of the same listing as the caption
of a single photo/photo-album media message (sendPhoto / sendMediaGroup).
Image URLs arrive in the listing dict under ``image_urls`` (extracted from
the Funda detail page by detail_scraper.py). Photos are best-effort —
individual download failures never fail the notification. When no photos
are available, or the text is too long for a media caption, the
notification degrades to a text-only message (sendMessage). Exactly one
delivered notification is produced per listing — never a duplicate
standalone text message or duplicate image messages.

Does NOT import storage.py or scraper.py — orchestration belongs in main.py.
"""

import html
import io
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
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
_SEND_PHOTO_PATH = "/bot{token}/sendPhoto"
_SEND_MEDIA_GROUP_PATH = "/bot{token}/sendMediaGroup"
_MESSAGE_DELAY = 1.1  # seconds between messages (Telegram rate limit safety)

# --- Caption limits ---
# Telegram caps photo/media-group captions at 1024 characters; a small
# safety margin is kept for emoji width and entity parsing.
_TELEGRAM_CAPTION_MAX_CHARS = 1024
_CAPTION_SAFETY_LIMIT = _TELEGRAM_CAPTION_MAX_CHARS - 24

# --- Image handling constants ---
# Exactly 3 property photos are attached per listing when at least 3 valid
# image URLs are available; fewer images are sent as-is.
MAX_IMAGES_PER_LISTING = 3
_IMAGE_DOWNLOAD_TIMEOUT = 20  # seconds
_TELEGRAM_SEND_TIMEOUT = 30  # seconds
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # hard cap per downloaded image
_IMAGE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Presentation-layer value mapping (English-first notification)
# ---------------------------------------------------------------------------
# detail_scraper.py stores parking_type either as a short English code or,
# when unclassified, as raw Dutch page text. These mappings convert values
# to English at presentation level ONLY — the stored values, scraping and
# scoring are never touched. They mirror the keyword logic already used by
# detail_scraper.py / scoring.py. Raw Dutch source terminology is kept in
# parentheses after the English meaning.

_PARKING_VALUE_MAP: dict[str, str] = {
    "private": "Private",
    "carport": "Carport",
    "public": "Public",
    "paid": "Paid",
}

# Valid Funda energy labels (worst -> best). Anything else (e.g. a garbled
# concatenated Dutch page section) is not a label and is not displayed.
_VALID_ENERGY_LABEL = re.compile(r"^[A-G](?:\+{1,4})?$")

# Trailing house-number pattern of a "Street 123", "Street 34-3",
# "Street 80-A" or "Street 245-III" style address.
_HOUSE_NUMBER_SUFFIX = re.compile(r"\s+\d+(?:-\d+)?(?:-[A-Za-z]{1,4})?$")


def _parking_value(raw: str | None) -> str:
    """English presentation value for a stored parking_type.

    English codes map directly to English labels. Raw Dutch page text is
    shown as "English meaning (original Dutch term)", e.g.
    "Available (Parkeervergunning)". Returns "No" when the value is
    absent or unrecognized so the line is always shown.
    """
    if not raw:
        return "No"
    primary = raw.strip().split("+")[0].strip()
    primary_lower = primary.lower()
    if primary_lower in _PARKING_VALUE_MAP:
        return _PARKING_VALUE_MAP[primary_lower]
    if "geen" in primary_lower:
        return f"No ({primary})"
    for keyword, label in (
        ("eigen terrein", "Private"),
        ("parkeervergunning", "Available"),
        ("vergunning", "Available"),
        ("carport", "Carport"),
        ("openbaar", "Public"),
        ("betaald", "Paid"),
    ):
        if keyword in primary_lower:
            return f"{label} ({primary})"
    return "No"


def _street_from_address(address: str) -> str | None:
    """Derive the street name from a "Street 123[-X]" address.

    Returns None when no house number can be stripped, so a street is
    never guessed for addresses like "Open huis".
    """
    if not address:
        return None
    address = address.strip()
    street = _HOUSE_NUMBER_SUFFIX.sub("", address)
    if street and street != address:
        return street
    return None


def _fits_telegram_caption(text: str) -> bool:
    """Return True when ``text`` fits Telegram's media caption limit."""
    return len(text) <= _CAPTION_SAFETY_LIMIT


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
    """Format a listing dict into the approved Telegram HTML template.

    Presentation-only; every value comes from the listing dict exactly as
    produced by scraper.py / detail_scraper.py / scoring.py. The Score is
    intentionally NOT displayed — the score fields and all scoring logic
    remain untouched. Layout:

        <b>{address}</b>

        💰 Price: €{price}
        🏠 Living area Size: {living_area} m² · €{price/m²}/m²
        🌳 Plot Size: {plot_size_m2} m²
        🛏 Bedrooms: {bedrooms}
        🌳 Garden area: {garden_size_m2} m² / Yes / No
        📍 Location: {city} · {street} · <a href="{url}">View on Funda</a>
        ⚡ Energy label: {label}
        🏗 Year built: {year_built}
        🅿️ Parking: {value}

    Rules:

    * every metric line follows ``EMOJI + English metric name + ":" +
      value``;
    * missing metrics are omitted — never invented. "Number of stories"
      and Pros/Cons/Bottom line require data the project does not
      extract (number of floors, property description text) and are
      therefore omitted rather than fabricated;
    * price-per-m² is computed only from the two required fields
      (price, living_area_m2);
    * non-numeric values are English; raw Dutch source terminology is
      kept in parentheses (see _parking_value);
    * the address is kept exactly as provided; Location combines the
      available components in the order City → Area/District → Street →
      Postal code (Area/District and Postal code are never invented)
      and carries the Funda link.
    """
    address = listing.get("address", "N/A")
    price = listing.get("price")
    price_text = f"€{price:,.0f}" if price is not None else "N/A"
    living_area = listing.get("living_area_m2")
    area_text = f"{living_area} m²" if living_area is not None else "N/A"
    bedrooms = listing.get("bedrooms")
    bed_text = str(bedrooms) if bedrooms is not None else "N/A"
    neighborhood = listing.get("neighborhood", "") or ""
    url = listing.get("url", "")

    # Price per m² — computed strictly from the two reliable required fields.
    price_per_m2_text = None
    if price and living_area:
        price_per_m2_text = f"€{price / living_area:,.0f}/m²"

    parts: list[str] = []
    parts.append(f"<b>{address}</b>")
    parts.append("")

    parts.append(f"\U0001f4b0 Price: {price_text}")

    size_line = f"\U0001f3e0 Living area Size: {area_text}"
    if price_per_m2_text:
        size_line += f" \u00b7 {price_per_m2_text}"
    parts.append(size_line)

    if listing.get("plot_size_m2"):
        parts.append(f"\U0001f333 Plot Size: {listing['plot_size_m2']} m\u00b2")

    parts.append(f"\U0001f6cf Bedrooms: {bed_text}")

    garden_size = listing.get("garden_size_m2")
    if isinstance(garden_size, (int, float)) and garden_size > 0:
        garden_text = f"{garden_size} m\u00b2"
    elif listing.get("garden_present"):
        garden_text = "Yes"
    else:
        garden_text = "No"
    parts.append(f"\U0001f333 Garden area: {garden_text}")

    if neighborhood or url:
        location_bits = []
        if neighborhood:
            location_bits.append(neighborhood.title())
            street = _street_from_address(address if address != "N/A" else "")
            if street:
                location_bits.append(street)
        if url:
            location_bits.append(f'<a href="{url}">View on Funda</a>')
        parts.append("\U0001f4cd Location: " + " \u00b7 ".join(location_bits))

    energy_label = listing.get("energy_label")
    if energy_label and _VALID_ENERGY_LABEL.match(str(energy_label).strip().upper()):
        parts.append(f"\u26a1 Energy label: {str(energy_label).strip().upper()}")

    if listing.get("year_built"):
        parts.append(f"\U0001f3d7 Year built: {listing['year_built']}")

    parts.append(
        f"\U0001f17f\ufe0f Parking: {_parking_value(listing.get('parking_type'))}"
    )

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


# ---------------------------------------------------------------------------
# Property image selection, download and album delivery
# ---------------------------------------------------------------------------

def _looks_like_image(data: bytes) -> bool:
    """Return True when the byte payload has a known image magic number.

    Guards against CDNs serving HTML error pages with a misleading
    Content-Type.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return True  # JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True  # PNG
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return True  # WEBP
    if data.startswith(b"GIF8"):
        return True  # GIF
    return False


def _select_images(
    image_urls, max_images: int = MAX_IMAGES_PER_LISTING,
) -> list[str]:
    """Deterministically select up to ``max_images`` unique image URLs.

    Selection rule (documented in Architecture.md): preserve the input
    order — which is the Funda gallery order (hero/facade photo first) —
    skipping entries that are not http(s) URLs and exact duplicates,
    stopping at ``max_images``. No randomness; the same input always
    yields the same selection.

    Accepts either the decoded list form or the JSON TEXT representation
    as stored in the listings table (e.g. when callers read raw DB rows).
    Unparseable input yields an empty selection.
    """
    if isinstance(image_urls, str):
        try:
            image_urls = json.loads(image_urls)
        except (ValueError, TypeError):
            return []
    selected: list[str] = []
    seen: set[str] = set()
    for url in image_urls or []:
        if not isinstance(url, str):
            continue
        candidate = url.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        selected.append(candidate)
        if len(selected) >= max_images:
            break
    return selected


def _download_image(url: str, dest_path: Path) -> bool:
    """Download a single property image to ``dest_path``.

    Safety properties:
    * bounded by ``_IMAGE_DOWNLOAD_TIMEOUT`` and ``_MAX_IMAGE_BYTES``;
    * response Content-Type must be an image type;
    * payload must carry a known image magic number before it is written;
    * the file is written only after full validation (no partial files);
    * any failure is logged (without secrets) and returns False — never
      raises to the caller.
    """
    try:
        req = request.Request(url, headers={
            "User-Agent": _IMAGE_USER_AGENT,
            "Accept": "image/*",
        })
        chunks: list[bytes] = []
        budget = _MAX_IMAGE_BYTES + 1
        with request.urlopen(req, timeout=_IMAGE_DOWNLOAD_TIMEOUT) as resp:
            content_type = (
                (resp.headers.get("Content-Type") or "").split(";")[0]
                .strip().lower()
            )
            if not content_type.startswith("image/"):
                logger.warning(
                    "Image URL did not return an image (Content-Type=%s); skipping.",
                    content_type or "none",
                )
                return False
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                budget -= len(chunk)
                if budget < 0:
                    logger.warning(
                        "Image exceeds %d byte cap; skipping.", _MAX_IMAGE_BYTES,
                    )
                    return False
                chunks.append(chunk)
    except error.HTTPError as exc:
        logger.warning("Image download failed (HTTP %d): %s", exc.code, exc.reason)
        return False
    except error.URLError as exc:
        logger.warning("Image download failed (network error): %s", exc.reason)
        return False
    except Exception as exc:
        logger.warning("Image download failed: %s", exc)
        return False

    data = b"".join(chunks)
    if not data or not _looks_like_image(data):
        logger.warning("Image payload is not a recognisable image; skipping.")
        return False

    dest_path.write_bytes(data)
    return True


def _build_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """Build a multipart/form-data body from text fields and file payloads.

    ``files`` maps field name -> (filename, bytes). Returns (body, boundary).
    """
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    def write_line(line: str) -> None:
        buf.write(line.encode("utf-8"))

    for name, value in fields.items():
        write_line(f"--{boundary}\r\n")
        write_line(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        write_line(str(value))
        write_line("\r\n")
    for name, (filename, data) in files.items():
        write_line(f"--{boundary}\r\n")
        write_line(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        )
        write_line("Content-Type: application/octet-stream\r\n\r\n")
        buf.write(data)
        write_line("\r\n")
    write_line(f"--{boundary}--\r\n")
    return buf.getvalue(), boundary


def _post_multipart(
    token: str, method_path: str, fields: dict, files: dict,
) -> bool:
    """POST a multipart/form-data request to a Telegram Bot API method."""
    body, boundary = _build_multipart(fields, files)
    url = f"{_TELEGRAM_API_BASE}{method_path.format(token=token)}"
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=_TELEGRAM_SEND_TIMEOUT) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
            if resp_body.get("ok"):
                logger.info("%s sent successfully.", method_path.split("/")[-1])
                return True
            logger.error(
                "Telegram API returned error on %s: %s",
                method_path.split("/")[-1],
                resp_body.get("description", "unknown"),
            )
            return False
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            logger.error(
                "Authentication failed (HTTP %d). Check TELEGRAM_BOT_TOKEN "
                "or that the bot is in the target group.",
                exc.code,
            )
        else:
            logger.error(
                "Telegram API HTTP error %d on %s", exc.code,
                method_path.split("/")[-1],
            )
        return False
    except Exception as exc:
        logger.error("Failed to send %s: %s", method_path.split("/")[-1], exc)
        return False


def _send_images(
    token: str, chat_id: str, image_paths: list, caption: str = "",
) -> bool:
    """Send 1..n downloaded images as one Telegram media message.

    * 1 image  -> sendPhoto (multipart upload).
    * 2+ images -> sendMediaGroup (album; caption on the first item only).

    ``caption`` is already-formatted HTML and is sent verbatim with
    parse_mode=HTML, so the property text and its photos are delivered
    as one coherent notification. Callers passing plain text must
    HTML-escape it first.
    """
    if not image_paths:
        return False

    try:
        if len(image_paths) == 1:
            fields = {"chat_id": chat_id}
            if caption:
                fields["caption"] = caption
                fields["parse_mode"] = "HTML"
            files = {
                "photo": ("photo.jpg", Path(image_paths[0]).read_bytes()),
            }
            return _post_multipart(token, _SEND_PHOTO_PATH, fields, files)

        media = []
        files = {}
        for i, path in enumerate(image_paths[:10]):  # API max: 10 per album
            attach_name = f"image{i}"
            item = {"type": "photo", "media": f"attach://{attach_name}"}
            if i == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
            files[attach_name] = (f"image{i}.jpg", Path(path).read_bytes())
        fields = {"chat_id": chat_id, "media": json.dumps(media)}
        return _post_multipart(token, _SEND_MEDIA_GROUP_PATH, fields, files)
    except Exception as exc:
        logger.error("Image album upload failed: %s", exc)
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
    """Send a single coherent Telegram notification for a listing.

    Delivery flow (text and photos presented together as ONE coherent
    notification):
      1. format the rich HTML message (header metrics, score sections,
         key facts, Funda URL);
      2. select up to 3 property images (deterministic, from
         ``listing["image_urls"]`` produced by detail_scraper.py) and
         download them into a temporary directory (individual failures
         are skipped, never fatal);
      3. preferred path — deliver text and photos together as one
         Telegram media message: the full message rides as the HTML
         caption of the photo (sendPhoto) or photo album
         (sendMediaGroup). No standalone text message is sent;
      4. fallbacks — each listing still receives exactly one delivered
         notification, never duplicates:
         * no images / all downloads fail  -> text-only sendMessage;
         * album send failed               -> text-only sendMessage
           (the failed album produced no delivered message);
         * message too long for a Telegram media caption (> 1024 chars)
           -> the text is sent via sendMessage and the photos follow as
           an album captioned with the address (text-first
           presentation, kept so no information is dropped);
      5. always clean up the temporary files.

    Failure semantics:
    * the authoritative notification (album-with-caption, or the text
      fallback) fails -> return False; the caller leaves the listing
      unnotified so it is retried on a later run;
    * photos are best-effort: download failures are skipped, and after a
      failed album upload no image retry is attempted (no duplicate
      image messages).

    Parameters
    ----------
    listing : dict
        A listing dict with the same structure produced by scraper.py /
        detail_scraper.py (listing_id, url, address, price,
        living_area_m2, bedrooms, ..., optional ``image_urls``).

    Returns
    -------
    bool
        True if exactly one coherent notification was delivered
        (with or without photos), False only when no notification could
        be delivered at all.
    """
    token = _get_token()
    chat_id = _get_chat_id()
    message = _format_listing_message(listing)

    logger.info(
        "Sending notification for listing %s (%s)",
        listing.get("listing_id"), listing.get("address"),
    )

    # --- Best-effort property photos (same listing identity) ---
    selected_urls = _select_images(listing.get("image_urls"))
    if not selected_urls:
        logger.info(
            "No property images available for listing %s; "
            "text-only notification.",
            listing.get("listing_id"),
        )
        return _send_message(token, chat_id, message)

    tmp_dir = tempfile.mkdtemp(prefix="funda-images-")
    try:
        downloaded_paths = []
        for i, image_url in enumerate(selected_urls):
            dest = Path(tmp_dir) / f"image_{i}.jpg"
            if _download_image(image_url, dest):
                downloaded_paths.append(dest)
            else:
                logger.warning(
                    "Skipping unavailable image %d for listing %s.",
                    i + 1, listing.get("listing_id"),
                )

        if not downloaded_paths:
            logger.warning(
                "All image downloads failed for listing %s; "
                "text-only notification.",
                listing.get("listing_id"),
            )
            return _send_message(token, chat_id, message)

        # --- Preferred delivery: photos + full text as one media message ---
        if _fits_telegram_caption(message):
            if _send_images(token, chat_id, downloaded_paths, caption=message):
                logger.info(
                    "Sent notification with %d photo(s) for listing %s.",
                    len(downloaded_paths), listing.get("listing_id"),
                )
                return True
            logger.warning(
                "Photo album upload failed for listing %s; falling back "
                "to a text-only notification (no image retry to avoid "
                "duplicates).",
                listing.get("listing_id"),
            )
            return _send_message(token, chat_id, message)

        # --- Caption too long: text first, photos as an album after it ---
        if not _send_message(token, chat_id, message):
            return False
        if _send_images(
            token, chat_id, downloaded_paths,
            caption=html.escape(listing.get("address") or ""),
        ):
            logger.info(
                "Sent %d photo(s) for listing %s.",
                len(downloaded_paths), listing.get("listing_id"),
            )
        else:
            logger.warning(
                "Photo album upload failed for listing %s; the text "
                "notification was already delivered (no retry to avoid "
                "duplicates).",
                listing.get("listing_id"),
            )
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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