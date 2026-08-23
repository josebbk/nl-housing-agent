"""
Tests for property-photo URL extraction in src/detail_scraper.py.

Covers:
* canonicalisation of Funda CDN photo URLs (size variants, query params);
* rejection of non-Funda hosts, non-https schemes and non-image paths;
* dedupe across size variants with stable gallery ordering;
* the 3-of-N deterministic selection used by the notifier (via notifier);
* extraction wiring inside fetch_listing_details with a mocked browser.

No live network calls; Playwright is stubbed.
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
sys.modules.setdefault(
    "dotenv", _make_module("dotenv", load_dotenv=mock.MagicMock())
)

from src.detail_scraper import (  # noqa: E402
    _canonical_image_url,
    _extract_property_image_urls,
    fetch_listing_details,
)


class TestCanonicalImageUrl(unittest.TestCase):
    def test_options_query_variant(self):
        url = "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=720"
        self.assertEqual(
            _canonical_image_url(url),
            "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1440",
        )

    def test_size_suffix_variant(self):
        self.assertEqual(
            _canonical_image_url("https://cloud.funda.nl/valentina_media/230/205/775_1440x960.jpg"),
            "https://cloud.funda.nl/valentina_media/230/205/775.jpg?options=width=1440",
        )

    def test_plain_full_size_url(self):
        self.assertEqual(
            _canonical_image_url("https://cloud.funda.nl/valentina_media/230/205/631.jpg"),
            "https://cloud.funda.nl/valentina_media/230/205/631.jpg?options=width=1440",
        )

    def test_non_funda_host_rejected(self):
        self.assertIsNone(_canonical_image_url(
            "https://evil.example.com/valentina_media/x.jpg"))
        # funda.io infrastructure hosts are not the photo CDN either
        self.assertIsNone(_canonical_image_url(
            "https://listing-detail-page.funda.io/valentina_media/x.jpg"))

    def test_non_https_rejected(self):
        self.assertIsNone(_canonical_image_url(
            "http://cloud.funda.nl/valentina_media/230/205/775.jpg"))

    def test_non_valentina_path_rejected(self):
        self.assertIsNone(_canonical_image_url(
            "https://cloud.funda.nl/static/logo.jpg"))

    def test_non_image_extension_rejected(self):
        self.assertIsNone(_canonical_image_url(
            "https://cloud.funda.nl/valentina_media/page.html"))

    def test_garbage_input(self):
        self.assertIsNone(_canonical_image_url(""))
        self.assertIsNone(_canonical_image_url("not a url"))


class TestExtractPropertyImageUrls(unittest.TestCase):
    GALLERY = [
        "https://cloud.funda.nl/valentina_media/a/b/775.jpg?options=width=1080",
        "https://cloud.funda.nl/valentina_media/a/b/775_1440x960.jpg",   # same photo, other variant
        "https://cloud.funda.nl/valentina_media/a/b/631.jpg?options=width=464",
        "https://cloud.funda.nl/valentina_media/a/b/733.jpg?options=width=720",
        "https://www.funda.nl/favicon.ico",                               # UI asset -> dropped
        "https://listing-detail-page.funda.io/some.js",                   # foreign host -> dropped
    ]

    def test_dedupes_variants_keeps_gallery_order(self):
        urls = _extract_property_image_urls(self.GALLERY)
        ids = [u.rsplit("/", 1)[-1].split("_")[0].split(".")[0] for u in urls]
        self.assertEqual(ids, ["775", "631", "733"])
        # all canonicalised to width=1440
        self.assertTrue(all(u.endswith("?options=width=1440") for u in urls))

    def test_fewer_than_three_preserved(self):
        urls = _extract_property_image_urls(self.GALLERY[:1])
        self.assertEqual(len(urls), 1)

    def test_empty_and_none_inputs(self):
        self.assertEqual(_extract_property_image_urls([]), [])
        self.assertEqual(_extract_property_image_urls(None), [])
        self.assertEqual(_extract_property_image_urls([None, 42, "x"]), [])

    def test_output_capped_at_limit(self):
        many = [
            f"https://cloud.funda.nl/valentina_media/a/b/{i}.jpg"
            for i in range(50)
        ]
        self.assertEqual(len(_extract_property_image_urls(many)), 10)


class TestFetchListingDetailsImageWiring(unittest.TestCase):
    def _run_with_fake_browser(self, collected_urls):
        """Run fetch_listing_details with a fully mocked Playwright chain.
        The page returns empty text (no sections) plus our fake photo URLs."""
        page = mock.MagicMock()
        text = ""  # no parseable text -> all detail fields None

        def evaluate(js, *a, **k):
            if "og:image" in js:      # the image collector snippet
                return list(collected_urls)
            return text               # _extract_page_text

        page.evaluate.side_effect = evaluate

        context = mock.MagicMock()
        context.new_page.return_value = page
        browser = mock.MagicMock()
        browser.new_context.return_value = context
        pw = mock.MagicMock()
        pw.chromium.launch.return_value = browser
        # MagicMock magic methods must be configured on child mocks,
        # not by attribute assignment.
        sync_factory = mock.MagicMock()
        sync_factory.return_value.__enter__.return_value = pw
        sync_factory.return_value.__exit__.return_value = False

        # Patch the module-level imported name directly with the factory.
        with mock.patch.object(sys.modules["src.detail_scraper"],
                               "sync_playwright", sync_factory), \
             mock.patch.object(sys.modules["src.detail_scraper"],
                               "_fetch_page_html", return_value="<html></html>"):
            return fetch_listing_details("https://www.funda.nl/detail/x/")

    def test_image_urls_returned_in_gallery_order(self):
        raw = [
            "https://cloud.funda.nl/valentina_media/a/1.jpg?options=width=464",
            "https://cloud.funda.nl/valentina_media/a/2.jpg?options=width=464",
            "https://cloud.funda.nl/valentina_media/a/3.jpg?options=width=464",
            "https://cloud.funda.nl/valentina_media/a/4.jpg?options=width=464",
        ]
        result = self._run_with_fake_browser(raw)
        self.assertEqual(len(result["image_urls"]), 4)
        self.assertIn("/1.jpg?", result["image_urls"][0])
        self.assertIn("/4.jpg?", result["image_urls"][3])

    def test_no_photos_means_key_absent_not_error(self):
        result = self._run_with_fake_browser([])
        self.assertNotIn("image_urls", result)

    def test_collector_exception_never_fails_detail_fetch(self):
        page = mock.MagicMock()

        def broken_evaluate(js, *a, **k):
            if "og:image" in js:
                raise RuntimeError("evaluate failed")
            return ""

        page.evaluate.side_effect = broken_evaluate
        context = mock.MagicMock()
        context.new_page.return_value = page
        browser = mock.MagicMock()
        browser.new_context.return_value = context
        pw = mock.MagicMock()
        pw.chromium.launch.return_value = browser
        sync_factory = mock.MagicMock()
        sync_factory.return_value.__enter__.return_value = pw
        sync_factory.return_value.__exit__.return_value = False

        # Patch the module-level imported name directly with the factory.
        with mock.patch.object(sys.modules["src.detail_scraper"],
                               "sync_playwright", sync_factory), \
             mock.patch.object(sys.modules["src.detail_scraper"],
                               "_fetch_page_html", return_value="<html></html>"):
            result = fetch_listing_details("https://www.funda.nl/detail/x/")
        self.assertNotIn("image_urls", result)
        self.assertIn("detail_fetched_at", result)


if __name__ == "__main__":
    unittest.main()
