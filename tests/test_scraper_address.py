"""Tests for _extract_listing_data address extraction logic.

Verifies that the address-extraction loop in scraper.py correctly
distinguishes real Dutch property addresses from promotional taglines,
badge words, and other non-address card text.
"""

import pytest
from src.scraper import _extract_listing_data


# ---------------------------------------------------------------------------
# Fixtures — simulated card-level text for each known failure case
# ---------------------------------------------------------------------------

# Case 3 (HOOFDDORP — Noordvaarder 23): the confirmed failing case where
# the promotional tagline was accepted as the address.
CARD_PROMOTED_WITH_TAGLINE = """Uitgebouwde eindwoning met zonnige tuin op het zuidwesten
Noordvaarder 23
€ 485.000 k.k.
145 m²
4
A"""

# Case 1 (PURMEREND — Incastraat 19): promoted card with "Blikvanger" badge
# and "Open huis" badge concatenated on first line.
CARD_PROMOTED_WITH_BADGES = """BlikvangerNieuwOpen huis
Incastraat 19
€ 650.000 k.k.
105 m²
3
A"""

# Case 2 (AMSTELVEEN — Guido van Dethlaan 32): multi-word street name.
CARD_MULTI_WORD_STREET = """Guido van Dethlaan 32
€ 595.000 k.k.
130 m²
4
B"""

# Edge cases — lines that should NOT be accepted as addresses.
CARD_POSTCODE_CITY = """1068 HV Amsterdam
Kerkstraat 10
€ 450.000 k.k.
85 m²
2"""

CARD_ENERGY_LABEL_LINE = """Noordhollandedijk 12
€ 720.000 k.k.
126 m²
4
A"""

CARD_PROMO_WITH_COLON = """Ruim wonen: direct genieten van comfort en stijl!
Weg 25
€ 390.000 k.k.
90 m²
3"""

# Promo line with commas — already filtered by existing logic, but we
# include it for regression coverage.
CARD_PROMO_WITH_COMMAS = """Ruim wonen aan een kindvriendelijk woonerf, met een zonnige tuin.
Houtkat 2
€ 510.000 k.k.
110 m²
4"""


# ---------------------------------------------------------------------------
# Parametrized test — the three known failing cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "card_text,href,expected_address",
    [
        pytest.param(
            CARD_PROMOTED_WITH_TAGLINE,
            "/detail/koop/hoofddorp/huis-noordvaarder-23/44572336/",
            "Noordvaarder 23",
            id="promoted_tagline_noordvaarder",
        ),
        pytest.param(
            CARD_PROMOTED_WITH_BADGES,
            "/detail/koop/purmerend/huis-incastraat-19/44573601/",
            "Incastraat 19",
            id="promoted_badges_incastraat",
        ),
        pytest.param(
            CARD_MULTI_WORD_STREET,
            "/detail/koop/amstelveen/huis-guido-van-dethlaan-32/44481126/",
            "Guido van Dethlaan 32",
            id="multi_word_street_guido_van_dethlaan",
        ),
    ],
)
def test_extract_listing_data_address(card_text, href, expected_address):
    """Verify that _extract_listing_data extracts the correct address."""
    result = _extract_listing_data(card_text, href)
    assert result is not None, f"Parsed result is None for href={href}"
    assert (
        result["address"] == expected_address
    ), f"Expected {expected_address!r}, got {result['address']!r}"


# ---------------------------------------------------------------------------
# Regression tests — lines that must NOT be accepted as addresses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "card_text,expected_address",
    [
        pytest.param(
            CARD_POSTCODE_CITY,
            "Kerkstraat 10",
            id="postcode_city_not_address",
        ),
        pytest.param(
            CARD_ENERGY_LABEL_LINE,
            "Noordhollandedijk 12",
            id="energy_label_line_not_address",
        ),
        pytest.param(
            CARD_PROMO_WITH_COLON,
            "Weg 25",
            id="promo_with_colon_not_address",
        ),
        pytest.param(
            CARD_PROMO_WITH_COMMAS,
            "Houtkat 2",
            id="promo_with_commas_not_address",
        ),
    ],
)
def test_non_address_lines_rejected(card_text, expected_address):
    """Verify that known non-address lines are not extracted as the address."""
    result = _extract_listing_data(card_text, "/detail/koop/amsterdam/huis-foo/12345/")
    assert result is not None
    assert (
        result["address"] == expected_address
    ), f"Expected {expected_address!r}, got {result['address']!r}"


# ---------------------------------------------------------------------------
# House-number regex unit tests
# ---------------------------------------------------------------------------

import re
from src.scraper import _HOUSE_NUM_END_RE


@pytest.mark.parametrize(
    "line,should_match",
    [
        pytest.param("Noordvaarder 23", True, id="standard"),
        pytest.param("Incastraat 19", True, id="standard_short"),
        pytest.param("Guido van Dethlaan 32", True, id="multi_word_street"),
        pytest.param("Weg 12A", True, id="letter_suffix"),
        pytest.param("Straat 12-1", True, id="hyphen_number"),
        pytest.param("Molenweg 5a", True, id="lowercase_suffix"),
        pytest.param("Wijk 1AB", True, id="double_letter_suffix"),
        pytest.param(
            "Uitgebouwde eindwoning met zonnige tuin op het zuidwesten",
            False,
            id="promotional_tagline",
        ),
        pytest.param("BlikvangerNieuw", False, id="concatenated_badge"),
        pytest.param("Open huis", False, id="badge_with_space"),
        pytest.param("1068 HV Amsterdam", False, id="postcode_city"),
        pytest.param("\u20ac 650.000 k.k.", False, id="price_line"),
        pytest.param("126 m\u00b2", False, id="area_line"),
        pytest.param("Turn-key woning: direct genieten", False, id="promo_with_colon"),
        pytest.param(
            "Ruim wonen aan een kindvriendelijk woonerf, met een zonnige tuin.",
            False,
            id="promo_with_commas",
        ),
        pytest.param("3", False, id="standalone_number_bedrooms"),
        pytest.param("A", False, id="energy_label"),
        pytest.param("Dwarswatering 10", True, id="simple_street_number"),
        pytest.param("Van-Houtenstraat 15", True, id="hyphen_street_name"),
        pytest.param("P+R Parking 10", True, id="parking_location"),
    ],
)
def test_house_number_regex(line, should_match):
    """Verify the house-number-end-of-line regex matches correctly."""
    result = bool(_HOUSE_NUM_END_RE.search(line))
    assert result == should_match, (
        f"Expected match={should_match} for {line!r}, got {result}"
    )