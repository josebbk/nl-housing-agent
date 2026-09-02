"""Tests for the notifier forum-topic additions (src/notifier.py).

Covers:
  * create_forum_topic — parses the new topic's message_thread_id from a
    mocked Telegram createForumTopic response and handles failures;
  * send_listing_notification(thread_id=...) — the optional thread override
    is forwarded to the underlying sendMessage/sendMediaGroup so a listing
    can be posted to a specific new topic instead of the env default.

Telegram/network interactions are mocked; no real bot is contacted.
"""

import json
import unittest
from unittest import mock
from urllib import error

from src import notifier


class _FakeResponse:
    def __init__(self, payload_bytes):
        self._b = payload_bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *a, **k):
        return self._b


def _json_response(ok, result=None, description=None):
    body = {"ok": ok}
    if result is not None:
        body["result"] = result
    if description is not None:
        body["description"] = description
    return _FakeResponse(json.dumps(body).encode("utf-8"))


def _fake_urlopen(response):
    return mock.patch.object(
        notifier.request, "urlopen", return_value=response
    )


class CreateForumTopicTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(notifier, "_get_token", return_value="tok"), \
             mock.patch.object(notifier, "_get_chat_id", return_value="-100123"):
            self.ctx_token = notifier._get_token()
            self.ctx_chat = notifier._get_chat_id()

    def test_returns_message_thread_id_on_success(self):
        resp = _json_response(True, result={"message_thread_id": 42, "name": "T"})
        with _fake_urlopen(resp):
            with mock.patch.object(notifier, "_get_token", return_value="tok"):
                with mock.patch.object(notifier, "_get_chat_id", return_value="-100123"):
                    tid = notifier.create_forum_topic("Diemen — Funda Matches")
        self.assertEqual(tid, "42")

    def test_returns_none_when_not_ok(self):
        resp = _json_response(False, description="method is only available for supergroups")
        with _fake_urlopen(resp):
            with mock.patch.object(notifier, "_get_token", return_value="tok"):
                with mock.patch.object(notifier, "_get_chat_id", return_value="-100123"):
                    tid = notifier.create_forum_topic("T")
        self.assertIsNone(tid)

    def test_returns_none_on_http_403(self):
        http = error.HTTPError("url", 403, "Forbidden", {}, None)
        with mock.patch.object(notifier, "_get_token", return_value="tok"), \
             mock.patch.object(notifier, "_get_chat_id", return_value="-100123"), \
             mock.patch.object(notifier.request, "urlopen",
                               side_effect=http) as uo:
            tid = notifier.create_forum_topic("T")
        self.assertIsNone(tid)

    def test_returns_none_on_network_error(self):
        with mock.patch.object(notifier, "_get_token", return_value="tok"), \
             mock.patch.object(notifier, "_get_chat_id", return_value="-100123"), \
             mock.patch.object(notifier.request, "urlopen",
                               side_effect=error.URLError("net")) as uo:
            tid = notifier.create_forum_topic("T")
        self.assertIsNone(tid)


class SendListingNotificationThreadOverrideTest(unittest.TestCase):
    def _listing(self):
        return {
            "listing_id": "d-1",
            "url": "https://www.funda.nl/detail/koop/diemen/huis-x/999/",
            "address": "Diemenstraat 1",
            "neighborhood": "diemen",
            "price": 600000,
            "living_area_m2": 120,
            "bedrooms": 3,
            "property_type": "huis",
        }

    def test_removes_downloaded_images_and_only_text(self):
        """No images -> text-only path -> thread_id forwarded to _send_message."""
        listing = self._listing()
        with mock.patch.object(notifier, "_get_token", return_value="tok"), \
             mock.patch.object(notifier, "_get_chat_id", return_value="-100123"), \
             mock.patch.object(notifier, "_select_images", return_value=[]) as sel, \
             mock.patch.object(notifier, "_send_message", return_value=True) as send:
            ok = notifier.send_listing_notification(listing, thread_id="42")
        self.assertTrue(ok)
        self.assertEqual(send.call_args.kwargs.get("thread_id"), "42")


if __name__ == "__main__":
    unittest.main()
