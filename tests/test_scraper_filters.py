"""Tests for the Funda Results-page search-filter URL construction and the
``FilterConfig`` search-filter model (Parts 1-2 of the filters task).

Covers:

* ``build_search_url()`` reproducing every parameter of the authoritative
  Funda search URL (regression test for the whole filter pipeline);
* ``radius_km`` emitting a separate ``radius_search`` param, never an
  embedded ``selected_area`` JSON array;
* energy-label percent-encoding (``+`` -> ``%2B``) with exact order
  preservation, including single-item lists;
* ``construction_periods`` mapping human-readable keys to Funda codes via
  ``CONSTRUCTION_PERIOD_MAP``, and ``FilterConfig`` rejecting unknown keys;
* ``garden_size_min`` without ``garden=True`` raising ``ValueError``;
* ``FilterConfig.from_file()`` rejecting the removed
  ``energy_label_min``/``energy_label_max`` keys;
* ``bedrooms_min`` defaulting to 3 and always emitted as ``bedrooms=3-``;
* storage/main no longer touching ``energy_label_min``/``energy_label_max``.

Run standalone (``python tests/test_scraper_filters.py``) or via pytest
(``pytest tests/test_scraper_filters.py``). No real network calls.
"""

import json
import sys
import tempfile
import traceback
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import CONSTRUCTION_PERIOD_MAP, DEFAULT_FILTERS, FilterConfig  # noqa: E402
from src.scraper import build_search_url  # noqa: E402
from src.storage import fetch_unnotified_matching_listings, init_db  # noqa: E402

AUTHORITATIVE_URL = (
    "https://www.funda.nl/zoeken/koop?selected_area=amsterdam&radius_search=10"
    "&price=550000-750000&availability=available&floor_area=100-&bedrooms=3-"
    "&energy_label=A%2B%2B%2B%2B,A%2B%2B%2B,A%2B%2B,A%2B,A,B,C,D,A%2B%2B%2B%2B%2B"
    "&exterior_space_type=garden&exterior_space_garden_size=70-"
    "&construction_period=from_1981_to_1990,from_1991_to_2000,from_2001_to_2010,"
    "from_2011_to_2020,after_2020,from_1971_to_1980"
    "&sort=publish_date_utc_desc"
)


def _qs(url: str) -> dict:
    """Parse a URL's query string into a dict of decoded value lists."""
    return urllib.parse.parse_qs(
        urllib.parse.urlsplit(url).query, keep_blank_values=True
    )


def _build_url_from_defaults() -> str:
    """Build the search URL from the committed config/filters.json defaults."""
    f = FilterConfig.from_file()
    periods = (
        [CONSTRUCTION_PERIOD_MAP[k] for k in f.construction_periods]
        if f.construction_periods is not None
        else None
    )
    return build_search_url(
        area="amsterdam",
        offering_type="koop",
        price_min=f.price_min,
        price_max=f.price_max,
        floor_area_min=f.living_area_min,
        floor_area_max=f.living_area_max,
        bedrooms_min=f.bedrooms_min,
        bedrooms_max=f.bedrooms_max,
        rooms_min=f.rooms_min,
        rooms_max=f.rooms_max,
        radius_km=f.radius_km,
        construction_type=f.construction_type,
        energy_labels=f.energy_labels,
        construction_periods=periods,
        garden=f.garden,
        garden_size_min=f.garden_size_min,
        availability=f.availability,
        sort=f.sort,
        page=1,
    )


def _write_filters_file(raw: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "filters.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Core regression: reproduce the authoritative URL
# ---------------------------------------------------------------------------

def test_build_search_url_reproduces_authoritative_url():
    gen = _qs(_build_url_from_defaults())
    auth = _qs(AUTHORITATIVE_URL)
    # energy_label order is significant by design; other multi-value params
    # (construction_period) are compared order-insensitively.
    for key in auth:
        assert key in gen, f"missing key {key!r}"
        if key == "energy_label":
            assert gen[key] == auth[key], f"{key}: {gen[key]} != {auth[key]}"
        else:
            g = set(x for v in gen[key] for x in v.split(","))
            a = set(x for v in auth[key] for x in v.split(","))
            assert g == a, f"{key}: {g} != {a}"


# ---------------------------------------------------------------------------
# 2. Radius is its own param, never an embedded selected_area array
# ---------------------------------------------------------------------------

def test_radius_is_separate_param_not_embedded():
    url = build_search_url(area="amsterdam", radius_km=10, page=1)
    q = _qs(url)
    assert q["selected_area"] == ["amsterdam"]
    assert q["radius_search"] == ["10"]
    assert '["amsterdam' not in url
    assert "[%22" not in url


# ---------------------------------------------------------------------------
# 3. Energy label encoding and order preservation
# ---------------------------------------------------------------------------

def test_energy_labels_encode_plus_and_preserve_order():
    url = build_search_url(energy_labels=["A+++++", "A", "C"], page=1)
    assert "energy_label=A%2B%2B%2B%2B%2B,A,C" in url
    assert _qs(url)["energy_label"] == ["A+++++,A,C"]


def test_energy_label_single_item():
    url = build_search_url(energy_labels=["A+"], page=1)
    assert "energy_label=A%2B" in url
    assert _qs(url)["energy_label"] == ["A+"]


def test_energy_labels_default_order_is_exact():
    f = FilterConfig.from_file()
    assert f.energy_labels == ["A++++", "A+++", "A++", "A+", "A", "B", "C", "D", "A+++++"]


# ---------------------------------------------------------------------------
# 4. Construction periods mapping and validation
# ---------------------------------------------------------------------------

def test_construction_periods_map_to_codes():
    f = FilterConfig.from_file()
    mapped = [CONSTRUCTION_PERIOD_MAP[k] for k in f.construction_periods]
    assert set(mapped) == set(CONSTRUCTION_PERIOD_MAP.values())
    url = build_search_url(construction_periods=mapped, page=1)
    assert set(_qs(url)["construction_period"][0].split(",")) == set(
        CONSTRUCTION_PERIOD_MAP.values()
    )


def test_invalid_construction_period_raises():
    try:
        FilterConfig(
            price_min=1,
            price_max=2,
            bedrooms_min=3,
            living_area_min=4,
            construction_periods=["1991-2000", "nonsense"],
        )
    except ValueError as exc:
        assert "nonsense" in str(exc)
        assert "Valid options" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid construction_periods")


# ---------------------------------------------------------------------------
# 5. garden_size_min requires garden=True
# ---------------------------------------------------------------------------

def test_garden_size_without_garden_raises():
    try:
        FilterConfig(
            price_min=1, price_max=2, bedrooms_min=3, living_area_min=4,
            garden_size_min=70,
        )
    except ValueError as exc:
        assert "garden" in str(exc)
    else:
        raise AssertionError("expected ValueError for garden_size_min without garden")


def test_garden_without_size_is_allowed():
    f = FilterConfig(price_min=1, price_max=2, bedrooms_min=3, living_area_min=4, garden=True)
    assert f.garden_size_min is None


# ---------------------------------------------------------------------------
# 6. Removed energy_label_min/max keys are rejected, not ignored
# ---------------------------------------------------------------------------

def test_old_energy_label_keys_rejected():
    old = {
        "required": {
            "price_min": 550000,
            "price_max": 750000,
            "bedrooms_min": 3,
            "living_area_min": 100,
        },
        "optional": {"energy_label_min": "A", "energy_label_max": "B"},
    }
    try:
        FilterConfig.from_file(_write_filters_file(old))
    except ValueError as exc:
        assert "energy_label_min" in str(exc)
    else:
        raise AssertionError("expected ValueError for old energy_label_min/max keys")


# ---------------------------------------------------------------------------
# 7. bedrooms_min default and bedroom filter always present
# ---------------------------------------------------------------------------

def test_bedrooms_min_defaults_to_three_and_always_present():
    assert DEFAULT_FILTERS.bedrooms_min == 3
    assert FilterConfig.from_file().bedrooms_min == 3
    # full default URL
    assert _qs(_build_url_from_defaults())["bedrooms"] == ["3-"]
    # with several optional filters set, the bedroom filter must not disappear
    url = build_search_url(
        bedrooms_min=3,
        radius_km=10,
        garden=True,
        garden_size_min=70,
        energy_labels=["A", "B"],
        construction_periods=["after_2020"],
        availability="available",
        sort="publish_date_utc_desc",
        page=1,
    )
    assert _qs(url)["bedrooms"] == ["3-"]


# ---------------------------------------------------------------------------
# 8. storage/main no longer reference energy_label_min/max
# ---------------------------------------------------------------------------

def test_storage_and_main_no_energy_label_attribute_access():
    for rel in ("src/storage.py", "src/main.py"):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "energy_label_min" not in text, f"{rel} still references energy_label_min"
        assert "energy_label_max" not in text, f"{rel} still references energy_label_max"


def test_fetch_unnotified_matching_listings_works_with_new_config():
    db = Path(tempfile.mkdtemp()) / "test.db"
    init_db(db)
    result = fetch_unnotified_matching_listings(db, filters=FilterConfig.from_file())
    assert result == []


# ---------------------------------------------------------------------------
# Standalone runner (also pytest-discoverable via test_* functions above)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
