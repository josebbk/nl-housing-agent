"""Tests for the Diemen seed + live modes in src/diemen_topic.py.

This new Diemen topic flow is intentionally isolated from the global/old live
notification pipeline (src/main.py). These tests prove that isolation and the
seed/live behaviour, using mocks for Telegram/network and a temp SQLite DB.

All Telegram/network and detail/scoring paths are mocked; nothing is posted.
The main.py global flow, its `notified` flags, config/filters.json, and any
old-topic configuration are never touched by the Diemen seed/live code.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.config import FilterConfig
import src.diemen_topic as dt


DIEMEN_FILTERS = dict(
    price_min=500000, price_max=700000, bedrooms_min=3, living_area_min=100,
    object_type=["house"],
    selected_area="diemen/wijk-diemen-noord,diemen/wijk-diemen-centrum,"
                  "diemen/wijk-diemen-zuid,duivendrecht",
    availability="available", sort="publish_date_utc_desc",
)


def _filters():
    return FilterConfig(**DIEMEN_FILTERS)


def _house(listing_id="1", neighborhood="diemen", price=600000, bedrooms=3,
           living_area=120, notified=0):
    return {
        "listing_id": listing_id,
        "url": f"https://www.funda.nl/detail/koop/{neighborhood}/huis-x/{listing_id}/",
        "address": f"Huisstraat {listing_id}",
        "neighborhood": neighborhood,
        "price": price,
        "living_area_m2": living_area,
        "bedrooms": bedrooms,
        "property_type": "huis",
        "notified": notified,
    }


class _TempDB:
    """Minimal temp DB with just the columns diemen_topic reads."""

    def __init__(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmpdir.name) / "t.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE listings (listing_id TEXT PRIMARY KEY, url TEXT, "
            "address TEXT, neighborhood TEXT, price INTEGER, living_area_m2 "
            "INTEGER, bedrooms INTEGER, property_type TEXT, notified INTEGER)"
        )
        self.conn.commit()

    def add(self, listings):
        self.conn.executemany(
            "INSERT INTO listings (listing_id, url, address, neighborhood, "
            "price, living_area_m2, bedrooms, property_type, notified) "
            "VALUES (:listing_id, :url, :address, :neighborhood, :price, "
            ":living_area_m2, :bedrooms, :property_type, :notified)",
            listings,
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
        self.tmpdir.cleanup()


class SeedSelectionTest(unittest.TestCase):
    """select_for_seed: count handling, no fabrication, skip already-sent."""

    def setUp(self):
        self.filters = _filters()

    def test_keeps_all_available_match_order(self):
        cands = [_house(str(i)) for i in range(7)]
        sel = dt.select_for_seed(cands, already_sent={"9"}, requested=50)
        self.assertEqual(len(sel), 7)
        self.assertEqual([l["listing_id"] for l in sel], [str(i) for i in range(7)])

    def test_respects_requested_count_cap(self):
        cands = [_house(str(i)) for i in range(7)]
        sel = dt.select_for_seed(cands, already_sent=set(), requested=4)
        self.assertEqual(len(sel), 4)

    def test_skips_already_sent(self):
        cands = [_house(str(i)) for i in range(7)]
        sel = dt.select_for_seed(cands, already_sent={"1", "4"}, requested=50)
        self.assertEqual([l["listing_id"] for l in sel], ["0", "2", "3", "5", "6"])

    def test_fewer_than_requested_no_fabrication(self):
        cands = [_house(str(i)) for i in range(7)]
        sel = dt.select_for_seed(cands, already_sent=set(), requested=50)
        self.assertLess(len(sel), 50)
        self.assertEqual(len(sel), 7)

    def test_empty_candidates(self):
        self.assertEqual(dt.select_for_seed([], set(), 50), [])


class LoadFromDbTest(unittest.TestCase):
    def test_loads_only_matching_diemen_rows(self):
        db = _TempDB()
        try:
            db.add([
                _house("1", neighborhood="diemen", price=600000, bedrooms=4, living_area=120),
                _house("2", neighborhood="amsterdam", price=600000, bedrooms=4, living_area=120),
                _house("3", neighborhood="diemen", price=400000, bedrooms=3, living_area=120),
                _house("4", neighborhood="duivendrecht", price=650000, bedrooms=3, living_area=130),
            ])
            got = dt.load_seed_candidates(filters=_filters(), db_path=db.path)
            self.assertEqual({l["listing_id"] for l in got}, {"1", "4"})
        finally:
            db.close()


class DiemenSentStoreTest(unittest.TestCase):
    def test_mark_and_read_roundtrip(self):
        db = _TempDB()
        try:
            dt._init_diemen_sent(db.path)
            dt._mark_diemen_sent("77", db.path)
            self.assertEqual(dt._diemen_sent_ids(db.path), {"77"})
            dt._mark_diemen_sent("88", db.path)
            self.assertEqual(dt._diemen_sent_ids(db.path), {"77", "88"})
        finally:
            db.close()

    def test_init_is_idempotent(self):
        db = _TempDB()
        try:
            dt._init_diemen_sent(db.path)
            dt._init_diemen_sent(db.path)  # must not raise
            self.assertEqual(dt._diemen_sent_ids(db.path), set())
        finally:
            db.close()

    def test_does_not_touch_global_notified(self):
        db = _TempDB()
        try:
            db.add([_house("1")])
            dt._init_diemen_sent(db.path)
            dt._mark_diemen_sent("1", db.path)
            row = db.conn.execute(
                "SELECT notified FROM listings WHERE listing_id='1'").fetchone()
            self.assertEqual(row[0], 0)  # notified unchanged
        finally:
            db.close()


class SeedRunsIsolatedTest(unittest.TestCase):
    """apply_seed: posts only to the Diemen topic, uses rich sender, marks
    diemen_sent, never touches notified, never touches other topics."""

    def test_posts_to_diemen_topic_id_only(self):
        db = _TempDB()
        try:
            db.add([_house("1"), _house("2"), _house("3")])
            dt._init_diemen_sent(db.path)
            calls = []
            sender = lambda listing, thread_id: calls.append(
                (listing["listing_id"], thread_id)) or True
            with mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_seed(
                    candidates=[_house("1"), _house("2"), _house("3")],
                    filters=_filters(), db_path=db.path, requested=50,
                    topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False, sender=sender,
                )
            self.assertEqual(n, 3)
            for cid, tid in calls:
                self.assertEqual(tid, dt.DIEMEN_TOPIC_ID)
            self.assertEqual(dt._diemen_sent_ids(db.path), {"1", "2", "3"})
        finally:
            db.close()

    def test_dry_run_sends_nothing_and_marks_nothing(self):
        db = _TempDB()
        try:
            db.add([_house("1")])
            dt._init_diemen_sent(db.path)
            sender = lambda listing, thread_id: (_ for _ in ()).throw(
                AssertionError("dry-run must not send"))
            with mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_seed(
                    candidates=[_house("1")], filters=_filters(),
                    db_path=db.path, requested=1, topic_id=dt.DIEMEN_TOPIC_ID,
                    dry_run=True, sender=sender,
                )
            self.assertEqual(n, 0)
            self.assertEqual(dt._diemen_sent_ids(db.path), set())
        finally:
            db.close()

    def test_global_notified_untouched_and_old_topic_untouched(self):
        db = _TempDB()
        try:
            db.add([_house("1", notified=0), _house("2", notified=1)])
            dt._init_diemen_sent(db.path)
            # Use the real notifier path via a mock so we can assert it is
            # only ever asked for the Diemen thread id.
            with mock.patch.object(dt, "send_listing_notification",
                                   return_value=True) as send, \
                 mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_seed(
                    candidates=[_house("1", notified=0), _house("2", notified=1)],
                    filters=_filters(), db_path=db.path, requested=50,
                    topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False,
                )
            self.assertEqual(n, 2)
            for c in send.call_args_list:
                self.assertEqual(c[0][1], dt.DIEMEN_TOPIC_ID)  # thread id override
            # notified column unchanged for both
            vals = {
                r[0]: r[1] for r in db.conn.execute(
                    "SELECT listing_id, notified FROM listings").fetchall()
            }
            self.assertEqual(vals["1"], 0)
            self.assertEqual(vals["2"], 1)
        finally:
            db.close()


class LiveRoutingTest(unittest.TestCase):
    """apply_live: only new matching Diemen listings -> Diemen topic; nothing
    else; non-matching never routed; old flow untouched."""

    def test_posts_new_matches_only_to_diemen_topic(self):
        db = _TempDB()
        try:
            # "already in topic" via diemen_sent and "already in DB" both skip
            db.add([_house("1")])
            dt._init_diemen_sent(db.path)
            dt._mark_diemen_sent("1", db.path)
            scraped = [
                _house("1"),           # already sent -> skip
                _house("2", neighborhood="amsterdam"),  # wrong area -> skip
                _house("3", price=400000),              # out of price -> skip
                _house("4", neighborhood="diemen"),     # new match -> post
            ]
            sent_ids = []
            sender = lambda listing, thread_id: sent_ids.append(
                (listing["listing_id"], thread_id)) or True
            with mock.patch.object(dt, "_scrape_kwargs", return_value={}), \
                 mock.patch.object(dt, "scrape_funda", return_value=scraped), \
                 mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_live(
                    filters=_filters(), db_path=db.path,
                    topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False, sender=sender,
                )
            self.assertEqual(sent_ids, [("4", dt.DIEMEN_TOPIC_ID)])
            self.assertEqual(n, 1)
            self.assertEqual(dt._diemen_sent_ids(db.path), {"1", "4"})
        finally:
            db.close()

    def test_nothing_sent_when_no_new_match(self):
        db = _TempDB()
        try:
            dt._init_diemen_sent(db.path)
            sender = lambda listing, thread_id: (_ for _ in ()).throw(
                AssertionError("no new matching listings to post"))
            with mock.patch.object(dt, "scrape_funda", return_value=[]), \
                 mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_live(
                    filters=_filters(), db_path=db.path,
                    topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False, sender=sender,
                )
            self.assertEqual(n, 0)
        finally:
            db.close()


class IsolationGuaranteesTest(unittest.TestCase):
    def test_live_uses_diemen_filters(self):
        with mock.patch.object(dt, "scrape_funda", return_value=[]) as sc:
            dt.apply_live(filters=_filters(), db_path=":memory:",
                          topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False,
                          sender=lambda l, t: True)
        # apply_live must map filters through _scrape_kwargs (the Diemen set)
        # is invoked with the Diemen filters' area.
        self.assertEqual(sc.call_args.kwargs["area"], DIEMEN_FILTERS["selected_area"])

    def test_diemen_topic_id_is_fixed_target(self):
        self.assertIsInstance(dt.DIEMEN_TOPIC_ID, str)
        self.assertTrue(dt.DIEMEN_TOPIC_ID)  # non-empty target


class IsolatedDatabaseTest(unittest.TestCase):
    """The Diemen flow uses its own data/diemen.db, separate from the old
    flow's data/funda.db. It must not read the old flow's `listings` table or
    `notified` flag for dedup."""

    def test_default_db_is_diemen_db_not_funda_db(self):
        self.assertEqual(dt._DEFAULT_DB_PATH.name, "diemen.db")
        self.assertNotEqual(dt._DEFAULT_DB_PATH.name, "funda.db")

    def test_live_dedups_on_diemen_sent_only(self):
        # A listing present in the old flow's `listings` table but NOT in the
        # Diemen sent-ledger must still be posted: the Diemen live flow does
        # not read the old flow's listings/notified state.
        db = _TempDB()
        try:
            db.add([_house("1", neighborhood="diemen")])  # in listings, notified=0
            dt._init_diemen_sent(db.path)  # but diemen_sent is empty
            sent = []
            with mock.patch.object(dt, "scrape_funda",
                                   return_value=[_house("1")]), \
                 mock.patch.object(dt, "_enrich", side_effect=lambda l, f: l):
                n = dt.apply_live(
                    filters=_filters(), db_path=db.path,
                    topic_id=dt.DIEMEN_TOPIC_ID, dry_run=False,
                    sender=lambda l, t: sent.append((l["listing_id"], t)) or True,
                )
            self.assertEqual(n, 1)
            self.assertEqual(sent, [("1", dt.DIEMEN_TOPIC_ID)])
            # notified flag untouched by the Diemen flow
            row = db.conn.execute(
                "SELECT notified FROM listings WHERE listing_id='1'").fetchone()
            self.assertEqual(row[0], 0)
        finally:
            db.close()

    def test_load_seed_candidates_tolerates_missing_listings_table(self):
        # The isolated diemen.db holds only the diemen_sent ledger, so a seed
        # candidate query against a DB without a listings table yields [].
        db = _TempDB()
        try:
            db.conn.execute("DROP TABLE listings")
            db.conn.commit()
            self.assertEqual(
                dt.load_seed_candidates(_filters(), db.path), [])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
