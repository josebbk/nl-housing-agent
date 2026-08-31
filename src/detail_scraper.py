"""
Fetch and parse a single Funda listing detail page.

Reuses the existing urllib-headers -> data: URL -> Playwright rendering
technique from scraper.py (same Akamai bypass). Returns a dict with all
detail fields. Any field that cannot be parsed is set to None — never
omitted, never guessed.

Must not raise on missing/absent sections — absence is expected and normal.

Also extracts property photo URLs from the rendered detail page. Photos are
collected in gallery document order (hero/facade photo first), restricted
to Funda's own image CDN, deduplicated across size variants, and capped at
_MAX_IMAGE_URLS so downstream consumers (notifier.py) can deterministically
select the first three.
"""

import logging
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, Page, Browser

logger = logging.getLogger(__name__)

# Property photos live on Funda's own CDN under a "valentina" media path.
# Confirmed live 2026-08-23:
#   https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1080
#   https://cloud.funda.nl/valentina_media/230/205/775_1440x960.jpg
_PROPERTY_IMAGE_HOST = "cloud.funda.nl"
_PROPERTY_IMAGE_PATH_MARKER = "/valentina"
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)$", re.IGNORECASE)
# Sized variants embed the dimensions before the extension: ..._1440x960.jpg
_SIZE_SUFFIX_RE = re.compile(r"_\d+x\d+(?=\.\w+$)", re.IGNORECASE)
# Canonical download resolution requested from the CDN (?options=width=N).
_PROPERTY_IMAGE_WIDTH = 1440
# Upper bound on returned photo URLs (bounded payload; notifier uses 3).
_MAX_IMAGE_URLS = 10

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
    bathrooms: Optional[int] = None
    stories: Optional[int] = None
    has_attic: bool = False
    neighborhood_avg_price_m2: Optional[float] = None
    detail_fetched_at: Optional[str] = None
    rooms: Optional[int] = None
    energy_label: Optional[str] = None
    property_type: Optional[str] = None
    year_built: Optional[int] = None
    status: Optional[str] = None
    plot_size_m2: Optional[int] = None
    neighborhood: Optional[str] = None
    image_urls: Optional[list] = None
    description: Optional[str] = None

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

# Known top-level section headings on Funda detail pages
_TOP_LEVEL_SECTIONS = {
    "kenmerken", "buitenruimte", "isolatie",
    "garage en parkeergelegenheid", "energie", "buurt",
    "locatie", "indeling", "beschrijving", "omschrijving",
}

# Known subsection headings within Kenmerken
_KENMERKEN_SUBSECTIONS = {
    "overdracht", "bouw", "specifiek", "oppervlakten en inhoud",
    "indeling", "energie", "kadastrale gegevens", "buitenruimte",
    "garage", "parkeergelegenheid", "bergruimte",
}


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split page text into (section_heading, section_body) tuples.

    Funda detail pages are organized into sections with headings like
    "Kenmerken", "Buitenruimte", "Voorzieningen", etc.

    Handles both newline-separated sections and concatenated text where
    section headings appear without spaces between them.
    """
    sections = []
    current_heading = ""
    current_body_lines = []

    # First, try splitting by actual newlines
    lines = text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this line is a standalone section heading
        is_heading = False
        for section in _TOP_LEVEL_SECTIONS:
            if stripped.lower() == section.lower():
                is_heading = True
                break

        if is_heading:
            if current_heading:
                sections.append((current_heading, "\n".join(current_body_lines)))
            current_heading = stripped
            current_body_lines = []
        else:
            current_body_lines.append(stripped)

    if current_heading:
        sections.append((current_heading, "\n".join(current_body_lines)))

    # If no sections were found (text is all concatenated without newlines),
    # try to split by known section headings embedded in the text
    if not sections and text:
        sections = _split_concatenated_sections(text)

    return sections


def _split_concatenated_sections(text: str) -> list[tuple[str, str]]:
    """Split text where section headings are concatenated without spaces.

    E.g., "KenmerkenOverdrachtAangeboden..." splits into:
    ("Kenmerken", "OverdrachtAangeboden...")
    ("Overdracht", "Aangeboden...")
    """
    sections = []
    # Sort sections by length (longest first) to match "oppervlakten en inhoud"
    # before "oppervlakten"
    sorted_sections = sorted(_TOP_LEVEL_SECTIONS, key=len, reverse=True)

    # Find all heading positions
    heading_positions = []
    for section in sorted_sections:
        pattern = re.compile(
            r'(?i)(?<!\w)' + re.escape(section) + r'(?!\w)'
        )
        for m in pattern.finditer(text):
            heading_positions.append((m.start(), section))

    # Sort by position
    heading_positions.sort(key=lambda x: x[0])

    if not heading_positions:
        return []

    # Extract sections between heading positions
    for i, (pos, heading) in enumerate(heading_positions):
        if i + 1 < len(heading_positions):
            next_pos = heading_positions[i + 1][0]
            body = text[pos + len(heading):next_pos]
        else:
            body = text[pos + len(heading):]
        sections.append((heading, body.strip()))

    return sections


def _split_kenmerken_subsections(body: str) -> list[tuple[str, str]]:
    """Split Kenmerken body into subsections.

    Kenmerken contains subsections like "Overdracht", "Bouw", "Energie", etc.
    These appear as headings within the Kenmerken text, often concatenated
    with the previous field value (no separator).
    """
    if not body:
        return []

    # Sort by length (longest first) to match multi-word sections first
    sorted_subsections = sorted(_KENMERKEN_SUBSECTIONS, key=len, reverse=True)

    heading_positions = []
    for subsection in sorted_subsections:
        # No word-boundary assertions — subsections may be embedded in
        # concatenated text (e.g., "achteromGarageSoort" or "erfpachtBuitenruimte")
        pattern = re.compile(
            r'(?i)' + re.escape(subsection),
        )
        for m in pattern.finditer(body):
            # Special case: skip "buitenruimte" when it's part of
            # "gebouwgebonden buitenruimte" (not a subsection heading)
            if subsection == "buitenruimte":
                start = m.start()
                # Check if preceded by "gebouwgebonden" (case-insensitive)
                preceding = body[max(0, start - 20):start].lower()
                if "gebouwgebonden" in preceding:
                    continue
            heading_positions.append((m.start(), subsection))

    heading_positions.sort(key=lambda x: x[0])

    if not heading_positions:
        return []

    # Deduplicate: keep only the first occurrence of each subsection name
    seen = set()
    unique_positions = []
    for pos, heading in heading_positions:
        if heading not in seen:
            seen.add(heading)
            unique_positions.append((pos, heading))

    subsections = []
    for i, (pos, heading) in enumerate(unique_positions):
        if i + 1 < len(unique_positions):
            next_pos = unique_positions[i + 1][0]
            subsection_body = body[pos + len(heading):next_pos]
        else:
            subsection_body = body[pos + len(heading):]
        subsections.append((heading, subsection_body.strip()))

    return subsections


def _find_section(sections: list[tuple[str, str]], keyword: str) -> Optional[str]:
    """Find a section body by keyword matching the heading."""
    for heading, body in sections:
        if keyword.lower() in heading.lower():
            return body
    return None


def _find_section_body(
    sections: list[tuple[str, str]], keyword: str
) -> Optional[str]:
    """Find a section body by keyword matching the heading.

    Returns the body of the first non-empty section whose heading contains
    the keyword. Skips empty bodies (Funda sometimes has duplicate section
    headings where only some have content).
    """
    for heading, body in sections:
        if keyword.lower() in heading.lower() and body:
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
    end_idx = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        stripped = lines[i].strip().lower()
        for section in _TOP_LEVEL_SECTIONS:
            if stripped == section:
                end_idx = i
                break
        if end_idx != len(lines):
            break

    return "\n".join(lines[heading_idx + 1:end_idx]).strip()


def _extract_field_value(body: str, field_name: str) -> Optional[str]:
    """Extract a field value from a section body by field name.

    Funda detail pages use "Field name: value" format.
    Also handles concatenated format: "FieldnameValue" (no separator).
    """
    # Try with colon/hyphen separator first
    pattern = re.compile(
        re.escape(field_name) + r"\s*[:\-]\s*(.+)",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(body)
    if m:
        return m.group(1).strip()

    # Try without separator (just the field name, value immediately follows)
    # Use word boundary to avoid matching partial field names
    pattern2 = re.compile(
        r'(?<!\w)' + re.escape(field_name) + r'(\S.*)$',
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern2.search(body)
    if m:
        return m.group(1).strip()

    return None


def _extract_field_until_next(
    body: str, field_name: str, next_fields: Optional[list[str]] = None
) -> Optional[str]:
    """Extract a field value that has no separator between name and value.

    Funda's rendered text often has duplicate content: a concatenated block
    followed by a newline-separated block. This function prefers the
    newline-separated format where each field is on its own line.

    Args:
        body: The text body to search.
        field_name: The field name to find.
        next_fields: Optional list of known next field names to use as
            boundaries for free-text fields.

    Returns:
        The extracted field value (first line only), or None if not found.
    """
    if not body:
        return None

    escaped = re.escape(field_name)

    # Strategy 1: Try newline-separated format (most reliable)
    # Look for "^field_name\nvalue\n" pattern where value is on its own line
    if next_fields:
        nl_boundary = r'(?:\n' + '|'.join(next_fields) + r')'
    else:
        nl_boundary = r'\n'
    nl_pattern = re.compile(
        r'(?m)^' + escaped + r'\n(.*?)(?=' + nl_boundary + r'$)',
        re.DOTALL,
    )
    m = nl_pattern.search(body)
    if m:
        val = m.group(1).strip()
        if val:
            return val.split('\n')[0].strip()

    # Strategy 2: Fallback — capture until next newline (for fields where
    # the next line is a duplicate value, not a field name)
    fallback_pattern = re.compile(
        r'(?m)^' + escaped + r'\n(.*?)(?=\n)',
        re.DOTALL,
    )
    m = fallback_pattern.search(body)
    if m:
        val = m.group(1).strip()
        if val:
            return val.split('\n')[0].strip()

    # Strategy 3: Try concatenated format
    # Funda concatenates fields like "EnergielabelCIsolatieDakisolatie..."
    if next_fields:
        # Match next field at start of line, or inline, or end of string.
        # Also add `$` as a final fallback so non-greedy capture doesn't
        # fail when no next_field is found (e.g., "Soort garageCarport"
        # where "Carport" is not in next_fields).
        next_pattern = "|".join(
            f"(?:\\n{n})|(?:{n})|(?:{n}$)" for n in next_fields
        ) + "|$"
        pattern = re.compile(
            escaped + r'(\S.*?)(?:' + next_pattern + r')',
            re.IGNORECASE | re.DOTALL,
        )
    else:
        pattern = re.compile(
            escaped + r'(\S.*?)(?:\n|$)',
            re.IGNORECASE | re.DOTALL,
        )

    m = pattern.search(body)
    if m:
        val = m.group(1).strip()
        if val:
            return val.split('\n')[0].strip()
    return None


def _extract_field_no_sep(body: str, field_name: str) -> Optional[str]:
    """Extract a field value where there is NO separator between name and value.

    Funda uses formats like "Bouwjaar1969" (no space/colon between field
    name and value). This function handles that format.

    For fields with known value patterns (numbers, letters), uses a
    pattern-specific regex. For free-text fields, captures until the next
    known field or section heading.
    """
    escaped = re.escape(field_name)
    pattern = re.compile(
        r'(?<!\w)' + escaped + r'(\S.*)$',
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(body)
    if m:
        return m.group(1).strip()
    return None


def _extract_field_number(
    body: str, field_name: str, suffix: str = ""
) -> Optional[int]:
    """Extract a numeric field value with optional suffix.

    E.g., "Perceel163 m²" → 163, "Bouwjaar1969" → 1969.
    """
    escaped = re.escape(field_name)
    suffix_pattern = re.escape(suffix) if suffix else ""
    pattern = re.compile(
        r'(?<!\w)' + escaped + r'\s*(\d+)' + suffix_pattern,
        re.IGNORECASE,
    )
    m = pattern.search(body)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Field extraction functions
# ---------------------------------------------------------------------------

def _extract_ownership(
    kenmerken_body: Optional[str],
    subsections: list[tuple[str, str]],
) -> tuple[Optional[str], Optional[float]]:
    """Extract ownership_type and erfpacht_canon_annual from Kenmerken.

    ownership_type is in "Kadastrale gegevens" subsection → "Eigendomssituatie".
    erfpacht_canon_annual is in "Kadastrale gegevens" subsection → "Lasten".
    """
    if not kenmerken_body:
        return None, None

    ownership_type = None
    erfpacht_canon = None

    # Find "Kadastrale gegevens" subsection
    kadastrale_body = None
    for heading, body in subsections:
        if "kadastrale" in heading.lower() or "kadastraal" in heading.lower():
            kadastrale_body = body
            break

    if kadastrale_body:
        # Extract ownership from "Eigendomssituatie" field
        eigendom = _extract_field_until_next(kadastrale_body, "Eigendomssituatie", [
            "Lasten", "Oppervlakte", "Kadastraal",
        ])
        if eigendom:
            if "erfpacht" in eigendom.lower():
                ownership_type = "erfpacht"
            elif "volle eigendom" in eigendom.lower():
                ownership_type = "full"

        # Extract canon from "Lasten" field within kadastrale subsection
        lasten = _extract_field_until_next(kadastrale_body, "Lasten", [
            "AMSTERDAM", "MILL", "WIJCHEN", "AALSMEER",  # Next parcel name
        ])
        if lasten:
            # Pattern: "€ 408,85 per jaar"
            m = re.search(
                r"€\s*([\d]+)\s*,\s*(\d+)\s*(?:per\s*)?jaar",
                lasten,
                re.IGNORECASE,
            )
            if m:
                try:
                    erfpacht_canon = float(f"{m.group(1)}.{m.group(2)}")
                except ValueError:
                    pass
            else:
                # Single amount without cents
                m = re.search(
                    r"€\s*([\d.]+)\s*(?:per\s*)?jaar",
                    lasten,
                    re.IGNORECASE,
                )
                if m:
                    raw = m.group(1).replace(".", "").replace(",", ".")
                    try:
                        erfpacht_canon = float(raw)
                    except ValueError:
                        pass

    # Fallback: check whole body if not found in subsection
    if not ownership_type:
        if "erfpacht" in kenmerken_body.lower():
            ownership_type = "erfpacht"
        elif "volle eigendom" in kenmerken_body.lower():
            ownership_type = "full"

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
    if re.search(r'(?<!\w)Tuin\s*(?:[:\-]|\S)', body, re.IGNORECASE):
        garden_present = True

    # garden_type: raw value of Tuin field
    if garden_present:
        garden_type = _extract_field_until_next(body, "Tuin", [
            "Ligging tuin", "Ligging", "Balkon/dakterras", "Bergruimte",
        ])

    # garden_size_m2: the size field label is dynamic — it matches whatever
    # "Tuin"'s value was (e.g. "Tuin: Achtertuin" → size field is "Achtertuin:
    # 74 m² ...").  Also fall back to generic "Tuin" label and "grootte"
    # patterns if the dynamic match fails.
    if garden_type:
        dynamic_pattern = re.compile(
            re.escape(garden_type) + r"\s*[:\-]?\s*(\d+)\s*m\u00b2",
            re.IGNORECASE,
        )
        m = dynamic_pattern.search(body)
        if m:
            try:
                garden_size_m2 = int(m.group(1))
            except ValueError:
                pass

    if garden_size_m2 is None:
        # Fallback: generic patterns for non-dynamic cases
        fallback_patterns = [
            r"Tuin\s*[:\-]?\s*(\d+)\s*m\u00b2",
            r"grootte.*?(\d+)\s*m\u00b2",
        ]
        for pattern in fallback_patterns:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    garden_size_m2 = int(m.group(1))
                except ValueError:
                    pass
                break

    # garden_orientation: "Ligging tuin" — extract just the compass direction
    orientation_raw = _extract_field_until_next(body, "Ligging tuin", [
        "Garage", "Parkeergelegenheid", "Bergruimte", "Buitenruimte",
    ])
    if orientation_raw:
        # Extract compass direction using whole-word matching.
        # Prefer longer compound words first (noordwesten before noord).
        compass_words = [
            "noordwesten", "zuidwesten", "noordoosten", "zuidoosten",
            "noorden", "zuiden", "oosten", "westen",
        ]
        for kw in compass_words:
            if kw in orientation_raw.lower():
                garden_orientation = kw
                break

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

    balcony_text = _extract_field_until_next(body, "Balkon/dakterras", [
        "Bergruimte", "Garage", "Parkeergelegenheid", "Buitenruimte",
    ])
    if balcony_text and "aanwezig" in balcony_text.lower():
        return True
    return None


def _extract_building_bound_outdoor(body: Optional[str]) -> Optional[int]:
    """Extract building_bound_outdoor_m2 from body.

    This field is "Gebouwgebonden buitenruimte" — do NOT confuse with
    "Overige inpandige ruimte" or "Externe bergruimte".
    """
    if not body:
        return None

    m = re.search(
        r"Gebouwgebonden buitenruimte\s*[:\-]?\s*(\d+)\s*m\u00b2",
        body,
        re.IGNORECASE,
    )
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

    garage_text = _extract_field_until_next(body, "Soort garage", [
        "Capaciteit", "Voorzieningen", "Buitenruimte", "Parkeergelegenheid",
    ])
    if not garage_text:
        return None

    garage_lower = garage_text.lower()
    if "niet aanwezig" in garage_lower:
        return None  # Spec says: "Niet aanwezig, wel mogelijk" → null
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

    parking_text = _extract_field_no_sep(body, "Soort parkeergelegenheid")
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

    # The Isolatie section body may contain the value directly (without
    # a field name prefix) or it may be in the Energie subsection with
    # concatenated format. Handle both cases.

    raw = None

    # Strategy 1: If body starts with a value (no "Isolatie" prefix),
    # take the first segment (before " | " or newline)
    if not re.search(r'(?i)^isolatie', body):
        # Body doesn't start with "Isolatie" — take first segment
        m = re.match(r'([^\n|]+)', body.strip())
        if m:
            raw = m.group(1).strip()

    # Strategy 2: If body has "Isolatie" field, extract using boundary
    if not raw:
        raw = _extract_field_until_next(body, "Isolatie", [
            "Verwarming", "Warm water", "Cv-ketel",
        ])

    # Strategy 3: Fallback regex
    if not raw:
        m = re.search(r"Isolatie\s*[:\-]?\s*(.+)", body, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()

    if not raw:
        return None, None

    # Strip leading comma/space
    raw = re.sub(r'^[,;\s]+', '', raw)

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
    component_score = component_count / 4.0

    # Glass quality tier groups — synonyms within a group map to the same score.
    # "dubbel glas" and "dubbelglas" are spelling variants (same quality).
    # "hr-glas" and "hr glas" are also spelling variants.
    # "hr+" and "hr++" are distinct quality tiers, not variants.
    glass_tier_groups = [
        ["enkel glas"],
        ["dubbel glas", "dubbelglas"],
        ["hr-glas", "hr glas"],
        ["hr+"],
        ["hr++"],
    ]
    glass_score = 0.0
    for group_idx, group in enumerate(glass_tier_groups):
        for tier in group:
            if tier in raw_lower:
                # "hr++" contains "hr+" as a substring, so check that if we
                # matched "hr+" but the string actually contains "hr++", skip
                # to the higher group instead.
                if tier == "hr+" and "hr++" in raw_lower:
                    continue
                glass_score = group_idx / (len(glass_tier_groups) - 1)
                break
        else:
            continue
        break

    # Composite: 60% components, 40% glass
    return round(component_score * 0.6 + glass_score * 0.4, 4)


def _extract_heating(body: Optional[str]) -> Optional[str]:
    """Extract heating_type from body."""
    if not body:
        return None

    # "Verwarming" value extends until "Cv-ketel" or end of section
    heating_text = _extract_field_until_next(body, "Verwarming", [
        "Cv-ketel", "Warm water",
    ])
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


def _extract_boiler_year_from_energie(energie_body: Optional[str]) -> Optional[int]:
    """Extract boiler_year from Energie section by finding the Cv-ketel field.

    The Energie section has multiple "Cv-ketel" occurrences:
    - Verwarming -> Cv-ketel (heating type)
    - Warm water -> Cv-ketel (heating type)
    - Cv-ketel -> Nefit (gas gestookt combiketel uit 2011, eigendom) (boiler desc)

    We need the LAST occurrence which has the boiler description.
    """
    if not energie_body:
        return None

    # Find all "Cv-ketel" positions and take the last one
    positions = [m.start() for m in re.finditer(r'(?i)Cv-ketel', energie_body)]
    if not positions:
        return None

    # Take the last occurrence
    last_pos = positions[-1]
    cv_text = energie_body[last_pos + len("Cv-ketel"):]

    # Search for "uit YYYY" in the boiler description
    m = re.search(r"uit\s+(\d{4})", cv_text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extract_energy_label(body: Optional[str]) -> Optional[str]:
    """Extract energy_label from body (Energielabel field).

    The Energie section on Funda uses "FieldnameValue" format with no
    separator (e.g. "EnergielabelCIsolatieDakisolatie...").  We must
    extract the value immediately after "Energielabel" — which is a
    single letter like A, B, C, D, E, F, or G (possibly followed by
    "+" like A+, A++, A+++).
    """
    if not body:
        return None

    # Primary: "FieldnameValue" format (no separator) — match Energielabel
    # followed by the grade letter (+ signs) immediately.
    m = re.search(r"Energielabel\s*([A-G][+]*)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Fallback: try with colon/hyphen separator (in case format varies)
    label = _extract_field_value(body, "Energielabel")
    if label:
        return label

    # Fallback: try "Energielabel" on its own line, value on next line
    m = re.search(r"Energielabel\s*\n\s*([A-G][+]*)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


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


def _extract_stories_and_attic(body: Optional[str]) -> tuple[Optional[int], bool]:
    """Extract stories count and has_attic from the "Aantal woonlagen" field.

    The field lives in the "Indeling" subsection (sibling of "Aantal
    kamers" and "Aantal badkamers"). Returns (stories, has_attic):

    * stories — the LEADING number of the field value ("3 woonlagen en
      een zolder" -> 3, "1 woonlaag" -> 1, "2 woonlagen" -> 2). The attic
      ("zolder") is never added to this count.
    * has_attic — True when "zolder" appears (case-insensitive) anywhere
      in the same field value, else False.

    When the field is absent, returns (None, False). When the field is
    present but has no leading integer, stories is None (logged) and
    has_attic is still resolved from the value — never None.
    """
    if not body:
        return None, False

    raw = _extract_field_until_next(body, "Aantal woonlagen", [
        "Voorzieningen", "Aantal kamers", "Aantal badkamers",
        "Overdracht", "Bouw", "Energie", "Oppervlakten", "Kadastrale",
        "Buitenruimte", "Garage", "Parkeergelegenheid", "Bergruimte",
    ])
    if not raw:
        return None, False

    has_attic = "zolder" in raw.lower()

    m = re.search(r"(\d+)", raw)
    if m:
        try:
            return int(m.group(1)), has_attic
        except ValueError:
            pass

    logger.warning(
        "Aantal woonlagen field found but no leading number parsed: %r", raw,
    )
    return None, has_attic


def _extract_neighborhood_avg_price(body: Optional[str]) -> Optional[float]:
    """Extract neighborhood_avg_price_m2 from Buurt section."""
    if not body:
        return None

    # "Gem. vraagprijs / m²" followed by a number
    m = re.search(
        r"Gem\.?\s*vraagprijs.*?[/\s]m\u00b2.*?€?\s*([\d.]+)",
        body,
        re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass

    # Fallback: just look for price per m² pattern
    m = re.search(
        r"€?\s*([\d.]+)\s*/\s*m\u00b2", body, re.IGNORECASE
    )
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


def _extract_property_type(kenmerken_body: Optional[str]) -> Optional[str]:
    """Extract property_type from the Bouw subsection by field-LABEL presence.

    Returns "House" when the "Soort woonhuis" label is present, else
    "Appartement" when "Soort appartement" is present, else None. Only the
    label presence matters — the field value is not used, and no other
    source (title, URL, etc.) is consulted. Both labels live in the Bouw
    subsection (the same subsection `_extract_year_built` reads from).
    """
    if not kenmerken_body:
        return None

    if re.search(r"Soort woonhuis", kenmerken_body, re.IGNORECASE):
        return "House"
    if re.search(r"Soort appartement", kenmerken_body, re.IGNORECASE):
        return "Appartement"

    return None


def _extract_year_built(kenmerken_body: Optional[str]) -> Optional[int]:
    """Extract year_built from Kenmerken body (Bouw -> Bouwjaar)."""
    if not kenmerken_body:
        return None

    m = re.search(
        r"(?<!\w)Bouwjaar(\d{4})",
        kenmerken_body,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # Fallback: no word boundary (Bouwjaar may be preceded by word char)
    m = re.search(
        r"Bouwjaar(\d{4})",
        kenmerken_body,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


def _extract_status(kenmerken_body: Optional[str]) -> Optional[str]:
    """Extract status from Kenmerken body (Overdracht -> Status)."""
    if not kenmerken_body:
        return None

    # Use _extract_field_until_next for boundary-aware extraction
    status = _extract_field_until_next(kenmerken_body, "Status", [
        "Bouw", "Oppervlakten", "Indeling", "Energie", "Kadastrale",
        "Buitenruimte", "Garage", "Parkeergelegenheid", "Bergruimte",
    ])
    if status:
        return status

    # Fallback: simple pattern without word boundary
    m = re.search(
        r"Status(\S.+?)(?=\s*(?:Bouw|Oppervlakten|Indeling|Energie|Kadastrale|Buitenruimte|Garage|Parkeergelegenheid|Bergruimte)\b|$)",
        kenmerken_body,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    return None


def _extract_plot_size(kenmerken_body: Optional[str]) -> Optional[int]:
    """Extract plot_size_m2 from Kenmerken body (Oppervlakten en inhoud → Perceel)."""
    if not kenmerken_body:
        return None

    m = re.search(
        r"(?<!\w)Perceel\s*[:\-]?\s*(\d+)\s*m\u00b2",
        kenmerken_body,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # No separator format
    m = re.search(
        r"(?<!\w)Perceel(\d+)\s*m\u00b2",
        kenmerken_body,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Address header (street + postal/city + neighborhood) extraction
# ---------------------------------------------------------------------------

_ADDRESS_H1_RE = re.compile(r"<h1\b[^>]*>.*?</h1>", re.DOTALL)

# Dutch postal-code opening: "1234 AB" (the two letters may be followed
# immediately by the city, as Funda renders it in the header span).
_POSTAL_CITY_RE = re.compile(r"^\d{4}\s*[A-Za-z]{2}\b")


def _extract_neighborhood(html: str) -> Optional[str]:
    """Extract the normalized neighborhood string from the detail page HTML.

    The rendered detail page's address header (``<h1>``) contains, in order:
    the street address, the postal code + city, and a linked neighborhood
    name. Funda renders these as sibling elements with no separator in the
    raw HTML, e.g.::

        <h1>
          <span>Incastraat 19</span>
          <span>1448 XS Purmerend</span>
          <a aria-label="Amerika" href=".../informatie/purmerend/amerika">Amerika</a>
        </h1>

    Returns ``"{neighborhood_name} - {postal_code} {city}"`` (e.g.
    ``"Amerika - 1448 XS Purmerend"``), or None when the header cannot be
    parsed. Values are taken verbatim from the page — never invented, never
    guessed.
    """
    if not html:
        return None

    m = _ADDRESS_H1_RE.search(html)
    if not m:
        return None
    header = m.group(0)

    # Neighborhood name — the linked name in the header. Prefer aria-label
    # (the accessible name), falling back to the visible link text.
    nb_m = re.search(r'<a\b[^>]*aria-label="([^"]+)"', header)
    if nb_m:
        neighborhood_name = nb_m.group(1).strip()
    else:
        a_m = re.search(r"<a\b[^>]*>(.*?)</a>", header, re.DOTALL)
        neighborhood_name = re.sub(r"<[^>]+>", "", a_m.group(1)).strip() if a_m else None

    # Postal code + city — the header span whose text opens with a Dutch
    # postal code ("1234 AB"). Matched by content rather than position so
    # the extraction survives reordering of the header children.
    postal_city = None
    for span_text in re.findall(r"<span\b[^>]*>([^<]+)</span>", header):
        candidate = span_text.strip()
        if _POSTAL_CITY_RE.match(candidate):
            postal_city = candidate
            break

    if neighborhood_name and postal_city:
        return f"{neighborhood_name} - {postal_city}"
    return None


# ---------------------------------------------------------------------------
# Property image extraction
# ---------------------------------------------------------------------------

_COLLECT_IMAGE_URLS_JS = """
() => {
    const out = [];
    const seen = new Set();
    const push = u => {
        if (!u || seen.has(u)) return;
        seen.add(u);
        out.push(u);
    };
    document.querySelectorAll('meta[property="og:image"]').forEach(m => {
        push(m.content);
    });
    document.querySelectorAll('img').forEach(img => {
        push(img.currentSrc || img.src);
        if (img.srcset) {
            const cands = img.srcset.split(',')
                .map(s => s.trim().split(/\\s+/)[0])
                .filter(Boolean);
            if (cands.length) push(cands[cands.length - 1]);
        }
    });
    return out;
}
"""


def _canonical_image_url(url: str) -> Optional[str]:
    """Normalise one raw CDN URL to the canonical full-size photo URL.

    * strips any query string (``?options=width=720`` etc.);
    * strips embedded size suffixes (``775_1440x960.jpg`` -> ``775.jpg``);
    * re-requests the photo at the canonical width
      (``?options=width=<n>``), which the CDN serves for media paths.

    Returns None when the URL is not a Funda property-photo URL.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.netloc.lower() != _PROPERTY_IMAGE_HOST:
        return None
    path = parts.path
    if _PROPERTY_IMAGE_PATH_MARKER not in path:
        return None
    path = _SIZE_SUFFIX_RE.sub("", path)
    if not _IMAGE_EXT_RE.search(path):
        return None
    return (
        f"https://{_PROPERTY_IMAGE_HOST}{path}"
        f"?options=width={_PROPERTY_IMAGE_WIDTH}"
    )


def _extract_property_image_urls(raw_urls: list) -> list[str]:
    """Filter, normalise, dedupe and cap raw page image URLs.

    Only https URLs on Funda's property-photo CDN are kept. The same photo
    referenced through several size variants collapses to its canonical
    form; first-seen gallery order is preserved. Output is capped at
    ``_MAX_IMAGE_URLS``.
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_urls or []:
        if not isinstance(raw, str):
            continue
        canonical = _canonical_image_url(raw.strip())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
        if len(result) >= _MAX_IMAGE_URLS:
            break
    return result


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

        # --- Address header (neighborhood + postal code + city) ---
        # Extracted from the raw HTML before rendering — the header is
        # server-side rendered and needs no JavaScript. Enriches the
        # card-level neighborhood (city slug) with the full
        # "{neighborhood} - {postal} {city}" string.
        neighborhood = _extract_neighborhood(html)
        if neighborhood:
            result.neighborhood = neighborhood

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

                # --- Description (Beschrijving / Omschrijving section) ---
                # Raw Dutch text; used by the notifier for the Pros/Cons
                # and Bottom line sections. Never interpreted here.
                description_body = _find_section_body(sections, "Beschrijving")
                if not description_body:
                    description_body = _find_section_body(sections, "Omschrijving")
                if not description_body:
                    description_body = _find_text_block(text, "Beschrijving")
                if description_body:
                    result.description = description_body.strip()[:4000]

                # Kenmerken section — most fields
                kenmerken_body = _find_section_body(sections, "Kenmerken")
                if not kenmerken_body:
                    kenmerken_body = _find_text_block(text, "Kenmerken")

                # Parse subsections within Kenmerken
                kenmerken_subsections = _split_kenmerken_subsections(
                    kenmerken_body or ""
                )

                # --- Ownership (Kadastrale gegevens subsection) ---
                ownership_type, erfpacht_canon = _extract_ownership(
                    kenmerken_body, kenmerken_subsections
                )
                result.ownership_type = ownership_type
                result.erfpacht_canon_annual = erfpacht_canon

                # --- Rooms (Indeling subsection) ---
                rooms = _extract_rooms(kenmerken_body)
                if rooms:
                    result.rooms = rooms

                # --- Property type (Bouw subsection) ---
                property_type = _extract_property_type(kenmerken_body)
                if property_type:
                    result.property_type = property_type

                # --- Year built (Bouw subsection) ---
                year_built = _extract_year_built(kenmerken_body)
                if year_built:
                    result.year_built = year_built

                # --- Status (Overdracht subsection) ---
                status = _extract_status(kenmerken_body)
                if status:
                    result.status = status

                # --- Plot size (Oppervlakten en inhoud subsection) ---
                plot_size = _extract_plot_size(kenmerken_body)
                if plot_size:
                    result.plot_size_m2 = plot_size

                # --- Buitenruimte section ---
                buitenruimte_body = _find_section_body(sections, "Buitenruimte")
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

                # Building bound outdoor — in "Oppervlakten en inhoud" subsection
                # Do NOT read from Buitenruimte (that's a different section)
                opp_body = None
                for heading, body in kenmerken_subsections:
                    if "oppervlakten" in heading.lower():
                        opp_body = body
                        break
                if not opp_body:
                    opp_body = _find_text_block(text, "Oppervlakten en inhoud")
                if opp_body:
                    building_outdoor = _extract_building_bound_outdoor(opp_body)
                    if building_outdoor is not None:
                        result.building_bound_outdoor_m2 = building_outdoor

                # --- Garage and parking ---
                # Garage type: check "Garage" subsection within Kenmerken
                garage_body = None
                for heading, body in kenmerken_subsections:
                    if "garage" in heading.lower():
                        garage_body = body
                        break
                if not garage_body:
                    garage_body = _find_text_block(text, "Garage")

                if garage_body:
                    garage_type = _extract_garage_type(garage_body)
                    if garage_type:
                        result.garage_type = garage_type

                # Parking type: check "Parkeergelegenheid" subsection
                parking_body = None
                for heading, body in kenmerken_subsections:
                    if "parkeergelegenheid" in heading.lower():
                        parking_body = body
                        break
                if not parking_body:
                    parking_body = _find_text_block(text, "Parkeergelegenheid")
                if parking_body:
                    parking_type = _extract_parking_type(parking_body)
                    if parking_type:
                        result.parking_type = parking_type

                # --- Isolatie section ---
                isolatie_body = _find_section_body(sections, "Isolatie")
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

                # --- Energielabel (from "Energie" Kenmerken subsection) ---
                # Must read from the "Energie" subsection (siblings: Isolatie,
                # Verwarming, Warm water, Cv-ketel), NOT from the compact
                # icon-stat row near the top of the page (which has no labels).
                energie_body = _find_section_body(sections, "Energie")
                if not energie_body:
                    energie_body = _find_text_block(text, "Energie")
                energy_label = _extract_energy_label(energie_body)
                if energy_label:
                    result.energy_label = energy_label

                # --- Cv-ketel (boiler year) ---
                # Must scope to the Cv-ketel field within the Energie section
                # to avoid matching year_built or other years in the text.
                # Use _extract_boiler_year_from_energie which finds the LAST
                # "Cv-ketel" occurrence (the one with the boiler description).
                if energie_body:
                    boiler_year = _extract_boiler_year_from_energie(energie_body)
                    if boiler_year:
                        result.boiler_year = boiler_year

                # --- Aantal badkamers ---
                bathrooms = _extract_bathrooms(kenmerken_body)
                if bathrooms:
                    result.bathrooms = bathrooms

                # --- Aantal woonlagen (stories) & attic (Indeling subsection) ---
                stories, has_attic = _extract_stories_and_attic(kenmerken_body)
                if stories is not None:
                    result.stories = stories
                result.has_attic = has_attic

                # --- Buurt section ---
                buurt_body = _find_section_body(sections, "Buurt")
                if not buurt_body:
                    buurt_body = _find_text_block(text, "Buurt")
                neighborhood_avg = _extract_neighborhood_avg_price(buurt_body)
                if neighborhood_avg is not None:
                    result.neighborhood_avg_price_m2 = neighborhood_avg

                # --- Property photos (same rendered page, gallery order) ---
                try:
                    raw_image_urls = page.evaluate(_COLLECT_IMAGE_URLS_JS)
                    image_urls = _extract_property_image_urls(raw_image_urls)
                    if image_urls:
                        result.image_urls = image_urls
                        logger.info(
                            "Extracted %d property photo URL(s) from %s",
                            len(image_urls), url,
                        )
                    else:
                        logger.info("No property photo URLs found on %s", url)
                except Exception as exc:
                    # Photo extraction must never fail the detail fetch.
                    logger.warning(
                        "Property photo extraction failed for %s: %s", url, exc,
                    )

            finally:
                browser.close()

    except Exception as exc:
        logger.warning(
            "Failed to fetch detail page %s: %s (returning partial data)",
            url,
            exc,
        )
        # Return whatever we have — all fields will be None if we failed early
        result.detail_fetched_at = datetime.now(timezone.utc).isoformat()

    return result.to_dict()