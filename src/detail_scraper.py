"""
Fetch and parse a single Funda listing detail page.

Reuses the existing urllib-headers -> data: URL -> Playwright rendering
technique from scraper.py (same Akamai bypass). Returns a dict with all
detail fields. Any field that cannot be parsed is set to None — never
omitted, never guessed.

Must not raise on missing/absent sections — absence is expected and normal.
"""

import logging
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP fetching — reuses the same Akamai-bypass technique from scraper.py
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
    """Fetch a Funda page using urllib with realistic browser headers."""
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    response = urllib.request.urlopen(req, timeout=timeout)
    return response.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Data model — all fields nullable, never fabricated
# ---------------------------------------------------------------------------

@dataclass
class DetailData:
    """Parsed detail page data. All fields nullable."""
    ownership_type: Optional[str] = None
    erfpacht_canon_annual: Optional[float] = None
    garden_present: Optional[bool] = None
    garden_type: Optional[str] = None
    garden_size_m2: Optional[int] = None
    garden_orientation: Optional[str] = None
    balcony_present: Optional[bool] = None
    building_bound_outdoor_m2: Optional[int] = None
    garage_type: Optional[str] = None
    parking_type: Optional[str] = None
    insulation_raw: Optional[str] = None
    insulation_score: Optional[float] = None
    heating_type: Optional[str] = None
    boiler_year: Optional[int] = None
    amenities_raw: Optional[str] = None
    amenities_matched: list = field(default_factory=list)
    bathrooms: Optional[int] = None
    neighborhood_avg_price_m2: Optional[float] = None
    detail_fetched_at: Optional[str] = None
    rooms: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Page text extraction
# ---------------------------------------------------------------------------

def _extract_page_text(page: Page) -> str:
    """Extract all visible text from the current page via JS evaluation."""
    return page.evaluate("""
        () => {
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_ELEMENT,
                {
                    acceptNode: (node) => {
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden') {
                            return NodeFilter.FILTER_REJECT;
                        }
                        return NodeFilter.FILTER_ACCEPT;
                    }
                }
            );
            const texts = [];
            while (walker.nextNode()) {
                const text = walker.currentNode.textContent.trim();
                if (text) texts.push(text);
            }
            return texts.join('\\n');
        }
    """)


# ---------------------------------------------------------------------------
# Section parsing helpers
# ---------------------------------------------------------------------------

def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split page text into (section_heading, section_body) tuples.

    Funda detail pages are organized into sections with headings like
    "Kenmerken", "Buitenruimte", "Voorzieningen", etc.
    """
    sections = []
    current_heading = ""
    current_body_lines = []

    # Known section headings on Funda detail pages
    known_sections = {
        "kenmerken", "buitenruimte", "voorzieningen", "isolatie",
        "garage en parkeergelegenheid", "energie", "buurt",
        " Kadastrale gegevens", " Kadastraal", "locatie",
    }

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this line looks like a section heading
        # Section headings are typically standalone lines that match known sections
        is_heading = False
        for section in known_sections:
            if stripped.lower().replace("  ", " ") == section.lower().replace("  ", " "):
                is_heading = True
                break

        # Also check for lines that are short and followed by key-value pairs
        if not is_heading and len(stripped) < 50 and ":" not in stripped:
            # Could be a heading if it's short and doesn't contain ":"
            # (key-value pairs contain ":")
            pass

        if is_heading:
            if current_heading:
                sections.append((current_heading, "\n".join(current_body_lines)))
            current_heading = stripped
            current_body_lines = []
        else:
            current_body_lines.append(stripped)

    if current_heading:
        sections.append((current_heading, "\n".join(current_body_lines)))

    return sections


def _find_section(sections: list[tuple[str, str]], keyword: str) -> Optional[str]:
    """Find a section body by keyword matching the heading."""
    for heading, body in sections:
        if keyword.lower() in heading.lower():
            return body
    return None


def _find_text_block(text: str, heading: str) -> Optional[str]:
    """Find a text block under a heading in the raw page text.

    Looks for the heading line, then returns all text until the next
    major heading or end of text.
    """
    lines = text.split("\n")
    heading_lower = heading.lower()
    heading_idx = None

    for i, line in enumerate(lines):
        if heading_lower in line.lower():
            heading_idx = i
            break

    if heading_idx is None:
        return None

    # Find the end of this section (next heading or end)
    known_sections = {
        "kenmerken", "buitenruimte", "voorzieningen", "isolatie",
        "garage en parkeergelegenheid", "energie", "buurt",
        "kadastrale gegevens", "kadastraal", "locatie",
    }

    end_idx = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        stripped = lines[i].strip().lower()
        for section in known_sections:
            if stripped == section:
                end_idx = i
                break
        if end_idx != len(lines):
            break

    return "\n".join(lines[heading_idx + 1:end_idx]).strip()


def _extract_field_value(body: str, field_name: str) -> Optional[str]:
    """Extract a field value from a section body by field name.

    Funda detail pages use "Field name: value" format.
    """
    # Try exact match with colon
    pattern = re.compile(
        re.escape(field_name) + r"\s*[:\-]\s*(.+)",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(body)
    if m:
        return m.group(1).strip()

    # Try without colon (just the field name on one line, value on next)
    pattern2 = re.compile(
        re.escape(field_name) + r"\s*\n\s*(.+)",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern2.search(body)
    if m:
        return m.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# Field extraction functions
# ---------------------------------------------------------------------------

def _extract_ownership(body: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """Extract ownership_type and erfpacht_canon_annual from Kenmerken body."""
    if not body:
        return None, None

    ownership_type = None
    erfpacht_canon = None

    # Check for eigendomssituatie
    eigendom = _extract_field_value(body, "Eigendomssituatie")
    if eigendom:
        if "erfpacht" in eigendom.lower():
            ownership_type = "erfpacht"
        elif "volle eigendom" in eigendom.lower():
            ownership_type = "full"

    # If not found in Eigendomssituatie, check the whole body
    if not ownership_type:
        if "erfpacht" in body.lower():
            ownership_type = "erfpacht"
        elif "volle eigendom" in body.lower():
            ownership_type = "full"

    # Extract erfpacht canon (annual last)
    # Look for patterns like "Lasten: € 123/year" or "€ 123 per jaar"
    canon_patterns = [
        r"€\s*([\d.]+)\s*(?:per\s*)?jaar",
        r"€\s*([\d.]+)\s*/\s*jaar",
        r"€\s*([\d.]+)\s*jaarlijks",
        r"lasten.*?€\s*([\d.]+)",
    ]
    for pattern in canon_patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(".", "").replace(",", ".")
            try:
                erfpacht_canon = float(raw)
            except ValueError:
                pass
            break

    return ownership_type, erfpacht_canon


def _extract_garden(body: Optional[str]) -> dict:
    """Extract garden fields from Buitenruimte body."""
    if not body:
        return {
            "garden_present": None,
            "garden_type": None,
            "garden_size_m2": None,
            "garden_orientation": None,
        }

    garden_present = None
    garden_type = None
    garden_size_m2 = None
    garden_orientation = None

    # garden_present: True if "Tuin" field exists at all
    if re.search(r"Tuin\s*[:\-]", body, re.IGNORECASE):
        garden_present = True

    # garden_type: raw value of Tuin field
    if garden_present:
        garden_type = _extract_field_value(body, "Tuin")

    # garden_size_m2: regex for (\d+)\s*m² in tuin-related text
    size_patterns = [
        r"Achtertuin\s*[:\-]?\s*(\d+)\s*m\u00b2",
        r"Tuin\s*[:\-]?\s*(\d+)\s*m\u00b2",
        r"grootte.*?(\d+)\s*m\u00b2",
    ]
    for pattern in size_patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            try:
                garden_size_m2 = int(m.group(1))
            except ValueError:
                pass
            break

    # garden_orientation: "Ligging tuin"
    orientation = _extract_field_value(body, "Ligging tuin")
    if orientation:
        garden_orientation = orientation

    return {
        "garden_present": garden_present,
        "garden_type": garden_type,
        "garden_size_m2": garden_size_m2,
        "garden_orientation": garden_orientation,
    }


def _extract_balcony(body: Optional[str]) -> Optional[bool]:
    """Extract balcony_present from body."""
    if not body:
        return None

    balcony_text = _extract_field_value(body, "Balkon/dakterras")
    if balcony_text and "aanwezig" in balcony_text.lower():
        return True
    return None


def _extract_building_bound_outdoor(body: Optional[str]) -> Optional[int]:
    """Extract building_bound_outdoor_m2 from body."""
    if not body:
        return None

    text = body
    m = re.search(r"Gebouwgebonden buitenruimte\s*[:\-]?\s*(\d+)\s*m\u00b2", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extract_garage_type(body: Optional[str]) -> Optional[str]:
    """Extract garage_type from body."""
    if not body:
        return None

    garage_text = _extract_field_value(body, "Soort garage")
    if not garage_text:
        return None

    garage_lower = garage_text.lower()
    if "niet aanwezig" in garage_lower:
        return "none"
    elif any(w in garage_lower for w in ["aangebouwd", "inpandig"]):
        return "attached"
    elif "vrijstaand" in garage_lower:
        return "detached"
    elif "carport" in garage_lower:
        return "carport"

    return garage_text


def _extract_parking_type(body: Optional[str]) -> Optional[str]:
    """Extract parking_type from body."""
    if not body:
        return None

    parking_text = _extract_field_value(body, "Soort parkeergelegenheid")
    if not parking_text:
        return None

    parking_lower = parking_text.lower()
    # Priority order: private > carport > public > paid
    if "eigen terrein" in parking_lower:
        return "private"
    elif "carport" in parking_lower:
        return "carport"
    elif "openbaar" in parking_lower:
        return "public"
    elif "betaald" in parking_lower:
        return "paid"

    return parking_text


def _extract_insulation(body: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """Extract insulation_raw and compute insulation_score."""
    if not body:
        return None, None

    raw = _extract_field_value(body, "Isolatie")
    if raw is None:
        # Try finding "Isolatie" as a standalone field
        m = re.search(r"Isolatie\s*[:\-]?\s*(.+)", body, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()

    if not raw:
        return None, None

    score = _compute_insulation_score(raw)
    return raw, score


def _compute_insulation_score(raw: str) -> Optional[float]:
    """Compute insulation score from raw text.

    Counts component keywords (max 3) and glass quality tier.
    Returns a float in [0, 1].
    """
    raw_lower = raw.lower()

    # Count component matches
    components = ["dakisolatie", "vloerisolatie", "muurisolatie", "spouwmuurisolatie"]
    component_count = sum(1 for c in components if c in raw_lower)
    component_score = min(component_count, 3) / 3.0  # cap at 3

    # Glass quality tier
    glass_tiers = ["enkel glas", "dubbel glas", "dubbelglas", "hr-glas", "hr glas", "hr+", "hr++"]
    glass_score = 0.0
    for tier in glass_tiers:
        if tier in raw_lower:
            tier_idx = glass_tiers.index(tier)
            glass_score = tier_idx / (len(glass_tiers) - 1)  # normalize to [0, 1]
            break

    # Composite: 60% components, 40% glass
    return round(component_score * 0.6 + glass_score * 0.4, 4)


def _extract_heating(body: Optional[str]) -> Optional[str]:
    """Extract heating_type from body."""
    if not body:
        return None

    heating_text = _extract_field_value(body, "Verwarming")
    if not heating_text:
        return None

    heating_lower = heating_text.lower()
    if "warmtepomp" in heating_lower:
        return "heat_pump"
    elif "stadsverwarming" in heating_lower or "blokverwarming" in heating_lower:
        return "district"
    elif "cv-ketel" in heating_lower:
        return "gas_boiler"

    return None


def _extract_boiler_year(body: Optional[str]) -> Optional[int]:
    """Extract boiler_year from body (Cv-ketel descriptive text)."""
    if not body:
        return None

    # Look for "uit YYYY" pattern
    m = re.search(r"uit\s+(\d{4})", body)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extract_amenities(body: Optional[str]) -> tuple[Optional[str], list]:
    """Extract amenities_raw from Voorzieningen body."""
    if not body:
        return None, []

    raw = _extract_field_value(body, "Voorzieningen")
    if not raw:
        # Try finding Voorzieningen as a standalone heading
        m = re.search(r"Voorzieningen\s*[:\-]?\s*(.+)", body, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()

    return raw, []  # matched list computed by scoring.py


def _extract_bathrooms(body: Optional[str]) -> Optional[int]:
    """Extract bathrooms count from body."""
    if not body:
        return None

    m = re.search(r"(\d+)\s*badkamer", body, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extract_neighborhood_avg_price(body: Optional[str]) -> Optional[float]:
    """Extract neighborhood_avg_price_m2 from Buurt section."""
    if not body:
        return None

    # "Gem. vraagprijs / m²" followed by a number
    m = re.search(r"Gem\.?\s*vraagprijs.*?[/\s]m\u00b2.*?€?\s*([\d.]+)", body, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass

    # Fallback: just look for price per m² pattern
    m = re.search(r"€?\s*([\d.]+)\s*/\s*m\u00b2", body, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass

    return None


def _extract_rooms(body: Optional[str]) -> Optional[int]:
    """Extract number of rooms from Kenmerken body."""
    if not body:
        return None

    m = re.search(r"Aantal kamers\s*[:\-]?\s*(\d+)", body, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

def fetch_listing_details(url: str) -> dict:
    """Fetch and parse a single Funda listing detail page.

    Reuses the existing urllib-headers -> data: URL -> Playwright rendering
    technique already implemented in scraper.py (same Akamai bypass).

    Returns a dict with all detail fields (Section 3 of the spec).
    Any field that cannot be parsed is set to None — never omitted, never guessed.
    Must not raise on missing/absent sections — absence is expected and normal.
    """
    result = DetailData(detail_fetched_at=datetime.now(timezone.utc).isoformat())

    try:
        logger.info("Fetching detail page: %s", url)

        # Step 1: Fetch HTML with urllib (Akamai bypass)
        html = _fetch_page_html(url, timeout=30)

        # Step 2: Load into Playwright for JS rendering
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    locale="nl-NL",
                    user_agent=_BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                    timezone_id="Europe/Amsterdam",
                )
                page = context.new_page()

                data_url = "data:text/html," + urllib.parse.quote(html)
                page.goto(data_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(random.uniform(2000, 4000))

                # Extract all visible text
                text = _extract_page_text(page)

                # Split into sections and parse
                sections = _split_sections(text)

                # Kenmerken section — most fields
                kenmerken_body = _find_section(sections, "Kenmerken")
                if not kenmerken_body:
                    kenmerken_body = _find_text_block(text, "Kenmerken")

                # --- Ownership ---
                ownership_type, erfpacht_canon = _extract_ownership(kenmerken_body)
                result.ownership_type = ownership_type
                result.erfpacht_canon_annual = erfpacht_canon

                # --- Rooms ---
                rooms = _extract_rooms(kenmerken_body)
                if rooms:
                    result.rooms = rooms

                # --- Buitenruimte section ---
                buitenruimte_body = _find_section(sections, "Buitenruimte")
                if not buitenruimte_body:
                    buitenruimte_body = _find_text_block(text, "Buitenruimte")

                garden_data = _extract_garden(buitenruimte_body)
                result.garden_present = garden_data["garden_present"]
                result.garden_type = garden_data["garden_type"]
                result.garden_size_m2 = garden_data["garden_size_m2"]
                result.garden_orientation = garden_data["garden_orientation"]

                # Balcony — may be in Buitenruimte
                balcony = _extract_balcony(buitenruimte_body)
                if balcony is not None:
                    result.balcony_present = balcony

                # Building bound outdoor — may be in Buitenruimte
                building_outdoor = _extract_building_bound_outdoor(buitenruimte_body)
                if building_outdoor is not None:
                    result.building_bound_outdoor_m2 = building_outdoor

                # --- Garage and parking ---
                garage_body = _find_section(sections, "Garage en parkeergelegenheid")
                if not garage_body:
                    garage_body = _find_text_block(text, "Garage en parkeergelegenheid")

                # Also check Kenmerken for garage (one sample had "Carport" there)
                garage_from_kenmerken = _extract_garage_type(kenmerken_body)
                if not result.garage_type and garage_from_kenmerken:
                    result.garage_type = garage_from_kenmerken

                if garage_body:
                    garage_type = _extract_garage_type(garage_body)
                    if garage_type:
                        result.garage_type = garage_type
                    parking_type = _extract_parking_type(garage_body)
                    if parking_type:
                        result.parking_type = parking_type

                # --- Isolatie section ---
                isolatie_body = _find_section(sections, "Isolatie")
                if not isolatie_body:
                    isolatie_body = _find_text_block(text, "Isolatie")

                insulation_raw, insulation_score = _extract_insulation(isolatie_body)
                result.insulation_raw = insulation_raw
                result.insulation_score = insulation_score

                # --- Verwarming ---
                heating = _extract_heating(isolatie_body)
                if not heating:
                    verwarming_body = _find_text_block(text, "Verwarming")
                    heating = _extract_heating(verwarming_body)
                result.heating_type = heating

                # --- Cv-ketel (boiler year) ---
                cv_body = _find_text_block(text, "Cv-ketel")
                if not cv_body:
                    cv_body = isolatie_body  # sometimes cv-ketel is in isolatie section
                boiler_year = _extract_boiler_year(cv_body)
                if boiler_year:
                    result.boiler_year = boiler_year

                # --- Voorzieningen (amenities) ---
                voorzieningen_body = _find_section(sections, "Voorzieningen")
                if not voorzieningen_body:
                    voorzieningen_body = _find_text_block(text, "Voorzieningen")
                amenities_raw, _ = _extract_amenities(voorzieningen_body)
                result.amenities_raw = amenities_raw

                # --- Aantal badkamers ---
                bathrooms = _extract_bathrooms(kenmerken_body)
                if bathrooms:
                    result.bathrooms = bathrooms

                # --- Buurt section ---
                buurt_body = _find_section(sections, "Buurt")
                if not buurt_body:
                    buurt_body = _find_text_block(text, "Buurt")
                neighborhood_avg = _extract_neighborhood_avg_price(buurt_body)
                if neighborhood_avg is not None:
                    result.neighborhood_avg_price_m2 = neighborhood_avg

            finally:
                browser.close()

    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s (returning partial data)", url, exc)
        # Return whatever we have — all fields will be None if we failed early
        result.detail_fetched_at = datetime.now(timezone.utc).isoformat()

    return result.to_dict()