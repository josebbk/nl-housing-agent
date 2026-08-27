"""
Tests for the Telegram notification presentation (src/notifier.py).

Covers the coherent-notification contract:

* message formatting — emoji-structured layout (header metrics, score
  section, key facts, Funda URL) with values unchanged from the listing
  data, missing fields omitted, and all 12 scoring-criterion display
  labels rendered;
* coherent delivery — text and photos delivered together as ONE
  Telegram media message (sendPhoto / sendMediaGroup with the full
  message as HTML caption); no standalone duplicate text message;
* fallbacks — text-only sendMessage when no photos are available or all
  downloads fail, when an album upload fails, and text-first delivery
  when the message is too long for a media caption;
* deterministic image selection (unchanged business rule: first 3
  unique http(s) URLs in gallery order).

No real network calls, no real Telegram credentials.
"""

import io
import json
import os
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
    _fits_telegram_caption,
    _looks_like_image,
    _select_images,
    _send_images,
    send_listing_notification,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16

FULL_BREAKDOWN = [
    {"criterion": "neighborhood_value", "points_earned": 18, "points_possible": 21, "matched": True},
    {"criterion": "construction_condition", "points_earned": 10, "points_possible": 11, "matched": True},
    {"criterion": "ownership", "points_earned": 17, "points_possible": 17, "matched": True},
    {"criterion": "energy_label", "points_earned": 14, "points_possible": 14, "matched": True},
    {"criterion": "living_area", "points_earned": 12, "points_possible": 12, "matched": True},
    {"criterion": "garage", "points_earned": 8, "points_possible": 8, "matched": True},
    {"criterion": "parking", "points_earned": 5, "points_possible": 8, "matched": True},
    {"criterion": "rooms", "points_earned": 5, "points_possible": 7, "matched": True},
    {"criterion": "plot_size", "points_earned": 3, "points_possible": 4, "matched": True},
    {"criterion": "garden", "points_earned": 3, "points_possible": 4, "matched": True},
    {"criterion": "heating", "points_earned": 2, "points_possible": 5, "matched": False},
    {"criterion": "balcony", "points_earned": 0, "points_possible": 3, "matched": True},
]

PARTIAL_BREAKDOWN = [
    {"criterion": "neighborhood_value", "points_earned": 12, "points_possible": 21, "matched": False},
    {"criterion": "ownership", "points_earned": 17, "points_possible": 17, "matched": True},
    {"criterion": "parking", "points_earned": 0, "points_possible": 8, "matched": False},
    {"criterion": "rooms", "points_earned": 5, "points_possible": 7, "matched": True},
]


def make_listing(**overrides):
    """Realistic listing dict as produced by main.py after detail merge."""
    base = {
        "listing_id": "80918988",
        "url": ("https://www.funda.nl/detail/koop/amsterdam/"
                "huis-lambert-rimastraat-15/80918988/"),
        "address": "Lambert Rimastraat 15",
        "neighborhood": "amsterdam",
        "price": 599000,
        "living_area_m2": 133,
        "plot_size_m2": 122,
        "bedrooms": 3,
        "property_type": "huis",
        "year_built": 1996,
        "energy_label": "C",
        "status": "Beschikbaar",
        "ownership_type": "full",
        "erfpacht_canon_annual": None,
        "score": 82,
        "score_confidence": "full",
        "score_breakdown": json.dumps(FULL_BREAKDOWN),
        "image_urls": [
            "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/230/205/631.jpg?options=width=1440",
            "https://cloud.funda.nl/valentina_media/230/205/733.jpg?options=width=1440",
        ],
    }
    base.update(overrides)
    return base


class _FakeTelegramResponse:
    """Fake urllib response for api.telegram.org endpoints."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _extract_field(body: bytes, name: str) -> bytes:
    """Extract a form-data text field value from a multipart body."""
    marker = f'name="{name}"\r\n\r\n'.encode("utf-8")
    start = body.index(marker) + len(marker)
    end = body.index(b"\r\n", start)
    return body[start:end]


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

class TestMessageFormatting(unittest.TestCase):
    def test_header_uses_metric_name_colon_value_structure(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("🏠 <b>Lambert Rimastraat 15</b>", msg)
        self.assertIn("💰 Price: €599,000", msg)
        self.assertIn("📐 Size: 133 m² · €4,504/m²", msg)
        self.assertIn("🛏 Bedrooms: 3", msg)
        self.assertIn("⚡ Energy label: C", msg)
        self.assertIn("📍 Location: Amsterdam · Lambert Rimastraat", msg)
        self.assertIn("📏 Plot: 122 m²", msg)
        self.assertIn("🏗 Year built: 1996", msg)

    def test_every_metric_line_has_emoji_name_and_colon(self):
        msg = _format_listing_message(make_listing())
        metric_lines = [
            line for line in msg.splitlines()
            if line and not line.startswith("🔗") and "🏠" not in line
        ]
        self.assertTrue(metric_lines)
        for line in metric_lines:
            self.assertRegex(line, r"^\S+ [A-Za-z ]+: ", line)

    def test_no_dutch_property_terminology(self):
        msg = _format_listing_message(make_listing(
            property_type="Eengezinswoning, tussenwoning",
            status="Beschikbaar",
            ownership_type="erfpacht",
            erfpacht_canon_annual=408.85))
        for dutch in ("Eengezinswoning", "tussenwoning", "Erfpacht",
                      "Beschikbaar", "Eigendom"):
            self.assertNotIn(dutch, msg)

    def test_address_kept_exactly_as_provided(self):
        msg = _format_listing_message(make_listing(
            address="Van Woustraat 245-III, Amsterdam"))
        self.assertIn("🏠 <b>Van Woustraat 245-III, Amsterdam</b>", msg)

    def test_price_per_m2_present_and_omitted(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("€4,504/m²", msg)
        msg = _format_listing_message(make_listing(living_area_m2=None))
        self.assertNotIn("/m²", msg)
        self.assertIn("📐 Size: N/A", msg)
        msg = _format_listing_message(make_listing(price=None))
        self.assertNotIn("/m²", msg)
        self.assertIn("💰 Price: N/A", msg)

    def test_optional_header_lines_omitted(self):
        msg = _format_listing_message(make_listing(
            energy_label=None, neighborhood=None, plot_size_m2=None,
            year_built=None))
        self.assertNotIn("⚡", msg)
        self.assertNotIn("📍", msg)
        self.assertNotIn("📏", msg)
        self.assertNotIn("🏗", msg)


# ---------------------------------------------------------------------------
# Property features (Garage / Parking / Garden)
# ---------------------------------------------------------------------------

class TestPropertyFeatureLines(unittest.TestCase):
    def test_garage_english_codes(self):
        self.assertIn("🚗 Garage: Attached",
                      _format_listing_message(make_listing(garage_type="attached")))
        self.assertIn("🚗 Garage: Detached",
                      _format_listing_message(make_listing(garage_type="detached")))
        self.assertIn("🚗 Garage: Carport",
                      _format_listing_message(make_listing(garage_type="carport")))

    def test_garage_dutch_raw_values_mapped_to_english(self):
        self.assertIn("🚗 Garage: Possible",
                      _format_listing_message(make_listing(garage_type="Garage mogelijk")))
        self.assertIn("🚗 Garage: Detached",
                      _format_listing_message(make_listing(garage_type="Vrijstaande garage")))
        self.assertIn("🚗 Garage: Attached",
                      _format_listing_message(make_listing(garage_type="Inpandige garage")))

    def test_garage_shows_no_when_missing_or_unrecognized(self):
        msg = _format_listing_message(make_listing(garage_type=None))
        self.assertIn("🚗 Garage: No", msg)
        msg = _format_listing_message(make_listing(garage_type="Elk soort garage"))
        self.assertIn("🚗 Garage: No", msg)

    def test_parking_english_codes(self):
        for raw, label in (("private", "Private"), ("carport", "Carport"),
                           ("public", "Public"), ("paid", "Paid")):
            self.assertIn(f"🅿️ Parking: {label}",
                          _format_listing_message(make_listing(parking_type=raw)))

    def test_parking_dutch_raw_values_mapped_to_english(self):
        self.assertIn("🅿️ Parking: Private",
                      _format_listing_message(make_listing(parking_type="Op eigen terrein")))
        self.assertIn("🅿️ Parking: Paid",
                      _format_listing_message(make_listing(parking_type="Betaald parkeren")))
        self.assertIn("🅿️ Parking: Public",
                      _format_listing_message(make_listing(parking_type="Openbaar parkeren")))
        self.assertIn("🅿️ Parking: No",
                      _format_listing_message(make_listing(parking_type="geen parkeergelegenheid")))

    def test_parking_shows_no_when_missing(self):
        msg = _format_listing_message(make_listing(parking_type=None))
        self.assertIn("🅿️ Parking: No", msg)

    def test_garden_yes_and_no(self):
        msg = _format_listing_message(make_listing(garden_present=True))
        self.assertIn("🌳 Garden: Yes", msg)
        msg = _format_listing_message(make_listing(
            garden_present=None, garden_size_m2=37))
        self.assertIn("🌳 Garden: Yes", msg)
        msg = _format_listing_message(make_listing(
            garden_present=False, garden_size_m2=None))
        self.assertIn("🌳 Garden: No", msg)
        msg = _format_listing_message(make_listing(
            garden_present=None, garden_size_m2=None))
        self.assertIn("🌳 Garden: No", msg)

    def test_features_ordered_after_year_built_before_score(self):
        msg = _format_listing_message(make_listing(
            garage_type="attached", parking_type="private", garden_present=True))
        lines = msg.splitlines()
        year_idx = next(i for i, l in enumerate(lines) if "Year built" in l)
        garage_idx = next(i for i, l in enumerate(lines) if "Garage:" in l)
        parking_idx = next(i for i, l in enumerate(lines) if "Parking:" in l)
        garden_idx = next(i for i, l in enumerate(lines) if "Garden:" in l)
        score_idx = next(i for i, l in enumerate(lines) if "Score:" in l)
        self.assertTrue(year_idx < garage_idx < parking_idx < garden_idx < score_idx)


# ---------------------------------------------------------------------------
# Location construction
# ---------------------------------------------------------------------------

class TestLocationConstruction(unittest.TestCase):
    def test_city_and_street_shown_in_order(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("📍 Location: Amsterdam · Lambert Rimastraat", msg)

    def test_street_omitted_when_no_house_number(self):
        msg = _format_listing_message(make_listing(address="Open huis"))
        self.assertIn("📍 Location: Amsterdam", msg)
        self.assertNotIn("·", _format_listing_message(make_listing(
            address="Open huis", neighborhood=None)).splitlines()[-1])

    def test_location_omitted_without_neighborhood(self):
        msg = _format_listing_message(make_listing(neighborhood=None))
        self.assertNotIn("📍", msg)

    def test_address_variants_strip_house_number(self):
        for addr, street in (
            ("Zeelandstraat 34-3", "Zeelandstraat"),
            ("IJsselmeerstraat 80-A", "IJsselmeerstraat"),
            ("Sam van Houtenstraat 197-E", "Sam van Houtenstraat"),
            ("Van Woustraat 245-III", "Van Woustraat"),
            ("Bos en Lommerplein 228", "Bos en Lommerplein"),
        ):
            msg = _format_listing_message(make_listing(address=addr))
            self.assertIn(f"📍 Location: Amsterdam · {street}", msg)


# ---------------------------------------------------------------------------
# English-only non-numeric values
# ---------------------------------------------------------------------------

class TestEnglishOnlyValues(unittest.TestCase):
    def test_energy_label_guard_hides_garbled_dutch(self):
        garbled = ("Niet verplichtVerwarmingCv-ketelWarm water"
                   "Cv-ketelCv-ketelGas gestookt combiketel, eigendom")
        msg = _format_listing_message(make_listing(energy_label=garbled))
        self.assertNotIn("⚡", msg)
        msg = _format_listing_message(make_listing(energy_label="Niet verplicht"))
        self.assertNotIn("⚡", msg)
        msg = _format_listing_message(make_listing(energy_label="A+++"))
        self.assertIn("⚡ Energy label: A+++", msg)

    def test_score_section_values_unchanged(self):
        msg = _format_listing_message(make_listing())
        self.assertIn("⭐ Score: <b>82/100</b>", msg)
        self.assertIn("🟢 Best: Neighborhood 18/21 · Ownership 17/17 · Energy 14/14", msg)
        self.assertIn("🔴 Weakest: Balcony 0/3 · Plot size 3/4 · Garden 3/4", msg)
        self.assertIn("📊 Breakdown:", msg)
        self.assertIn("Neighborhood 18/21", msg)
        self.assertIn("Ownership 17/17", msg)
        self.assertNotIn("⚠️ Adjusted", msg)  # confidence == full

    def test_partial_confidence_adjusted_line(self):
        msg = _format_listing_message(make_listing(
            score=78, score_confidence="partial",
            score_breakdown=json.dumps(PARTIAL_BREAKDOWN)))
        self.assertIn("⭐ Score: <b>78/100</b>", msg)
        self.assertIn("⚠️ Adjusted: Neighborhood, Parking data unavailable", msg)
        self.assertIn("Neighborhood N/A", msg)
        self.assertIn("Parking N/A", msg)

    def test_no_data_shows_unavailable_score(self):
        msg = _format_listing_message(make_listing(
            score=None, score_confidence="no_data",
            score_breakdown=json.dumps([])))
        self.assertIn("⭐ Score: unavailable", msg)
        self.assertNotIn("⚠️", msg)
        self.assertNotIn("🟢", msg)
        self.assertNotIn("🔴", msg)
        self.assertNotIn("📊", msg)
        self.assertIn("🔗", msg)

    def test_new_criterion_display_labels(self):
        msg = _format_listing_message(make_listing())
        for label in ("Garage", "Plot size", "Balcony", "Heating"):
            self.assertIn(label, msg)

    def test_url_link_present(self):
        msg = _format_listing_message(make_listing())
        self.assertIn('🔗 <a href="https://www.funda.nl/detail/koop/amsterdam/'
                      'huis-lambert-rimastraat-15/80918988/">View on Funda</a>', msg)

    def test_realistic_full_message_fits_media_caption(self):
        msg = _format_listing_message(make_listing())
        self.assertTrue(_fits_telegram_caption(msg))
        self.assertLessEqual(len(msg), 1000)

    def test_fits_telegram_caption_boundary(self):
        self.assertTrue(_fits_telegram_caption("x" * 1000))
        self.assertFalse(_fits_telegram_caption("x" * 1001))


# ---------------------------------------------------------------------------
# Coherent delivery (mocked network)
# ---------------------------------------------------------------------------

class TestCoherentDelivery(unittest.TestCase):
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

    def test_album_with_full_caption_is_the_only_message(self):
        """3 photos -> ONE sendMediaGroup carrying the full text as caption;
        no standalone sendMessage."""
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())

        self.assertTrue(ok)
        sent_methods = self._sent_methods(urlopen)
        self.assertEqual(sent_methods, ["sendMediaGroup"])
        body = urlopen.call_args_list[0].args[0].data
        media = json.loads(_extract_field(body, "media").decode("utf-8"))
        self.assertEqual(len(media), 3)
        caption = media[0]["caption"]
        self.assertIn("🏠 <b>Lambert Rimastraat 15</b>", caption)
        self.assertIn("⭐ Score: <b>82/100</b>", caption)
        self.assertIn("View on Funda", caption)
        self.assertEqual(media[0]["parse_mode"], "HTML")

    def test_single_photo_carries_full_caption(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing(image_urls=[
                "https://cloud.funda.nl/valentina_media/a/1.jpg"]))

        self.assertTrue(ok)
        self.assertEqual(self._sent_methods(urlopen), ["sendPhoto"])
        body = urlopen.call_args_list[0].args[0].data
        caption = _extract_field(body, "caption").decode("utf-8")
        self.assertIn("🏠 <b>Lambert Rimastraat 15</b>", caption)

    def test_no_images_yields_single_text_message(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True) as dl:
            ok = send_listing_notification(make_listing(image_urls=None))

        self.assertTrue(ok)
        dl.assert_not_called()
        self.assertEqual(self._sent_methods(urlopen), ["sendMessage"])
        body = urlopen.call_args_list[0].args[0].data
        payload = json.loads(body)
        self.assertIn("🏠 <b>Lambert Rimastraat 15</b>", payload["text"])

    def test_all_downloads_fail_yields_single_text_message(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(False):
            ok = send_listing_notification(make_listing())

        self.assertTrue(ok)
        self.assertEqual(self._sent_methods(urlopen), ["sendMessage"])

    def test_album_failure_falls_back_to_text_only(self):
        responses = iter([
            _FakeTelegramResponse({"ok": False}),  # sendMediaGroup fails
            _FakeTelegramResponse({"ok": True}),   # fallback sendMessage OK
        ])
        urlopen = mock.Mock(side_effect=lambda req, timeout=None: next(responses))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())

        self.assertTrue(ok)
        # exactly one album attempt + one text message; no image retry
        self.assertEqual(self._sent_methods(urlopen),
                         ["sendMediaGroup", "sendMessage"])

    def test_album_and_text_failure_returns_false(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": False}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(make_listing())
        self.assertFalse(ok)
        self.assertEqual(self._sent_methods(urlopen),
                         ["sendMediaGroup", "sendMessage"])

    def test_text_failure_without_images_returns_false(self):
        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": False}))
        with mock.patch.object(notifier.request, "urlopen", urlopen):
            ok = send_listing_notification(make_listing(image_urls=None))
        self.assertFalse(ok)
        self.assertEqual(self._sent_methods(urlopen), ["sendMessage"])

    def test_oversized_message_uses_text_first_then_album(self):
        # A breakdown with many criteria pushes the message past the
        # Telegram media-caption limit.
        many = [
            {"criterion": f"criterion_{i}", "points_earned": 10,
             "points_possible": 20, "matched": True}
            for i in range(40)
        ]
        listing = make_listing(score_breakdown=json.dumps(many))
        self.assertFalse(_fits_telegram_caption(
            _format_listing_message(listing)))

        urlopen = mock.Mock(return_value=_FakeTelegramResponse({"ok": True}))
        with mock.patch.object(notifier.request, "urlopen", urlopen), \
             self._patch_downloads(True):
            ok = send_listing_notification(listing)

        self.assertTrue(ok)
        sent_methods = self._sent_methods(urlopen)
        self.assertEqual(sent_methods, ["sendMessage", "sendMediaGroup"])
        # The album caption is the plain address only (escaped), not the
        # full rich message.
        album_body = urlopen.call_args_list[1].args[0].data
        media = json.loads(_extract_field(album_body, "media").decode("utf-8"))
        self.assertIn("Lambert Rimastraat 15", media[0]["caption"])
        self.assertNotIn("⭐", media[0]["caption"])

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


# ---------------------------------------------------------------------------
# Image selection (unchanged business rule)
# ---------------------------------------------------------------------------

class TestImageSelection(unittest.TestCase):
    URLS = [
        "https://cloud.funda.nl/valentina_media/a/1.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/2.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/3.jpg?options=width=1440",
        "https://cloud.funda.nl/valentina_media/a/4.jpg?options=width=1440",
    ]

    def test_exactly_three_selected_in_gallery_order(self):
        picked = _select_images(self.URLS)
        self.assertEqual(picked, self.URLS[:3])

    def test_fewer_than_three_returns_all(self):
        self.assertEqual(_select_images(self.URLS[:2]), self.URLS[:2])

    def test_duplicates_and_invalid_urls_skipped(self):
        picked = _select_images(
            ["javascript:alert(1)", None, 42, "   ", self.URLS[0],
             self.URLS[0], self.URLS[1]])
        self.assertEqual(picked, [self.URLS[0], self.URLS[1]])

    def test_json_string_representation_accepted(self):
        raw = json.dumps([self.URLS[0], self.URLS[1]])
        picked = _select_images(raw)
        self.assertEqual(picked, [self.URLS[0], self.URLS[1]])

    def test_looks_like_image(self):
        self.assertTrue(_looks_like_image(JPEG_MAGIC))
        self.assertFalse(_looks_like_image(b"<html>nope</html>"))
        self.assertFalse(_looks_like_image(b""))


# ---------------------------------------------------------------------------
# _send_images caption handling
# ---------------------------------------------------------------------------

class TestSendImagesCaption(unittest.TestCase):
    def test_caption_sent_verbatim_as_html(self):
        """The full HTML message is NOT escaped when riding on the media."""
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

        caption = "🏠 <b>Lambert Rimastraat 15</b> & more"
        with mock.patch.object(notifier.request, "urlopen",
                               side_effect=fake_urlopen):
            ok = _send_images("tok", "chat", paths, caption=caption)

        self.assertTrue(ok)
        body = captured["body"]
        media = json.loads(_extract_field(body, "media").decode("utf-8"))
        self.assertEqual(media[0]["caption"], caption)
        self.assertEqual(media[0]["parse_mode"], "HTML")
        self.assertNotIn("&lt;b&gt;", media[0]["caption"])


if __name__ == "__main__":
    unittest.main()
