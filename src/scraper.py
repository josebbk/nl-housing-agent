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

_ALLOWED_PUBLICATION_DATES = frozenset({1, 3, 5, 10, 30})


def build_search_url(
    area: str = "amsterdam",
    offering_type: str = "koop",
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    publication_date_days: Optional[int] = None,
    floor_area_min: Optional[int] = None,
    bedrooms_min: Optional[int] = None,
    page: int = 1,
) -> str:
    """Build a Funda search URL with the given filters.

    Funda URL format (discovered by loading the site with Playwright):
        https://www.funda.nl/zoeken/{offering_type}?selected_area={area}
        &price={min}-{max}
        &publication_date={n}       (optional: 1, 3, 5, 10, or 30)
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

    if publication_date_days is not None:
        if publication_date_days not in _ALLOWED_PUBLICATION_DATES:
            raise ValueError(
                f"publication_date_days must be one of "
                f"{sorted(_ALLOWED_PUBLICATION_DATES)}, got {publication_date_days}"
            )
        params.append(f"publication_date={publication_date_days}")

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
# Total listing count extraction
# ---------------------------------------------------------------------------

_TOTAL_COUNT_RE = re.compile(
    r"(\d[\d.]*)\s+koopwoningen"
)


def extract_total_listing_count(page_text: str) -> Optional[int]:
    """Extract the total listing count from Funda search results page text.

    Handles confirmed text formats found on Funda:

    * Normal/low count:  "218 koopwoningen"  → 218
    * Low count:         "1 koopwoningen"    → 1
    * Zero count:        "0 koopwoningen binnen jouw zoekwensen"  → 0

    Uses the same thousands-separator convention as price parsing:
    dots are stripped before int() conversion (e.g. "1.234" → 1234).

    Returns None if the count cannot be parsed.  Callers must handle this
    as a fallback — never fail the run over this.
    """
    m = _TOTAL_COUNT_RE.search(page_text)
    if not m:
        return None
    raw = m.group(1).replace(".", "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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
        logger.warning(
            "Listing dropped: href %s does not match expected /detail/koop/.../{id}/ pattern",
            href,
        )
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
    # Promoted/featured cards also have a promo description line before the
    # address (e.g. "Turn-key woning: direct genieten van comfort en stijl!").
    badge_words = {"nieuw", "blikvanger", "advertentie", "verkoop", "verkoopwoning"}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    address = None
    # Build regex for concatenated badge words (e.g. "BlikvangerNieuw").
    # Check if the entire line (lowercased) is composed only of badge words
    # concatenated together. This won't match "Nieuwe Osdorpergracht 265"
    # because "nieuwe" != "nieuw" and "osdorpergracht" is not a badge word.
    concat_pattern = re.compile(
        r'^(' + '|'.join(badge_words) + r')+$',
    )
    for line in lines:
        line_lower = line.lower()
        # Skip badge words — exact match or concatenated badges
        if line_lower in badge_words or concat_pattern.match(line_lower):
            continue
        # Skip lines that are just postcode patterns (those are neighborhood)
        if re.match(r"^\d{4}[A-Z]{2}\s*[A-Z]{0,2}\d{0,2}$", line):
            continue
        # Skip lines that look like a street address with postcode
        # (e.g. "1068 HV Amsterdam" — postcode + city on same line)
        if re.match(r"^\d{4}[A-Z]{2}\s+[A-Z][a-z]+(\s+[A-Z][a-z]+)*$", line):
            continue
        # Skip lines that look like prices
        if "\u20ac" in line:
            continue
        # Skip lines that look like area measurements
        if "m\u00b2" in line:
            continue
        # Skip lines that look like promotional descriptions (full sentences
        # with colons or exclamation marks — Funda's "Blikvanger" cards have
        # a promo description before the actual address).
        if ":" in line or line.endswith("!"):
            continue
        # Skip lines that look like promotional descriptions: typically
        # longer than a street address and may contain commas.
        # Street addresses are usually short (e.g. "Dwarswatering 10").
        if len(line) > 40 and "," in line:
            continue
        # This should be the address
        address = line
        break

    # Rooms is only available on detail pages, not at card level
    rooms = None

    if not address:
        # Fallback: first non-badge line
        concat_pattern = re.compile(
            r'^(' + '|'.join(badge_words) + r')+$',
        )
        for line in lines:
            line_lower = line.lower()
            if line_lower in badge_words or concat_pattern.match(line_lower):
                continue
            if re.match(r"^\d{4}[A-Z]{2}\s*[A-Z]{0,2}\d{0,2}$", line):
                continue
            if re.match(r"^\d{4}[A-Z]{2}\s+[A-Z][a-z]+(\s+[A-Z][a-z]+)*$", line):
                continue
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
    publication_date_days: Optional[int] = None,
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
    publication_date_days : int or None
        Publication date filter — only listings published within the last
        N days. Accepted values: 1, 3, 5, 10, 30.  None (default) means
        no publication-date filter is applied.
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

            # --- Determine dynamic page count from page 1 ---
            page1_url = build_search_url(
                area=area,
                offering_type=offering_type,
                price_min=price_min,
                price_max=price_max,
                publication_date_days=publication_date_days,
                floor_area_min=floor_area_min,
                bedrooms_min=bedrooms_min,
                page=1,
            )
            logger.info("Fetching page 1 to detect total listing count: %s", page1_url)

            try:
                page1_html = _fetch_page_html(page1_url, timeout=30)
            except Exception as exc:
                logger.error("HTTP fetch failed on page 1: %s", exc)
                pages_to_scrape = max_pages
            else:
                data_url = "data:text/html," + urllib.parse.quote(page1_html)
                page.goto(data_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(random.uniform(3000, 5000))
                _dismiss_cookie_banner(page)
                page.wait_for_timeout(random.uniform(2000, 4000))

                # Extract total count from the rendered page text.
                # This uses the same page-text source (document.body.innerText)
                # that _extract_page_listings already works with.
                page_text = page.evaluate("() => document.body.innerText")
                total_count = extract_total_listing_count(page_text)

                if total_count is not None and total_count > 0:
                    computed_pages = (total_count + 14) // 15  # ceil(total / 15)
                    pages_to_scrape = min(max_pages, computed_pages)
                    logger.info(
                        "Total listing count: %d → computed pages: %d, "
                        "scraping %d page(s) (max_pages=%d)",
                        total_count, computed_pages, pages_to_scrape, max_pages,
                    )
                else:
                    if total_count == 0:
                        pages_to_scrape = 0
                        logger.info(
                            "Total listing count is 0 — scraping 0 pages.",
                        )
                    else:
                        pages_to_scrape = max_pages
                        logger.warning(
                            "Could not extract total listing count from page 1; "
                            "falling back to max_pages=%d.",
                            max_pages,
                        )

            for page_num in range(1, pages_to_scrape + 1):
                url = build_search_url(
                    area=area,
                    offering_type=offering_type,
                    price_min=price_min,
                    price_max=price_max,
                    publication_date_days=publication_date_days,
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
                page_listings, diag = _extract_page_listings(page)
                new_count = 0

                logger.info(
                    "Page %d diagnostics: total_links=%d, after_href_dedup=%d, "
                    "no_card=%d, fallback_used=%d, no_flexrow=%d, short_text=%d, "
                    "retained=%d",
                    page_num,
                    diag["total_links"],
                    diag["after_href_dedup"],
                    diag["dropped_no_card"],
                    diag["fallback_used"],
                    diag["dropped_no_flexrow"],
                    diag["dropped_short_text"],
                    diag["results"],
                )
                if diag["dropped_hrefs_no_card"]:
                    logger.warning(
                        "Page %d: dropped %d links (no card ancestor): %s",
                        page_num,
                        len(diag["dropped_hrefs_no_card"]),
                        diag["dropped_hrefs_no_card"][:10],
                    )
                if diag["dropped_hrefs_no_flexrow"]:
                    logger.warning(
                        "Page %d: dropped %d links (no flexRow ancestor): %s",
                        page_num,
                        len(diag["dropped_hrefs_no_flexrow"]),
                        diag["dropped_hrefs_no_flexrow"][:10],
                    )
                if diag["dropped_hrefs_short_text"]:
                    logger.warning(
                        "Page %d: dropped %d links (short text): %s",
                        page_num,
                        len(diag["dropped_hrefs_short_text"]),
                        diag["dropped_hrefs_short_text"][:10],
                    )

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
                if page_num < pages_to_scrape:
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

    Funda uses two card templates:
    1. Standard cards: link → relative/overflow-hidden card → @lg:flex-row
    2. Promoted/featured cards: link → parent div with no wrapper classes,
       but the parent's innerText contains the listing data directly.

    We deduplicate by listing_id in the caller.

    Returns a tuple of (listings, diagnostics) where diagnostics is a dict
    with per-stage counts for debugging listing loss.
    """
    raw = page.evaluate("""
        () => {
            const results = [];
            const allLinks = document.querySelectorAll('a[href*="/detail/koop/"]');
            const seen = new Set();

            const diag = {
                total_links: allLinks.length,
                after_href_dedup: 0,
                dropped_no_card: 0,
                dropped_no_flexrow: 0,
                fallback_used: 0,
                dropped_short_text: 0,
                results: 0,
                dropped_hrefs_no_card: [],
                dropped_hrefs_no_flexrow: [],
                dropped_hrefs_short_text: [],
            };

            allLinks.forEach(link => {
                const href = link.getAttribute('href');
                
                // Deduplicate by href — each unique listing has exactly one
                // href. Multiple links on the same card (image + text) share
                // the same href, so this correctly skips them. Using href
                // instead of card HTML prefix avoids false collisions:
                // Funda's card template has identical CSS classes and structure
                // for every card, so the first 200 chars of innerHTML are
                // nearly identical across different listings.
                if (seen.has(href)) return;
                seen.add(href);
                diag.after_href_dedup++;
                
                // Try standard path: find the card container (relative overflow-hidden)
                // then the flexRow parent that contains the listing data.
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
                
                let text = null;
                
                if (card) {
                    // Standard path: find the flexRow parent
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
                    
                    if (flexRow) {
                        text = flexRow.innerText.trim();
                    } else {
                        diag.dropped_no_flexrow++;
                        diag.dropped_hrefs_no_flexrow.push(href);
                        return;
                    }
                } else {
                    // Promoted/featured card fallback: no relative/overflow-hidden
                    // wrapper. The link's parent div itself contains the listing text.
                    diag.dropped_no_card++;
                    diag.dropped_hrefs_no_card.push(href);
                    
                    const lp = link.parentElement;
                    if (lp) {
                        const lpText = lp.innerText.trim();
                        // Accept if it has a price and enough content
                        if (lpText && lpText.length >= 10 && lpText.includes('\u20ac')) {
                            text = lpText;
                            diag.fallback_used++;
                        }
                    }
                }
                
                if (!text || text.length < 10) {
                    diag.dropped_short_text++;
                    diag.dropped_hrefs_short_text.push(href);
                    return;
                }
                
                diag.results++;
                results.push({ href, text });
            });

            return { results, diag };
        }
    """)

    listings = []
    for item in raw["results"]:
        data = _extract_listing_data(item["text"], item["href"])
        if data:
            listings.append(data)

    return listings, raw["diag"]


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