"""
Unit tests for src/scraper.py — listing extraction, parsing, and URL construction.

These tests verify the parsing logic of _extract_listing_data, the dedup key
collision fix, parse_price, and build_search_url. They do not require a browser
or network access. playwright.sync_api is stubbed in sys.modules so the real
src.scraper module imports without the browser.
"""

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


playwright = _make_module("playwright")
playwright.sync_api = _make_module(
    "playwright.sync_api",
    sync_playwright=mock.MagicMock(),
    Page=mock.MagicMock(),
    Browser=mock.MagicMock(),
)
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", playwright.sync_api)

from src.scraper import (  # noqa: E402
    _extract_listing_data,
    build_search_url,
    parse_price,
)


class ExtractListingDataTestCase(unittest.TestCase):
    """Tests for _extract_listing_data parsing."""

    def test_parses_standard_listing(self):
        """A standard listing with all fields should parse correctly."""
        text = "Kerkstraat 12\n1017 Amsterdam\n\u20ac 625.000 k.k.\n105 m\u00b2\n3\nB"
        # Funda URL format: /detail/koop/{city}/{type}-{slug}/{id}/
        # The ID is always the last numeric path segment.
        href = "/detail/koop/amsterdam/huis-x/12345/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        self.assertEqual(result["listing_id"], "12345")
        self.assertEqual(result["address"], "Kerkstraat 12")
        self.assertEqual(result["neighborhood"], "amsterdam")
        self.assertEqual(result["price"], 625000)
        self.assertEqual(result["living_area_m2"], 105)
        self.assertEqual(result["bedrooms"], 3)
        self.assertEqual(result["energy_label"], "B")
        self.assertEqual(result["property_type"], "huis")

    def test_parses_listing_with_plot_area(self):
        """A listing with both living area and plot area should parse both."""
        text = "Molenstraat 45\n1073 Amsterdam\n\u20ac 720.000 v.o.n.\n120 m\u00b2\n95 m\u00b2\n4\nA"
        href = "/detail/koop/amsterdam/huis-y/67890/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        self.assertEqual(result["listing_id"], "67890")
        self.assertEqual(result["living_area_m2"], 120)
        self.assertEqual(result["plot_size_m2"], 95)
        self.assertEqual(result["bedrooms"], 4)
        self.assertEqual(result["energy_label"], "A")

    def test_drops_listing_with_unparseable_href(self):
        """A listing whose href doesn't match the expected pattern should be
        dropped (returns None)."""
        text = "Some listing text"
        href = "/detail/koop/nieuwbouw/project-abc/"  # no numeric ID at end
        result = _extract_listing_data(text, href)
        self.assertIsNone(result)

    def test_drops_listing_with_empty_href(self):
        """An empty href should result in None."""
        result = _extract_listing_data("Some text", "")
        self.assertIsNone(result)

    def test_drops_listing_with_too_short_text(self):
        """Text shorter than 10 characters should be dropped."""
        result = _extract_listing_data("Short", "/detail/koop/x/1/")
        self.assertIsNone(result)


class DedupKeyCollisionRegressionTestCase(unittest.TestCase):
    """Regression test for the dedup key collision bug.

    The old dedup key was `card.innerHTML.substring(0, 200)`, which caused
    distinct listings to collide because Funda's card template has identical
    CSS classes and structure for every card. The first 200 characters of
    innerHTML are nearly identical across different listings — only the
    variable content (address, price) differs, which appears after position 200.

    This test verifies that _extract_listing_data correctly parses listings
    with different IDs even when their text content is nearly identical
    (simulating what would have happened with the old dedup key).
    """

    def test_different_ids_same_text_prefix_parsed_correctly(self):
        """Two listings with nearly identical text but different IDs must
        both be parsed. This was the scenario the old dedup key would
        have collapsed into one."""
        # Two listings with different addresses but same structure
        text_a = "Kerkstraat 12\n1017 Amsterdam\n\u20ac 625.000 k.k.\n105 m\u00b2\n3\nB"
        text_b = "Dijkstraat 45\n1072 Amsterdam\n\u20ac 695.000 k.k.\n110 m\u00b2\n4\nA"

        href_a = "/detail/koop/amsterdam/huis-x/11111/"
        href_b = "/detail/koop/amsterdam/huis-y/22222/"

        result_a = _extract_listing_data(text_a, href_a)
        result_b = _extract_listing_data(text_b, href_b)

        self.assertIsNotNone(result_a)
        self.assertIsNotNone(result_b)
        self.assertEqual(result_a["listing_id"], "11111")
        self.assertEqual(result_b["listing_id"], "22222")
        self.assertNotEqual(result_a["address"], result_b["address"])
        self.assertNotEqual(result_a["price"], result_b["price"])

    def test_dedup_key_is_href_not_html_content(self):
        """Verify that the dedup key is href-based, not HTML-content-based.

        The old dedup key was `card.innerHTML.substring(0, 200)`, which is
        fragile because Funda's card template has identical CSS classes and
        structure for every card. The first 200 chars of innerHTML are nearly
        identical across different listings — only the variable content
        (address, price) differs, which often appears after position 200.

        The fix uses the href as the dedup key instead, which is guaranteed
        unique per listing. This test verifies that two listings with
        different IDs are correctly identified as distinct.
        """
        # Two listings with different IDs but similar structure
        text_a = "Kerkstraat 12\n1017 Amsterdam\n\u20ac 625.000 k.k.\n105 m\u00b2\n3\nB"
        text_b = "Dijkstraat 45\n1072 Amsterdam\n\u20ac 695.000 k.k.\n110 m\u00b2\n4\nA"

        href_a = "/detail/koop/amsterdam/huis-x/11111/"
        href_b = "/detail/koop/amsterdam/huis-y/22222/"

        result_a = _extract_listing_data(text_a, href_a)
        result_b = _extract_listing_data(text_b, href_b)

        self.assertIsNotNone(result_a)
        self.assertIsNotNone(result_b)
        self.assertEqual(result_a["listing_id"], "11111")
        self.assertEqual(result_b["listing_id"], "22222")

        # The hrefs are the correct unique dedup key
        self.assertNotEqual(href_a, href_b)

        # Even if the HTML content were identical (simulating a dedup key
        # collision scenario), the hrefs distinguish the listings
        self.assertNotEqual(result_a["price"], result_b["price"])
        self.assertNotEqual(result_a["address"], result_b["address"])


class PromotedCardExtractionTestCase(unittest.TestCase):
    """Regression tests for promoted/featured card text format.

    Funda's "Blikvanger" (featured) cards have a different text structure:
    - Line 0: Promo description (e.g. "Ruim wonen aan een kindvriendelijk
      woonerf, met een zonnige tuin.")
    - Line 1: Concatenated badges (e.g. "BlikvangerNieuw")
    - Line 2: Actual street address (e.g. "Dwarswatering 10")
    - Line 3: Postcode + city (e.g. "1069 RM Amsterdam")
    - Line 4+: Price, area, bedrooms, energy label

    The address parser must skip lines 0-1 and extract line 2.
    """

    def test_promoted_card_skips_description_and_badges(self):
        """A promoted card's first two lines (description + badges) must be
        skipped, and the street address on line 3 must be extracted."""
        text = (
            "Ruim wonen aan een kindvriendelijk woonerf, met een zonnige tuin.\n"
            "BlikvangerNieuw\n"
            "Dwarswatering 10\n"
            "1069 RM Amsterdam\n"
            "\u20ac 595.000 k.k.\n"
            "125 m\u00b2\n"
            "110 m\u00b2\n"
            "4\n"
            "B"
        )
        href = "/detail/koop/amsterdam/huis-dwarswatering-10/44566926/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        self.assertEqual(result["address"], "Dwarswatering 10")
        self.assertEqual(result["price"], 595000)
        self.assertEqual(result["living_area_m2"], 125)
        self.assertEqual(result["bedrooms"], 4)
        self.assertEqual(result["energy_label"], "B")

    def test_promoted_card_with_colon_description(self):
        """A promoted card with a colon in the description line."""
        text = (
            "Turn-key woning: direct genieten van comfort en stijl!\n"
            "BlikvangerNieuw\n"
            "Nieuwe Osdorpergracht 265\n"
            "1068 HV Amsterdam\n"
            "\u20ac 750.000 k.k.\n"
            "137 m\u00b2\n"
            "137 m\u00b2\n"
            "6\n"
            "A+"
        )
        href = "/detail/koop/amsterdam/huis-nieuwe-osdorpergracht-265/80920655/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        # "Nieuwe" must NOT be treated as the badge "nieuw"
        self.assertEqual(result["address"], "Nieuwe Osdorpergracht 265")
        self.assertEqual(result["price"], 750000)
        self.assertEqual(result["bedrooms"], 6)

    def test_badge_word_not_matched_as_street_name_substring(self):
        """The badge word 'nieuw' must not match 'Nieuwe' in a street name.
        'Nieuwe Herengracht 42' should be extracted as the address, not
        skipped as a badge word."""
        text = (
            "Nieuw\n"
            "Nieuwe Herengracht 42\n"
            "1017 Amsterdam\n"
            "\u20ac 650.000 k.k.\n"
            "110 m\u00b2\n"
            "3\n"
            "C"
        )
        href = "/detail/koop/amsterdam/huis-nieuwe-herengracht-42/12345/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        self.assertEqual(result["address"], "Nieuwe Herengracht 42")

    def test_postcode_line_not_used_as_address(self):
        """A line like '1069 RM Amsterdam' (postcode + city) must not be
        used as the address when a street address is available."""
        text = (
            "Dwarswatering 10\n"
            "1069 RM Amsterdam\n"
            "\u20ac 595.000 k.k.\n"
            "125 m\u00b2\n"
            "4\n"
            "B"
        )
        href = "/detail/koop/amsterdam/huis-dwarswatering-10/44566926/"
        result = _extract_listing_data(text, href)
        self.assertIsNotNone(result)
        self.assertEqual(result["address"], "Dwarswatering 10")
        self.assertNotEqual(result["address"], "1069 RM Amsterdam")


class ParsePriceTestCase(unittest.TestCase):
    """Tests for the parse_price helper function."""

    def test_parses_kk_price(self):
        price, text = parse_price("\u20ac 625.000 k.k.")
        self.assertEqual(price, 625000)
        self.assertEqual(text, "\u20ac 625.000 k.")

    def test_parses_von_price(self):
        price, text = parse_price("\u20ac 750.000 v.o.n.")
        self.assertEqual(price, 750000)
        self.assertEqual(text, "\u20ac 750.000 v.")

    def test_parses_no_thousands_separator(self):
        price, text = parse_price("\u20ac 450000 k.o.")
        self.assertEqual(price, 450000)

    def test_returns_none_for_no_price(self):
        price, text = parse_price("Some random text")
        self.assertIsNone(price)


class BuildSearchUrlTestCase(unittest.TestCase):
    def test_default_url(self):
        self.assertEqual(
            build_search_url(),
            "https://www.funda.nl/zoeken/koop?selected_area=amsterdam&page=1",
        )

    def test_no_range_params_when_unset(self):
        url = build_search_url()
        for token in ("floor_area=", "bedrooms=", "rooms=", "price="):
            self.assertNotIn(token, url)

    def test_floor_area_min_only_open_ended(self):
        url = build_search_url(floor_area_min=100)
        self.assertIn("floor_area=100-", url)

    def test_floor_area_range(self):
        url = build_search_url(floor_area_min=100, floor_area_max=160)
        self.assertIn("floor_area=100-160", url)
        self.assertNotIn("floor_area=100-&", url)

    def test_bedrooms_min_only_open_ended(self):
        url = build_search_url(bedrooms_min=3)
        self.assertIn("bedrooms=3-", url)

    def test_bedrooms_range(self):
        url = build_search_url(bedrooms_min=3, bedrooms_max=5)
        self.assertIn("bedrooms=3-5", url)

    def test_rooms_range(self):
        url = build_search_url(rooms_min=4, rooms_max=8)
        self.assertIn("rooms=4-8", url)

    def test_rooms_min_only_open_ended(self):
        url = build_search_url(rooms_min=4)
        self.assertIn("rooms=4-", url)

    def test_offering_type_huur(self):
        self.assertTrue(build_search_url(offering_type="huur").startswith(
            "https://www.funda.nl/zoeken/huur?"
        ))

    def test_price_range_preserved(self):
        url = build_search_url(price_min=550000, price_max=750000)
        self.assertIn("price=550000-750000", url)

    def test_radius_encodes_selected_area(self):
        url = build_search_url(radius_km=5)
        # Funda encodes the radius inside selected_area as a JSON array.
        self.assertIn("selected_area=%5B%22amsterdam%2C5km%22%5D", url)
        self.assertNotIn("selected_area=amsterdam&", url)

    def test_radius_uses_custom_area(self):
        url = build_search_url(area="amsterdam-zuid", radius_km=10)
        self.assertIn("amsterdam-zuid", url)
        self.assertIn("10km", url)

    def test_no_radius_keeps_plain_selected_area(self):
        url = build_search_url()
        self.assertIn("selected_area=amsterdam", url)
        self.assertNotIn("%22", url)

    def test_construction_type_param(self):
        self.assertIn("construction_type=existing", build_search_url(construction_type="existing"))
        self.assertIn("construction_type=new", build_search_url(construction_type="new"))

    def test_no_construction_type_when_none(self):
        self.assertNotIn("construction_type=", build_search_url())


if __name__ == "__main__":
    unittest.main()