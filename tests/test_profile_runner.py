"""Tests for the generic profile runner (src/profile_runner.py) and the two
new profiles (Abcoude, Ouderkerk aan de Amstel).

Covers profile loading, exact filter values, independent DB paths, correct
topic routing, cross-profile isolation, no repeated seeding, and that the
existing Diemen / old live flows are unaffected. Telegram and network are
mocked; no messages are sent.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.config import FilterConfig
from src import profile_runner as pr
from src import diemen_topic as dt


def _house(listing_id, neighborhood, price=600000, bedrooms=4, living_area=120):
    return {
        "listing_id": listing_id,
        "url": f"https://www.funda.nl/detail/koop/{neighborhood}/huis-x/{listing_id}/",
        "address": f"Huisstraat {listing_id}",
        "neighborhood": neighborhood,
        "price": price,
        "living_area_m2": living_area,
        "bedrooms": bedrooms,
        "property_type": "huis",
        "notified": 0,
    }


def _tmp_db():
    return str(Path(tempfile.mkdtemp()) / "t.db")


class ProfileLoadingTest(unittest.TestCase):
    def setUp(self):
        self.profiles = pr.load_profiles()

    def test_both_profiles_present(self):
        self.assertEqual(set(self.profiles), {"abcoude", "ouderkerk-aan-de-amstel"})

    def test_abcoude_profile_fields(self):
        p = self.profiles["abcoude"]
        self.assertEqual(p.name, "Abcoude")
        self.assertEqual(p.topic_name, "Abcoude")
        self.assertEqual(p.filters_path.name, "Abcoude.json")
        self.assertEqual(p.db_path.name, "abcoude.db")
        self.assertEqual(p.area_slugs, ("abcoude",))

    def test_ouderkerk_profile_fields(self):
        p = self.profiles["ouderkerk-aan-de-amstel"]
        self.assertEqual(p.name, "Ouderkerk aan de Amstel")
        self.assertEqual(p.topic_name, "Ouderkerk aan de Amstel")
        self.assertEqual(p.filters_path.name, "Ouderkerk.json")
        self.assertEqual(p.db_path.name, "ouderkerk-aan-de-amstel.db")
        self.assertEqual(p.area_slugs, ("ouderkerk-aan-de-amstel",))

    def test_db_paths_independent_and_not_funda_db(self):
        dbs = {p.db_path for p in self.profiles.values()}
        self.assertEqual(len(dbs), 2)  # distinct from each other
        for p in self.profiles.values():
            self.assertNotEqual(p.db_path.name, "funda.db")
            self.assertNotEqual(p.db_path.name, "diemen.db")


class FilterValuesTest(unittest.TestCase):
    def test_abcoude_filter_values(self):
        f = FilterConfig.from_file("Abcoude.json")
        self.assertEqual(f.selected_area, "abcoude")
        self.assertEqual(f.price_min, 500000)
        self.assertEqual(f.price_max, 700000)
        self.assertEqual(f.bedrooms_min, 3)
        self.assertEqual(f.living_area_min, 100)
        self.assertEqual(f.object_type, ["house"])
        self.assertEqual(f.availability, "available")
        self.assertEqual(f.sort, "publish_date_utc_desc")

    def test_ouderkerk_filter_values(self):
        f = FilterConfig.from_file("Ouderkerk.json")
        self.assertEqual(f.selected_area, "ouderkerk-aan-de-amstel")
        self.assertEqual(f.price_min, 500000)
        self.assertEqual(f.price_max, 700000)
        self.assertEqual(f.bedrooms_min, 3)
        self.assertEqual(f.living_area_min, 100)
        self.assertEqual(f.object_type, ["house"])
        self.assertEqual(f.availability, "available")
        self.assertEqual(f.sort, "publish_date_utc_desc")

    def test_profiles_use_distinct_selected_area(self):
        a = FilterConfig.from_file("Abcoude.json")
        o = FilterConfig.from_file("Ouderkerk.json")
        self.assertNotEqual(a.selected_area, o.selected_area)


class RoutingTest(unittest.TestCase):
    """run_live/apply_live route only matching listings to the profile topic,
    never to another profile."""

    def test_abcoude_routes_abcoude_only(self):
        p = pr.Profile("abcoude", "Abcoude", "Abcoude",
                       Path("Abcoude.json"), Path(_tmp_db()), "111", ("abcoude",))
        sent = []
        scraped = [
            _house("1", "abcoude"),
            _house("2", "ouderkerk-aan-de-amstel"),   # wrong area
            _house("3", "amsterdam"),                  # wrong area
        ]
        with mock.patch.object(dt, "scrape_funda", return_value=scraped), \
             mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
            n = dt.apply_live(
                FilterConfig.from_file("Abcoude.json"), p.db_path, p.topic_id,
                dry_run=False, area_slugs=p.area_slugs,
                sender=lambda l, t: sent.append((l["listing_id"], t)) or True,
            )
        self.assertEqual(n, 1)
        self.assertEqual(sent, [("1", "111")])

    def test_ouderkerk_routes_ouderkerk_only(self):
        p = pr.Profile("ouderkerk-aan-de-amstel", "Ouderkerk aan de Amstel",
                       "Ouderkerk aan de Amstel", Path("Ouderkerk.json"),
                       Path(_tmp_db()), "222", ("ouderkerk-aan-de-amstel",))
        sent = []
        scraped = [
            _house("1", "abcoude"),                        # wrong area
            _house("2", "ouderkerk-aan-de-amstel"),        # correct
        ]
        with mock.patch.object(dt, "scrape_funda", return_value=scraped), \
             mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
            n = dt.apply_live(
                FilterConfig.from_file("Ouderkerk.json"), p.db_path, p.topic_id,
                dry_run=False, area_slugs=p.area_slugs,
                sender=lambda l, t: sent.append((l["listing_id"], t)) or True,
            )
        self.assertEqual(n, 1)
        self.assertEqual(sent, [("2", "222")])

    def test_non_matching_price_excluded(self):
        p = pr.Profile("abcoude", "Abcoude", "Abcoude",
                       Path("Abcoude.json"), Path(_tmp_db()), "111", ("abcoude",))
        sent = []
        with mock.patch.object(dt, "scrape_funda",
                               return_value=[_house("1", "abcoude", price=400000)]), \
             mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
            n = dt.apply_live(
                FilterConfig.from_file("Abcoude.json"), p.db_path, p.topic_id,
                dry_run=False, area_slugs=p.area_slugs,
                sender=lambda l, t: sent.append((l["listing_id"], t)) or True,
            )
        self.assertEqual(n, 0)
        self.assertEqual(sent, [])


class IndependenceTest(unittest.TestCase):
    def test_sent_ledgers_are_per_profile_db(self):
        # Same listing id posted to Abcoude must not mark it sent in Ouderkerk.
        db_a = _tmp_db()
        db_o = _tmp_db()
        filters_a = FilterConfig.from_file("Abcoude.json")
        filters_o = FilterConfig.from_file("Ouderkerk.json")
        house = _house("shared-1", "abcoude")
        # Post to Abcoude
        with mock.patch.object(dt, "scrape_funda", return_value=[house]), \
             mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
            dt.apply_live(filters_a, db_a, "111", area_slugs=("abcoude",),
                          sender=lambda l, t: True)
        self.assertEqual(dt._diemen_sent_ids(db_a), {"shared-1"})
        self.assertEqual(dt._diemen_sent_ids(db_o), set())  # untouched

    def test_no_repeated_seed_on_second_run(self):
        db = _tmp_db()
        filters = FilterConfig.from_file("Abcoude.json")
        house = _house("1", "abcoude")
        with mock.patch.object(dt, "scrape_funda", return_value=[house]), \
             mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
            n1 = dt.apply_live(filters, db, "111", area_slugs=("abcoude",),
                               sender=lambda l, t: True)
            n2 = dt.apply_live(filters, db, "111", area_slugs=("abcoude",),
                               sender=lambda l, t: True)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)

    def test_profile_db_not_global_funda_db(self):
        for p in pr.load_profiles().values():
            self.assertNotEqual(p.db_path.name, "funda.db")


class DiemenUnaffectedTest(unittest.TestCase):
    def test_default_area_slugs_still_diemen(self):
        # No area_slugs -> Diemen/Duivendrecht slugs are used.
        self.assertTrue(dt.area_is_diemen("diemen"))
        self.assertTrue(dt.area_is_diemen("duivendrecht"))
        self.assertFalse(dt.area_is_diemen("abcoude"))

    def test_diemen_default_matches_diemen_house(self):
        house = _house("1", "diemen")
        self.assertTrue(dt._matches_filters(house, FilterConfig.from_file("Diemen.json")))

    def test_diemen_default_rejects_abcoude(self):
        house = _house("1", "abcoude")
        self.assertFalse(dt._matches_filters(house, FilterConfig.from_file("Diemen.json")))


if __name__ == "__main__":
    unittest.main()
