"""Tests for the one-off Diemen topic flow (src/diemen_topic.py).

Covers only the deterministic, network-free parts:
  * area_is_diemen / _matches_filters verification against Diemen.json;
  * select_batch sampling (small, ordered batch);
  * _scrape_kwargs mapping from a FilterConfig.

Telegram/network behaviour is not exercised here — see test_notifier_topic.py.
"""

import unittest

from src.config import FilterConfig
from src.diemen_topic import (
    TOPIC_NAME,
    _matches_filters,
    area_is_diemen,
    select_batch,
    _scrape_kwargs,
)


def _diemen_filters() -> FilterConfig:
    return FilterConfig(
        price_min=500000,
        price_max=700000,
        bedrooms_min=3,
        living_area_min=100,
        object_type=["house"],
        selected_area=(
            "diemen/wijk-diemen-noord,diemen/wijk-diemen-centrum,"
            "diemen/wijk-diemen-zuid,duivendrecht"
        ),
        availability="available",
        sort="publish_date_utc_desc",
    )


def _house(listing_id="1", neighborhood="diemen", price=600000,
           bedrooms=3, living_area=120):
    return {
        "listing_id": listing_id,
        "url": f"https://www.funda.nl/detail/koop/{neighborhood}/huis-x/{listing_id}/",
        "address": f"Huisstraat {listing_id}",
        "neighborhood": neighborhood,
        "price": price,
        "living_area_m2": living_area,
        "bedrooms": bedrooms,
        "property_type": "huis",
    }


class AreaIsDiemenTest(unittest.TestCase):
    def test_municipality_slugs_accepted(self):
        self.assertTrue(area_is_diemen("diemen"))
        self.assertTrue(area_is_diemen("Diemen"))
        self.assertTrue(area_is_diemen("duivendrecht"))

    def test_other_areas_rejected(self):
        self.assertFalse(area_is_diemen("amsterdam"))
        self.assertFalse(area_is_diemen(None))
        self.assertFalse(area_is_diemen(""))
        self.assertFalse(area_is_diemen("weesp"))


class MatchesFiltersTest(unittest.TestCase):
    def setUp(self):
        self.filters = _diemen_filters()

    def test_matching_house(self):
        self.assertTrue(_matches_filters(_house(), self.filters))

    def test_rejects_wrong_area(self):
        self.assertFalse(_matches_filters(
            _house(listing_id="2", neighborhood="amsterdam"), self.filters))

    def test_rejects_price_too_low(self):
        self.assertFalse(_matches_filters(_house(price=450000), self.filters))

    def test_rejects_price_too_high(self):
        self.assertFalse(_matches_filters(_house(price=720000), self.filters))

    def test_rejects_too_few_bedrooms(self):
        self.assertFalse(_matches_filters(_house(bedrooms=2), self.filters))

    def test_rejects_too_small_living_area(self):
        self.assertFalse(_matches_filters(_house(living_area=80), self.filters))

    def test_null_value_never_matches(self):
        self.assertFalse(_matches_filters(
            {"neighborhood": "diemen", "price": None,
             "bedrooms": None, "living_area_m2": None}, self.filters))

    def test_duivendrecht_house_matches(self):
        self.assertTrue(_matches_filters(
            _house(listing_id="3", neighborhood="duivendrecht"), self.filters))


class SelectBatchTest(unittest.TestCase):
    def setUp(self):
        self.filters = _diemen_filters()

    def test_returns_small_ordered_batch(self):
        listings = [_house(listing_id=str(i), neighborhood="diemen") for i in range(1, 10)]
        batch = select_batch(listings, self.filters, limit=4)
        self.assertEqual([l["listing_id"] for l in batch], ["1", "2", "3", "4"])

    def test_skips_non_matching_and_then_selects(self):
        listings = [
            _house(listing_id="1", neighborhood="amsterdam"),
            _house(listing_id="2", neighborhood="diemen"),
            _house(listing_id="3", price=400000),
            _house(listing_id="4", neighborhood="diemen"),
        ]
        batch = select_batch(listings, self.filters, limit=2)
        self.assertEqual([l["listing_id"] for l in batch], ["2", "4"])

    def test_empty_when_none_match(self):
        self.assertEqual(select_batch(
            [_house(listing_id="1", neighborhood="amsterdam")],
            self.filters), [])

    def test_empty_input(self):
        self.assertEqual(select_batch([], self.filters), [])


class ScrapeKwargsTest(unittest.TestCase):
    def test_maps_diemen_filters_into_scrape_kwargs(self):
        kwargs = _scrape_kwargs(_diemen_filters())
        self.assertEqual(kwargs["area"],
                         "diemen/wijk-diemen-noord,diemen/wijk-diemen-centrum,"
                         "diemen/wijk-diemen-zuid,duivendrecht")
        self.assertEqual(kwargs["price_min"], 500000)
        self.assertEqual(kwargs["price_max"], 700000)
        self.assertEqual(kwargs["bedrooms_min"], 3)
        self.assertEqual(kwargs["floor_area_min"], 100)
        self.assertEqual(kwargs["object_type"], ["house"])
        self.assertEqual(kwargs["availability"], "available")
        self.assertEqual(kwargs["sort"], "publish_date_utc_desc")
        self.assertEqual(kwargs["max_pages"], 5)


class TopicNameTest(unittest.TestCase):
    def test_topic_name_is_set(self):
        self.assertEqual(TOPIC_NAME, "Diemen — Funda Matches")
        self.assertTrue(TOPIC_NAME.strip())


if __name__ == "__main__":
    unittest.main()
