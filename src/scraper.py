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

def build_search_url(
    area: str = "amsterdam",
    offering_type: str = "koop",
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    floor_area_min: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    page: int = 1,
) -> str:
    """Build a Funda search URL with the given filters.

    Funda URL format (discovered by loading the site with Playwright):
        https://www.funda.nl/zoeken/{offering_type}?selected_area={area}
        &price={min}-{max}
        &floor_area={min}-
        &bedrooms={min}-
        &page={n}
    """
    base = f"https://www.funda.nl/zoeken/{offering_type}"
    params = [f"selected_area={area}"]

    if price_min is not None or price_max is not None:
        p_min = price_min if price_min is not None else ""
        p_max = price_max if price_max is not None else ""
        params.append(f"price={p_min}-{p_max}")

    if floor_area_min is not None:
        params.append(f"floor_area={floor_area_min}-")

    if bedrooms_min is not None:
        params.append(f"bedrooms={bedrooms_min}-")

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
    """Parse a Funda listing card's text + href into a dict.

    Returns None if the text is too short or unparseable.
    """
    if not text or len(text) < 10:
        return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    # Address: find the line that looks like a street address
    # Funda cards often have marketing text on line 1, actual address later
    # Look for a line with street name + number + optional postcode
    # Pattern: starts with a word, has a number, optionally followed by postcode
    address_re = re.compile(
        r"^([A-Z][A-Za-z\s\-\.]{2,30}\s+\d{1,4}[\s\-]?[A-Z]{0,2}\d{0,2})$"
    )
    address = None
    for line in lines:
        if address_re.match(line):
            address = line
            break
    if not address:
        address = lines[0]

    # City from URL: /detail/koop/{city}/...
    city_m = re.search(r"/detail/koop/([^/]+)/", href)
    neighborhood = city_m.group(1).replace("-", " ") if city_m else ""

    # Property type from URL slug
    # URL format: /detail/koop/{city}/{type}-{slug}/{id}/
    # e.g. /detail/koop/amsterdam/huis-schaarbeekstraat-71/80913842/
    type_m = re.search(r"/detail/koop/[^/]+/([a-z]+)-", href)
    property_type = type_m.group(1) if type_m else None

    # Price
    price, price_text = parse_price(text)

    # Living area: "XXX m²"
    area_m = re.search(r"(\d+)\s*m\u00b2", text)
    living_area_m2 = int(area_m.group(1)) if area_m else None

    # Plot size (sometimes shown alongside living area): second m² value
    area_matches = re.findall(r"(\d+)\s*m\u00b2", text)
    plot_size_m2 = int(area_matches[1]) if len(area_matches) > 1 else None

    # Bedrooms: single digit followed by energy label on next line
    # Pattern in Funda cards: "\nX\nA" where X is bedrooms, A is energy label
    bed_m = re.search(r"\n(\d)\n([A-G+])", text)
    bedrooms = int(bed_m.group(1)) if bed_m else None

    # Energy label (from the same pattern)
    energy_label = bed_m.group(2) if bed_m else None

    # Listing ID from URL: /detail/koop/.../{id}/
    id_m = re.search(r"/(\d+)/$", href)
    listing_id = id_m.group(1) if id_m else None

    if not listing_id:
        return None

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
        "rooms": None,  # Only on detail pages
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
    bedrooms_min: Optional[int] = None,
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
    bedrooms_min : int or None
        Minimum number of bedrooms.
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
                    bedrooms_min=bedrooms_min,
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
    We deduplicate by listing_id in the caller.
    """
    raw = page.evaluate("""
        () => {
            const results = [];
            const allLinks = document.querySelectorAll('a[href*="/detail/koop/"]');
            const topContainer = document.querySelector('.hide-scrollbar.flex.snap-x.snap-mandatory');

            allLinks.forEach(link => {
                // Skip top-position (paid) listings
                if (topContainer && topContainer.contains(link)) return;

                const href = link.getAttribute('href');
                const parent = link.parentElement;
                const text = parent?.innerText?.trim() || '';

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