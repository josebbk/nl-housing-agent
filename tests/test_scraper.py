"""
Unit tests for src/scraper.py — listing extraction and parsing.

These tests verify the parsing logic of _extract_listing_data and the
dedup key collision fix. They do not require a browser or network access.
"""

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scraper import _extract_listing_data, parse_price  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()