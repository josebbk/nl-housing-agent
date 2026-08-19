"""
Unit tests for src/scraper.build_search_url (pure URL construction).

No network access and no browser is launched. playwright.sync_api is stubbed
in sys.modules so the real src.scraper module imports without the browser.
"""

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

from src.scraper import build_search_url  # noqa: E402


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