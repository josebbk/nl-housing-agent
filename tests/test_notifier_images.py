"""
Tests for the enriched Telegram notification and property-image pipeline
(src/notifier.py).

Covers:
* richer listing message metrics (price per m², plot, energy label,
  year built, ownership + lease canon, status);
* deterministic image selection (exactly 3 / fewer / duplicates /
  invalid URLs / ordering);
* image download robustness (HTTP failure, non-image content type,
  oversize payloads, magic-byte validation);
* Telegram delivery behaviour with mocked network (text first, album
  second, partial-failure semantics, single-photo fallback);
* notified-state integration: success marks notified, failure must not;
* a mocked end-to-end flow: listing -> metrics -> selection ->
  download -> send -> notified state.

No real network calls, no real Telegram credentials.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
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


sys.modules.setdefault(
    "dotenv", _make_module("dotenv", load_dotenv=lambda *a, **k: None)
)

from src import notifier  # noqa: E402
from src.notifier import (  # noqa: E402
    _format_listing_message,
    _looks_like_image,
    _select_images,
    _download_image,
    _build_multipart,
    _send_images,
    send_listing_notification,
    send_notifications,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make_listing(**overrides):
    """Realistic listing dict as produced by main.py after detail merge."""
    base = {
        "listing_id": "44480057",
        "url": "https://www.funda.nl/detail/koop/amsterdam/huis-x/44480057/",
        "address": "Hilversumstraat 60",
        "neighborhood": "amsterdam",
        "price": 650000,
        "living_area_m2": 115,
        "plot_size_m2": 113,
        "bedrooms": 3,
        "property_type": "huis",
        "year_built": 1930,
        "energy_label": "B",
        "status": "Beschikbaar",
        "ownership_type": "erfpacht",
        "erfpacht_canon_annual": 408.85,
        "score": 82,
        "score_confidence": "full",
        "score_breakdown": json.dumps([
            {"criterion": "neighborhood_value", "points_earned": 18,
             "points_possible": 21, "matched": True},
            {"criterion": "ownership", "points_earned": 12,
             "points_possible": 17, "matched": True},
        ]),
        "image_urls": [
            "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/230/205/631.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/230/205/733.jpg?options=width=1440",
        ],
    }
    base.update(overrides)
    return base


class _FakeTelegramResponse:
    """Fake urllib response for api.telegram.org JSON endpoints."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeImageResponse:
    """Fake urllib response for an image CDN endpoint."""

    def __init__(self, data=JPEG_MAGIC, content_type="image/jpeg"):
        self._data = data
        self.headers = {"Content-Type": content_type}
        self._read_once = False

    def read(self, n=-1):
        if self._read_once:
            return b""
        self._read_once = True
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# Feature 1 — richer message metrics
# ---------------------------------------------------------------------------

class TestRichMessageFormatting(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_header_contains_price_per_m2(self):
        msg = _format_listing_message(make_listing())
        # 650000 / 115 = 5652.17... -> "5,652"
        self.assertIn("€5,652/m²", msg)

    def test_price_per_m2_omitted_when_area_missing(self):
        msg = _format_listing_message(make_listing(living_area_m2=None))
        self.assertNotIn("/m² ·", msg)
        self.assertIn("N/A", msg)  # required-field convention preserved

    def test_price_per_m2_omitted_when_price_missing(self):
        msg = _format_listing_message(make_listing(price=None))
        self.assertNotIn("/m²", msg)

    def test_facts_line_plot_label_year(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("Plot 113 m² · Label B · Built 1930", msg)

    def test_facts_line_omits_missing_fields(self):
        msg = _format_listing_message(make_listing(
            plot_size_m2=None, energy_label=None))
        self.assertIn("Built 1930", msg)
        self.assertNotIn("Plot", msg)
        self.assertNotIn("Label", msg)

    def test_no_facts_line_when_all_missing(self):
        msg = _format_listing_message(make_listing(
            plot_size_m2=None, energy_label=None, year_built=None))
        self.assertNotIn("Plot", msg)
        self.assertNotIn("Built", msg)

    def test_ownership_erfpacht_with_canon_dutch_decimal(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("Erfpacht (€408,85/yr)", msg)
        self.assertIn("Beschikbaar", msg)

    def test_ownership_full_without_canon(self):
        msg = _format_listing_message(make_listing(
            ownership_type="full", erfpacht_canon_annual=None))
        self.assertIn("Eigendom", msg)
        self.assertNotIn("Erfpacht", msg)

    def test_ownership_line_omitted_when_absent(self):
        msg = _format_listing_message(make_listing(
            ownership_type=None, erfpacht_canon_annual=None, status=None))
        self.assertNotIn("Eigendom", msg)
        self.assertNotIn("Erfpacht", msg)
        self.assertNotIn("Beschikbaar", msg)

    def test_existing_core_format_preserved(self):
        """Phase 1/2 notification contract still holds."""
        msg = _format_listing_message(make_listing())
        self.assertIn("🏠 <b>Hilversumstraat 60</b>", msg)
        self.assertIn("€650,000 · 115 m²", msg)
        self.assertIn("3 bedrooms", msg)
        self.assertIn("huis · amsterdam", msg)
        self.assertIn("⭐ <b>82/100</b>", msg)
        self.assertIn("🟢 Best", msg)
        self.assertIn('href="https://www.funda.nl/detail/koop/amsterdam/huis-x/44480057/"', msg)

    def test_null_required_fields_still_show_na(self):
        msg = _format_listing_message(make_listing(
            price=None, living_area_m2=None, bedrooms=None))
        self.assertIn("N/A", msg)
        self.assertIn("🏠 <b>Hilversumstraat 60</b>", msg)


# ---------------------------------------------------------------------------
# Feature 2 — deterministic image selection
# ---------------------------------------------------------------------------

class TestImageSelection(unittest.TestCase):
    URLS = [
        "https://cloud.funda.nl/valentina_media/a/1.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/2.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/3.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/4.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/5.jpg?options=width=1440",
    ]

    def test_exactly_three_selected_from_five(self):
        picked = _select_images(self.URLS)
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked[1], self.URLS[1])
        self.assertEqual(picked[2], self.URLS[2])

    def test_selection_is_deterministic(self):
        for _ in range(5):
            self.assertEqual(_select_images(self.URLS), _select_images(self.URLS))

    def test_order_follows_gallery_input_order(self):
        picked = _select_images(self.URLS)
        self.assertEqual(picked, self.URLS[:3])

    def test_fewer_than_three_returns_all(self):
        picked = _select_images(self.URLS[:2])
        self.assertEqual(len(picked), 2)

    def test_empty_and_none_inputs(self):
        self.assertEqual(_select_images([]), [])
        self.assertEqual(_select_images(None), [])

    def test_json_string_representation_from_storage(self):
        """Persisted JSON TEXT (raw DB row) is accepted and decoded."""
        import json as _json
        raw = _json.dumps([
            "https://cloud.funda.nl/valentina_media/a/1.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/a/2.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/a/3.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/a/4.jpg?options=width=1440",
        ])
        picked = _select_images(raw)
        self.assertEqual(len(picked), 3)
        self.assertTrue(picked[0].endswith("1.jpg?options=width=1440"))

    def test_unparseable_string_yields_empty_selection(self):
        self.assertEqual(_select_images("garbage-not-json"), [])
        self.assertEqual(_select_images('{"not": "a-list"}'), [])

    def test_duplicate_urls_removed(self):
        dupes = [self.URLS[0], self.URLS[0], self.URLS[1]]
        picked = _select_images(dupes)
        self.assertEqual(len(picked), 2)
        self.assertEqual(len(set(picked)), 2)

    def test_invalid_url_schemes_skipped(self):
        candidates = [
            "javascript:alert(1)",
            "ftp://example.com/x.jpg",
            "/relative/path.jpg",
            "   ",
            None,
            42,
        ] + self.URLS
        picked = _select_images(candidates)
        self.assertEqual(len(picked), 3)
        self.assertTrue(all(u.startswith("https://") for u in picked))

    def test_duplicates_do_not_consume_slots(self):
        dupes = [self.URLS[0]] * 4 + [self.URLS[1], self.URLS[2]]
        picked = _select_images(dupes)
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked, self.URLS[:3])


# ---------------------------------------------------------------------------
# Feature 3 — image download
# ---------------------------------------------------------------------------

class TestImageDownload(unittest.TestCase):
    def _download_to_tmp(self, url, fake_resp):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name) / "img.jpg"
        with mock.patch.object(notifier.request, "urlopen",
                               return_value=fake_resp):
            ok = _download_image(url, dest)
        return ok, dest

    def test_successful_download_writes_file(self):
        ok, dest = self._download_to_tmp(
            "https://cloud.funda.nl/x.jpg", _FakeImageResponse(JPEG_MAGIC))
        self.assertTrue(ok)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), JPEG_MAGIC)

    def test_http_error_returns_false(self):
        import urllib.error
        err = urllib.error.HTTPError(
            "https://cloud.funda.nl/x.jpg", 404, "Not Found", io.BytesIO(), io.BytesIO(),
        )
        with mock.patch.object(notifier.request, "urlopen", side_effect=err):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            dest = Path(tmp.name) / "img.jpg"
            ok = _download_image("https://cloud.funda.nl/x.jpg", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_network_error_returns_false(self):
        import urllib.error
        err = urllib.error.URLError("connection refused")
        with mock.patch.object(notifier.request, "urlopen", side_effect=err):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            dest = Path(tmp.name) / "img.jpg"
            ok = _download_image("https://cloud.funda.nl/x.jpg", dest)
        self.assertFalse(ok)

    def test_non_image_content_type_rejected(self):
        ok, dest = self._download_to_tmp(
            "https://cloud.funda.nl/x.html",
            _FakeImageResponse(b"<html>blocked</html>", content_type="text/html"),
        )
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_html_body_with_image_content_type_rejected_by_magic(self):
        ok, dest = self._download_to_tmp(
            "https://cloud.funda.nl/x.jpg",
            _FakeImageResponse(b"<html>error page</html>", content_type="image/jpeg"),
        )
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_oversize_payload_aborts(self):
        big = JPEG_MAGIC + b"\x00" * (notifier._MAX_IMAGE_BYTES + 1024)
        ok, dest = self._download_to_tmp(
            "https://cloud.funda.nl/big.jpg", _FakeImageResponse(big))
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_png_magic_accepted(self):
        ok, _ = self._download_to_tmp(
            "https://cloud.funda.nl/x.png",
            _FakeImageResponse(PNG_MAGIC, content_type="image/png"))
        self.assertTrue(ok)

    def test_looks_like_image_rejects_garbage(self):
        self.assertFalse(_looks_like_image(b"GIF-fake-not-gif"))
        self.assertFalse(_looks_like_image(b""))
        self.assertTrue(_looks_like_image(JPEG_MAGIC))


# ---------------------------------------------------------------------------
# Feature 4 — Telegram delivery behaviour (mocked network)
# ---------------------------------------------------------------------------

class TestTelegramDelivery(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
        })
        env.start()
        self.addCleanup(env.stop)

    def _patch_downloads(self, result=True):
        """Patch _download_image; when successful it writes real image bytes
        so the multipart uploader can read them back."""
        def fake_download(url, dest):
            if result:
                Path(dest).write_bytes(JPEG_MAGIC)
            return result
        return mock.patch.object(notifier, "_download_image",
                                 side_effect=fake_download)

    @staticmethod
    def _sent_methods(mock_urlopen):
        return [c.args[0].full_url.rsplit("/", 1)[-1]
                for c in mock_urlopen.call_args_list]

    def test_text_then_album_sent_in_order(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())

        self.assertTrue(ok)
        sent_methods = self._sent_methods(urlopen)
        self.assertIn("sendMessage", sent_methods)
        self.assertIn("sendMediaGroup", sent_methods)
        self.assertLess(sent_methods.index("sendMessage"),
                        sent_methods.index("sendMediaGroup"))

    def test_text_failure_means_false_and_no_album_attempt(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": False}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True) as dl:
            ok = send_listing_notification(make_listing())
        self.assertFalse(ok)
        dl.assert_not_called()
        self.assertEqual(urlopen.call_count, 1)

    def test_all_downloads_fail_still_successful_text_only(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(False):
            ok = send_listing_notification(make_listing(image_urls=[
                "https://cloud.funda.nl/a.jpg", "https://cloud.funda.nl/b.jpg"]))
        self.assertTrue(ok)
        sent_methods = self._sent_methods(urlopen)
        self.assertEqual(sent_methods.count("sendMessage"), 1)
        self.assertNotIn("sendMediaGroup", sent_methods)
        self.assertNotIn("sendPhoto", sent_methods)

    def test_single_image_uses_sendphoto(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing(image_urls=[
                "https://cloud.funda.nl/a.jpg"]))
        self.assertTrue(ok)
        self.assertIn("sendPhoto", self._sent_methods(urlopen))

    def test_album_upload_failure_after_delivered_text_still_true(self):
        responses = iter([
            _FakeTelegramResponse({"ok": True}),   # sendMessage OK
            _FakeTelegramResponse({"ok": False}),  # sendMediaGroup fails
        ])
        urlopen = mock.Mock(side_effect=lambda req, timeout=None: next(responses))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())
        self.assertTrue(ok)

    def test_temp_files_cleaned_after_sending(self):
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*a, **k):
            d = real_mkdtemp(*a, **k)
            created_dirs.append(Path(d))
            return d

        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True), \
             mock.patch.object(notifier.tempfile, "mkdtemp", tracking_mkdtemp):
            ok = send_listing_notification(make_listing())
        self.assertTrue(ok)
        for d in created_dirs:
            self.assertFalse(d.exists(), f"{d} not cleaned up")

    def test_send_notifications_batch_results_ordered(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True), \
             mock.patch("time.sleep"):
            results = send_notifications(
                [make_listing(), make_listing(price=700000)], delay=0)
        self.assertEqual(results, [True, True])


# ---------------------------------------------------------------------------
# Multipart building
# ---------------------------------------------------------------------------

class TestMultipartBuilding(unittest.TestCase):
    def test_build_multipart_fields_and_files(self):
        body, boundary = _build_multipart(
            {"chat_id": "123"},
            {"image0": ("image0.jpg", JPEG_MAGIC)},
        )
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b'123', body)
        self.assertIn(b'name="image0"; filename="image0.jpg"', body)
        self.assertIn(JPEG_MAGIC, body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_send_images_two_plus_uses_mediagroup_payload(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return _FakeTelegramResponse({"ok": True})

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = []
        for i in range(2):
            p = Path(tmp.name) / f"i{i}.jpg"
            p.write_bytes(JPEG_MAGIC)
            paths.append(p)

        with mock.patch.object(notifier.request, "urlopen", side_effect=fake_urlopen):
            ok = _send_images("tok", "chat", paths, caption='Zuidas <A>')
        self.assertTrue(ok)
        body = captured["body"].decode("utf-8", errors="replace")
        self.assertIn('"type": "photo"', body.replace("'", '"'))
        self.assertIn("attach://image0", body)
        self.assertIn("attach://image1", body)
        self.assertIn("Zuidas &lt;A&gt;", body)  # caption HTML-escaped


# ---------------------------------------------------------------------------
# Notified-state integration (real temp SQLite, mocked network)
# ---------------------------------------------------------------------------

class TestNotifiedStateIntegration(unittest.TestCase):
    def setUp(self):
        from src.storage import init_db
        env = mock.patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
        })
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "funda.db")
        init_db(self.db)
        from src.storage import insert_listing
        insert_listing(make_listing(), self.db)

    def _notified_flag(self, listing_id):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT notified FROM listings WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _patch_downloads(self, result=True):
        return mock.patch.object(notifier, "_download_image", return_value=result)

    def test_failed_notification_leaves_listing_unnotified(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": False}))
        with mock.patch.object(notifier.request, "urlopen", urlopen):
            ok = send_listing_notification(make_listing())
        self.assertFalse(ok)
        self.assertEqual(self._notified_flag("44480057"), 0)

    def test_successful_notification_marks_notified(self):
        from src.storage import mark_as_notified
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())
        self.assertTrue(ok)
        mark_as_notified("44480057", self.db)
        self.assertEqual(self._notified_flag("44480057"), 1)
        from src.storage import fetch_unnotified_matching_listings
        from src.config import FilterConfig
        remaining = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        self.assertEqual(remaining, [])

    def test_end_to_end_mocked_pipeline(self):
        """listing -> metrics -> select -> download -> send -> notified state."""
        from src.storage import (
            fetch_unnotified_matching_listings, mark_as_notified,
        )
        from src.config import FilterConfig

        listing = make_listing()

        # 1. Metrics present in the formatted message.
        message = _format_listing_message(listing)
        for expected in ("€5,652/m²", "Plot 113 m²", "Label B", "Built 1930",
                         "Erfpacht (€408,85/yr)", "Beschikbaar"):
            self.assertIn(expected, message)

        # 2. Exactly 3 images selected deterministically.
        selected = _select_images(listing["image_urls"])
        self.assertEqual(len(selected), 3)

        # 3. One image download fails; the other two succeed (partial-safe).
        def flaky_download(url, dest):
            if "631" in url:
                return False
            Path(dest).write_bytes(JPEG_MAGIC)
            return True

        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier, "_download_image", side_effect=flaky_download), \
             mock.patch.object(notifier.request, "urlopen", urlopen):
            # 4. Send through the batch API used by main.py.
            results = send_notifications([listing], delay=0)

        self.assertEqual(results, [True])
        sent_methods = [c.args[0].full_url.rsplit("/", 1)[-1]
                        for c in urlopen.call_args_list]
        self.assertIn("sendMediaGroup", sent_methods)

        # 5. Success marks notified; a repeat run finds nothing unnotified.
        mark_as_notified(listing["listing_id"], self.db)
        remaining = fetch_unnotified_matching_listings(
            self.db, filters=FilterConfig.from_file())
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
