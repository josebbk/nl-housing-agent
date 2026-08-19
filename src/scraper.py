"""
Funda.nl scraper — Playwright-based listing extractor.

Extracts Amsterdam for-sale listings from Funda using headless Chromium.
Returns structured listing dicts ready for storage.py insertion.

Does NOT import storage.py or send notifications — orchestration belongs
in main.py.

Working filtered search URL (discovered 2025-08-11):
    https://www.funda.nl/zoeken/koop?selected_area=amsterdam&price=550000-750000&floor_area=100-&bedrooms=3-

Filter input IDs (for programmatic filter application via UI):
    #price_from, #price_to
    #floor_area_from, #floor_area_to
    #bedrooms_from, #bedrooms_to

Pagination: append &page=N to the URL (1-indexed).
"""

import logging
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model — mirrors storage.py's listings table columns
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    listing_id: str
    url: str
    address: str
    neighborhood: str
    price: Optional[int]
    living_area_m2: Optional[int]
    plot_size_m2: Optional[int] = None
    rooms: Optional[int] = None
    bedrooms: Optional[int] = None
    property_type: Optional[str] = None
    year_built: Optional[int] = None
    energy_label: Optional[str] = None
    status: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def _range_param(name: str, lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    """Build a Funda ``{name}={from}-{to}`` query parameter from a range.

    Either bound may be None (open-ended), matching Funda's convention
    (e.g. ``floor_area=100-`` for "at least 100", ``3-5`` for a range).
    Returns None when both bounds are unset.
    """
    if lo is None and hi is None:
        return None
    lo_s = "" if lo is None else str(lo)
    hi_s = "" if hi is None else str(hi)
    return f"{name}={lo_s}-{hi_s}"


def build_search_url(
    area: str = "amsterdam",
    offering_type: str = "koop",
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    floor_area_min: Optional[int] = None,
    floor_area_max: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    bedrooms_max: Optional[int] = None,
    rooms_min: Optional[int] = None,
    rooms_max: Optional[int] = None,
    radius_km: Optional[int] = None,
    construction_type: Optional[str] = None,
    page: int = 1,
) -> str:
    """Build a Funda search URL with the given filters.

    Funda URL format (discovered by loading the site with Playwright):
        https://www.funda.nl/zoeken/{offering_type}?selected_area={area}
        &price={min}-{max}
        &floor_area={min}-{max}
        &bedrooms={min}-{max}
        &rooms={min}-{max}
        &page={n}

    When ``radius_km`` is set, Funda encodes the radius inside the location
    value as a JSON array: ``selected_area=["{area},{radius}km"]``. The whole
    value is URL-encoded so quotes/brackets/comma are safe for any HTTP client.
    ``construction_type`` is a categorical exact-match parameter (``existing``
    or ``new``).
    """
    base = f"https://www.funda.nl/zoeken/{offering_type}"

    if radius_km is not None:
        location = f'["{area},{radius_km}km"]'
        params = [f"selected_area={urllib.parse.quote(location, safe='')}"]
    else:
        params = [f"selected_area={area}"]

    if price_min is not None or price_max is not None:
        p_min = price_min if price_min is not None else ""
        p_max = price_max if price_max is not None else ""
        params.append(f"price={p_min}-{p_max}")

    fp = _range_param("floor_area", floor_area_min, floor_area_max)
    if fp is not None:
        params.append(fp)

    bp = _range_param("bedrooms", bedrooms_min, bedrooms_max)
    if bp is not None:
        params.append(bp)

    rp = _range_param("rooms", rooms_min, rooms_max)
    if rp is not None:
        params.append(rp)

    if construction_type is not None:
        params.append(f"construction_type={construction_type}")

    params.append(f"page={page}")

    return f"{base}?{'&'.join(params)}"


# ---------------------------------------------------------------------------
# HTTP fetching — bypasses Akamai bot-detection on datacenter IPs
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_page_html(url: str, timeout: int = 30) -> str:
    """Fetch a Funda page using urllib with realistic browser headers.

    Funda's Akamai bot-protection blocks direct Playwright navigation from
    datacenter IPs.  Fetching the raw HTML with a standard HTTP client first,
    then loading it into the browser for JS rendering, bypasses this check.
    """
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    response = urllib.request.urlopen(req, timeout=timeout)
    return response.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"€\s*([\d.]+)\s*([kv.]?[o.]?)")


def parse_price(text: str) -> tuple[Optional[int], str]:
    """Return (price_in_cents, price_text) from listing text.

    Funda prices are in euros with '.' as thousands separator.
    'k.k.' = koopprijs (asking price), 'v.o.n.' = vraagprijs onder voorbehoud,
    'k.o.' = koopoption.
    """
    m = _PRICE_RE.search(text)
    if not m:
        return None, ""
    raw = m.group(1).replace(".", "")
    return int(raw), m.group(0)


# ---------------------------------------------------------------------------
# Listing extraction from DOM text
# ---------------------------------------------------------------------------

def _extract_listing_data(text: str, href: str) -> Optional[dict]:
    """Parse a Funda listing card's full text + href into a dict.

    The text comes from the card's flexRow container and contains all
    visible fields: address, postcode+city, price, living area, plot area,
    rooms, and energy label.

    Returns None if the text is too short or unparseable.
    """
    if not text or len(text) < 10:
        return None

    # --- URL-based fields (always reliable) ---
    # Listing ID: /detail/koop/.../{id}/
    id_m = re.search(r"/(\d+)/$", href)
    listing_id = id_m.group(1) if id_m else None
    if not listing_id:
        return None

    # Neighborhood/city: /detail/koop/{city}/
    city_m = re.search(r"/detail/koop/([^/]+)/", href)
    neighborhood = city_m.group(1).replace("-", " ") if city_m else None

    # Property type: /detail/koop/{city}/{type}-{slug}/{id}/
    type_m = re.search(r"/detail/koop/[^/]+/([a-z]+)-", href)
    property_type = type_m.group(1) if type_m else None

    # --- Price: element with class "truncate" containing "€" ---
    price = None
    price_text = ""
    price_m = re.search(r"€\s*([\d.]+)\s*([kv.]?[o.]?)", text)
    if price_m:
        raw = price_m.group(1).replace(".", "")
        price = int(raw)
        price_text = price_m.group(0)

    # --- Living area, plot area, bedrooms, energy label ---
    # These appear after the price line, in the format:
    #   126 m²
    #   113 m²
    #   4
    #   A
    # or:
    #   105 m²
    #   3
    #   D
    # The number after the last m² is the bedrooms count (from the
    # bedroom-icon SVG's sibling <span>), and the line after that
    # is the energy label.
    area_matches = re.findall(r"(\d+)\s*m\u00b2", text)
    living_area_m2 = int(area_matches[0]) if area_matches else None
    plot_size_m2 = int(area_matches[1]) if len(area_matches) > 1 else None

    # Extract bedrooms and energy label from the tail after the last m²
    bedrooms = None
    energy_label = None
    last_area_pos = text.rfind("m²")
    if last_area_pos >= 0:
        tail = text[last_area_pos + 2:].strip()
        tail_lines = [l.strip() for l in tail.split("\n") if l.strip()]
        # First line after m² is bedrooms (from bedroom-icon span)
        # Second line is energy label (A-G+)
        if tail_lines:
            try:
                bedrooms = int(tail_lines[0])
            except ValueError:
                pass
        if len(tail_lines) > 1 and re.match(r"^[A-G+]$", tail_lines[1]):
            energy_label = tail_lines[1]

    # --- Address ---
    # Address is typically the first meaningful line that looks like a street
    # address. Funda cards often have badge text ("Nieuw", "Blikvanger") on
    # the first line, so we skip those.
    badge_words = {"nieuw", "blikvanger", "advertentie", "verkoop", "verkoopwoning"}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    address = None
    for line in lines:
        # Skip badge words
        if line.lower() in badge_words:
            continue
        # Skip lines that are just postcode patterns (those are neighborhood)
        if re.match(r"^\d{4}[A-Z]{2}\s*[A-Z]{0,2}\d{0,2}$", line):
            continue
        # Skip lines that look like prices
        if "€" in line:
            continue
        # Skip lines that look like area measurements
        if "m²" in line:
            continue
        # This should be the address
        address = line
        break

    # Rooms is only available on detail pages, not at card level
    rooms = None

    if not address:
        # Fallback: first non-badge line
        for line in lines:
            if line.lower() not in badge_words:
                address = line
                break

    return {
        "listing_id": listing_id,
        "url": f"https://www.funda.nl{href}",
        "address": address,
        "neighborhood": neighborhood,
        "price": price,
        "living_area_m2": living_area_m2,
        "plot_size_m2": plot_size_m2,
        "bedrooms": bedrooms,
        "property_type": property_type,
        "energy_label": energy_label,
        "status": None,  # Not available at card level
        "rooms": rooms,
        "year_built": None,  # Only on detail pages
    }


# ---------------------------------------------------------------------------
# Main scrape function
# ---------------------------------------------------------------------------

def scrape_funda(
    area: str = "amsterdam",
    offering_type: str = "koop",
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    floor_area_min: Optional[int] = None,
    floor_area_max: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    bedrooms_max: Optional[int] = None,
    rooms_min: Optional[int] = None,
    rooms_max: Optional[int] = None,
    radius_km: Optional[int] = None,
    construction_type: Optional[str] = None,
    max_pages: int = 5,
    headless: bool = True,
) -> list[dict]:
    """Scrape Funda search results and return a list of listing dicts.

    Parameters
    ----------
    area : str
        City/area slug (e.g. "amsterdam").
    offering_type : str
        "koop" (for sale) or "huur" (for rent).
    price_min, price_max : int or None
        Price range in euros.
    floor_area_min : int or None
        Minimum living area in m².
    floor_area_max : int or None
        Maximum living area in m².
    bedrooms_min : int or None
        Minimum number of bedrooms.
    bedrooms_max : int or None
        Maximum number of bedrooms.
    rooms_min : int or None
        Minimum number of rooms.
    rooms_max : int or None
        Maximum number of rooms.
    radius_km : int or None
        Search radius in kilometres around the area (None = no radius).
    construction_type : str or None
        Exact construction type: "existing" or "new" (None = no restriction).
    max_pages : int
        Maximum pages to scrape (default 5, ~150 listings).
    headless : bool
        Whether to run the browser in headless mode.

    Returns
    -------
    list[dict]
        One dict per listing, with keys matching storage.py's insert_listing
        schema. Fields only available on detail pages (rooms, year_built,
        plot_size_m2) are left as None.
    """
    all_listings: list[dict] = []
    seen_ids: set[str] = set()

    browser: Optional[Browser] = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )

            context = browser.new_context(
                locale="nl-NL",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                timezone_id="Europe/Amsterdam",
            )

            page = context.new_page()

            for page_num in range(1, max_pages + 1):
                url = build_search_url(
                    area=area,
                    offering_type=offering_type,
                    price_min=price_min,
                    price_max=price_max,
                    floor_area_min=floor_area_min,
                    floor_area_max=floor_area_max,
                    bedrooms_min=bedrooms_min,
                    bedrooms_max=bedrooms_max,
                    rooms_min=rooms_min,
                    rooms_max=rooms_max,
                    radius_km=radius_km,
                    construction_type=construction_type,
                    page=page_num,
                )

                logger.info("Fetching page %d: %s", page_num, url)

                try:
                    html = _fetch_page_html(url, timeout=30)
                except Exception as exc:
                    logger.error("HTTP fetch failed on page %d: %s", page_num, exc)
                    break

                # Load the fetched HTML into the browser for JS rendering.
                # This bypasses Akamai bot-protection that blocks direct
                # Playwright navigation from datacenter IPs.
                data_url = "data:text/html," + urllib.parse.quote(html)
                page.goto(data_url, wait_until="domcontentloaded", timeout=30000)

                # Wait for JS to render the listing cards
                page.wait_for_timeout(random.uniform(3000, 5000))

                # Handle cookie consent banner if present
                _dismiss_cookie_banner(page)

                # Wait after cookie dismissal
                page.wait_for_timeout(random.uniform(2000, 4000))

                # Extract listings from the page
                page_listings = _extract_page_listings(page)
                new_count = 0

                for listing in page_listings:
                    lid = listing.get("listing_id")
                    if lid and lid not in seen_ids:
                        seen_ids.add(lid)
                        all_listings.append(listing)
                        new_count += 1

                logger.info(
                    "Page %d: found %d listings (%d new), total so far: %d",
                    page_num,
                    len(page_listings),
                    new_count,
                    len(all_listings),
                )

                # Anti-bot: random delay between pages
                if page_num < max_pages:
                    delay = random.uniform(2.0, 5.0)
                    logger.info("Waiting %.1fs before next page...", delay)
                    page.wait_for_timeout(int(delay * 1000))

    except Exception as exc:
        logger.error("Scrape failed with error: %s", exc, exc_info=True)
        raise
    finally:
        if browser:
            try:
                browser.close()
                logger.info("Browser closed.")
            except Exception:
                pass

    logger.info("Scrape complete. Total unique listings: %d", len(all_listings))
    return all_listings


# ---------------------------------------------------------------------------
# DOM extraction helpers
# ---------------------------------------------------------------------------

def _extract_page_listings(page: Page) -> list[dict]:
    """Extract listing dicts from the current page using JavaScript evaluation.

    Funda's listing cards have two links per listing (image + text).
    The card data lives in a flexRow parent container (@lg:flex-row) that
    contains the address, price, living area, rooms, and energy label.

    We deduplicate by listing_id in the caller.
    """
    raw = page.evaluate("""
        () => {
            const results = [];
            const allLinks = document.querySelectorAll('a[href*="/detail/koop/"]');
            const seen = new Set();

            allLinks.forEach(link => {
                const href = link.getAttribute('href');
                
                // Find the card container (relative overflow-hidden)
                let parent = link.parentElement;
                let card = null;
                for (let d = 0; d < 15; d++) {
                    if (!parent) break;
                    if (parent.classList.contains('relative') && 
                        parent.classList.contains('overflow-hidden')) {
                        card = parent;
                        break;
                    }
                    parent = parent.parentElement;
                }
                if (!card) return;
                
                // Skip duplicates (same card HTML prefix)
                const cardKey = card.innerHTML.substring(0, 200);
                if (seen.has(cardKey)) return;
                seen.add(cardKey);
                
                // Find the flexRow parent that contains the card data
                let flexRow = card.parentElement;
                if (flexRow && flexRow.className && 
                    flexRow.className.indexOf('@lg:flex-row') !== -1) {
                    // Found it
                } else {
                    let el = card.parentElement;
                    for (let d = 0; d < 10; d++) {
                        if (!el) break;
                        if (el.className && 
                            el.className.indexOf('@lg:flex-row') !== -1) {
                            flexRow = el;
                            break;
                        }
                        el = el.parentElement;
                    }
                }
                
                if (!flexRow) return;
                
                const text = flexRow.innerText.trim();
                if (!text || text.length < 10) return;
                
                results.push({ href, text });
            });

            return results;
        }
    """)

    listings = []
    for item in raw:
        data = _extract_listing_data(item["text"], item["href"])
        if data:
            listings.append(data)

    return listings


def _dismiss_cookie_banner(page: Page) -> None:
    """Click the cookie accept button if the banner is visible."""
    try:
        accept_btn = page.locator('button:has-text("Accepteren")')
        if accept_btn.count() > 0 and accept_btn.first.is_visible():
            accept_btn.first.click()
            logger.debug("Dismissed cookie consent banner.")
    except Exception:
        pass  # No banner or already dismissed


# ---------------------------------------------------------------------------
# CLI entry point for manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Funda.nl scraper (standalone)")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (for local debugging; requires display)",
    )
    args = parser.parse_args()

    listings = scrape_funda(
        area="amsterdam",
        offering_type="koop",
        price_min=550000,
        price_max=750000,
        floor_area_min=100,
        bedrooms_min=3,
        max_pages=5,
        headless=not args.headed,
    )

    print(f"\n{'='*60}")
    print(f"Total listings found: {len(listings)}")
    print(f"{'='*60}\n")

    for i, listing in enumerate(listings[:3], 1):
        print(f"--- Listing {i} ---")
        for k, v in listing.items():
            print(f"  {k}: {v}")
        print()