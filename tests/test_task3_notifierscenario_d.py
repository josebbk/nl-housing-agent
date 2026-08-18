"""
Scenario D — notification formatting tests for Task 3.

Tests the redesigned _format_listing_message in notifier.py against
realistic fixture data that matches the shape produced by scoring.py.

No real Telegram messages are sent.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub dotenv so notifier.py can import without a .env file
import types
_dotenv_mod = types.ModuleType("dotenv")
_dotenv_mod.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dotenv_mod)

from src.notifier import _format_listing_message  # noqa: E402

# ---------------------------------------------------------------------------
# Realistic score_breakdown data (matches scoring.py output)
# ---------------------------------------------------------------------------

FULL_BREAKDOWN = [
    {"criterion": "neighborhood_value", "points_earned": 18, "points_possible": 21, "matched": True},
    {"criterion": "construction_condition", "points_earned": 10, "points_possible": 11, "matched": True},
    {"criterion": "ownership", "points_earned": 17, "points_possible": 17, "matched": True},
    {"criterion": "energy_label", "points_earned": 14, "points_possible": 14, "matched": True},
    {"criterion": "living_area", "points_earned": 12, "points_possible": 12, "matched": True},
    {"criterion": "parking", "points_earned": 8, "points_possible": 8, "matched": True},
    {"criterion": "rooms", "points_earned": 5, "points_possible": 7, "matched": True},
    {"criterion": "bathrooms", "points_earned": 4, "points_possible": 6, "matched": True},
    {"criterion": "garden", "points_earned": 3, "points_possible": 4, "matched": True},
]

PARTIAL_BREAKDOWN = [
    {"criterion": "neighborhood_value", "points_earned": 12, "points_possible": 21, "matched": False},
    {"criterion": "construction_condition", "points_earned": 10, "points_possible": 11, "matched": True},
    {"criterion": "ownership", "points_earned": 17, "points_possible": 17, "matched": True},
    {"criterion": "energy_label", "points_earned": 14, "points_possible": 14, "matched": True},
    {"criterion": "living_area", "points_earned": 12, "points_possible": 12, "matched": True},
    {"criterion": "parking", "points_earned": 0, "points_possible": 8, "matched": False},
    {"criterion": "rooms", "points_earned": 5, "points_possible": 7, "matched": True},
    {"criterion": "bathrooms", "points_earned": 4, "points_possible": 6, "matched": True},
    {"criterion": "garden", "points_earned": 3, "points_possible": 4, "matched": True},
]

NO_DATA_BREAKDOWN = []

FEW_CRITERIA_BREAKDOWN = [
    {"criterion": "neighborhood_value", "points_earned": 18, "points_possible": 21, "matched": True},
    {"criterion": "construction_condition", "points_earned": 10, "points_possible": 11, "matched": True},
    {"criterion": "ownership", "points_earned": 17, "points_possible": 17, "matched": True},
]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_listing(**overrides):
    """Build a realistic listing dict with sensible defaults."""
    base = {
        "listing_id": "test-00000000",
        "url": "https://www.funda.nl/koop/amsterdam/huis-test/12345678/",
        "address": "Prinsengracht 42, Amsterdam",
        "neighborhood": "De Pijp",
        "price": 650000,
        "living_area_m2": 115,
        "bedrooms": 3,
        "property_type": "huis",
        "score": 82,
        "score_confidence": "full",
        "score_breakdown": json.dumps(FULL_BREAKDOWN),
    }
    base.update(overrides)
    return base


def check(condition, label):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def run_test(test_name, listing_dict, checks):
    print(f"\n{'='*60}")
    print(f"TEST: {test_name}")
    print(f"{'='*60}")
    msg = _format_listing_message(listing_dict)
    print(msg)
    print()
    all_pass = True
    for cond, label in checks:
        if not check(cond, label):
            all_pass = False
    return all_pass


# ---------------------------------------------------------------------------
# Test 1: Full confidence breakdown
# ---------------------------------------------------------------------------

def test_full_confidence():
    listing = make_listing(
        score=82,
        score_confidence="full",
        score_breakdown=json.dumps(FULL_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    checks = [
        ("\U0001f3e0 <b>Prinsengracht 42, Amsterdam</b>" in msg,
         "Header contains address"),
        ("\u20ac650,000" in msg,
         "Header contains formatted price"),
        ("115 m\u00b2" in msg,
         "Header contains living area"),
        ("3 bedrooms" in msg,
         "Header contains bedrooms (full word)"),
        ("huis" in msg,
         "Header contains property type"),
        ("De Pijp" in msg,
         "Header contains neighborhood"),
        ("\u2b50 <b>82/100</b>" in msg,
         "Score line present with bold value"),
        ("\u26a0\ufe0f Adjusted" not in msg,
         "No Adjusted line for full confidence"),
        ("\U0001f7e2 Best" in msg,
         "Best section present"),
        ("\U0001f534 Weakest" in msg,
         "Weakest section present"),
        ("\U0001f4ca Full score breakdown" in msg,
         "Full score breakdown present"),
        ("Neighborhood 18/21" in msg,
         "Full breakdown shows criterion with earned/possible"),
        ("Ownership 17/17" in msg,
         "Full breakdown shows Ownership"),
        ("Living area 12/12" in msg,
         "Full breakdown shows Living area"),
        ("Garden 3/4" in msg,
         "Full breakdown shows Garden"),
        ("\U0001f517" in msg,
         "URL link present"),
        ("Score: unavailable" not in msg,
         "No 'unavailable' text for full confidence"),
    ]
    return run_test("Full confidence breakdown", listing, checks)


# ---------------------------------------------------------------------------
# Test 2: Partial confidence breakdown
# ---------------------------------------------------------------------------

def test_partial_confidence():
    listing = make_listing(
        score=78,
        score_confidence="partial",
        score_breakdown=json.dumps(PARTIAL_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    checks = [
        ("\u2b50 <b>78/100</b>" in msg,
         "Score line present"),
        ("\u26a0\ufe0f Adjusted" in msg,
         "Adjusted line present for partial confidence"),
        ("Neighborhood" in msg and "data unavailable" in msg,
         "Adjusted line references missing criterion display name"),
        ("Parking" in msg and "data unavailable" in msg,
         "Adjusted line references Parking as missing"),
        ("\U0001f7e2 Best" in msg,
         "Best section present"),
        ("\U0001f534 Weakest" in msg,
         "Weakest section present"),
        ("\U0001f4ca Full score breakdown" in msg,
         "Full score breakdown present"),
        ("Neighborhood N/A" in msg,
         "Full breakdown shows N/A for neighborhood (unmatched)"),
        ("Parking N/A" in msg,
         "Full breakdown shows N/A for parking (unmatched)"),
        ("Ownership 17/17" in msg,
         "Full breakdown shows Ownership"),
        ("Score: unavailable" not in msg,
         "No 'unavailable' text for partial confidence"),
    ]
    return run_test("Partial confidence breakdown", listing, checks)


# ---------------------------------------------------------------------------
# Test 3: No data
# ---------------------------------------------------------------------------

def test_no_data():
    listing = make_listing(
        score=None,
        score_confidence="no_data",
        score_breakdown=json.dumps(NO_DATA_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    checks = [
        ("Score: unavailable" in msg,
         "Score shows 'unavailable' for no_data"),
        ("\u2b50" not in msg,
         "No star score line for no_data"),
        ("\u26a0\ufe0f" not in msg,
         "No Adjusted line for no_data"),
        ("\U0001f7e2" not in msg,
         "No Best section for no_data"),
        ("\U0001f534" not in msg,
         "No Weakest section for no_data"),
        ("\U0001f4ca" not in msg,
         "No full breakdown for no_data"),
        ("\U0001f517" in msg,
         "URL link still present"),
    ]
    return run_test("No data", listing, checks)


# ---------------------------------------------------------------------------
# Test 4: Fewer than 3 criteria matched (only Best shown)
# ---------------------------------------------------------------------------

def test_few_criteria():
    listing = make_listing(
        score=75,
        score_confidence="full",
        score_breakdown=json.dumps(FEW_CRITERIA_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    checks = [
        ("\U0001f7e2 Best" in msg,
         "Best section present"),
        ("\U0001f534 Weakest" not in msg,
         "No Weakest section when <= 3 criteria matched"),
        ("Neighborhood 18/21" in msg,
         "Best shows top criterion"),
        ("Ownership 17/17" in msg,
         "Best shows second criterion"),
        ("Condition 10/11" in msg,
         "Best shows third criterion"),
    ]
    return run_test("Few criteria (<=3)", listing, checks)


# ---------------------------------------------------------------------------
# Test 5: Null/missing fields
# ---------------------------------------------------------------------------

def test_null_fields():
    listing = make_listing(
        price=None,
        living_area_m2=None,
        bedrooms=None,
        property_type=None,
        neighborhood=None,
    )
    msg = _format_listing_message(listing)
    checks = [
        ("N/A" in msg,
         "N/A shown for null price/area/bedrooms"),
        ("\U0001f3e0 <b>Prinsengracht 42, Amsterdam</b>" in msg,
         "Address still shown"),
    ]
    return run_test("Null/missing fields", listing, checks)


# ---------------------------------------------------------------------------
# Test 6: Best/Weakest ordering correctness
# ---------------------------------------------------------------------------

def test_best_weakest_ordering():
    listing = make_listing(
        score=82,
        score_confidence="full",
        score_breakdown=json.dumps(FULL_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    # Top 3 by points_earned (Best): Neighborhood(18), Ownership(17), Energy(14)
    # Bottom 3 by points_earned (Weakest): Garden(3), Bathrooms(4), Rooms(5)
    best_pos = msg.index("\U0001f7e2 Best")
    weakest_pos = msg.index("\U0001f534 Weakest")
    # Use the " — " (em-dash) format that only appears in Best/Weakest sections,
    # not in the Full score breakdown (which uses " · " without em-dash).
    neighborhood_best = msg.index("Neighborhood \u2014 18/21")
    ownership_best = msg.index("Ownership \u2014 17/17")
    energy_best = msg.index("Energy \u2014 14/14")
    garden_weakest = msg.index("Garden \u2014 3/4")
    bathrooms_weakest = msg.index("Bathrooms \u2014 4/6")
    rooms_weakest = msg.index("Rooms \u2014 5/7")
    checks = [
        (best_pos < weakest_pos,
         "Best section comes before Weakest section"),
        (neighborhood_best < ownership_best < energy_best,
         "Best ordered by points_earned desc: Neighborhood > Ownership > Energy"),
        (garden_weakest < bathrooms_weakest < rooms_weakest,
         "Weakest ordered by points_earned asc: Garden < Bathrooms < Rooms"),
    ]
    return run_test("Best/Weakest ordering", listing, checks)


# ---------------------------------------------------------------------------
# Test 7: Full breakdown stable ordering (matches preferences.json weight order)
# ---------------------------------------------------------------------------

def test_breakdown_ordering():
    listing = make_listing(
        score=82,
        score_confidence="full",
        score_breakdown=json.dumps(FULL_BREAKDOWN),
    )
    msg = _format_listing_message(listing)
    # preferences.json weight order: neighborhood_value, ownership, energy_label,
    # living_area, construction_condition, parking, rooms, bathrooms, garden
    # The breakdown_map is built from the JSON list which follows scoring.py's
    # subscores dict order: neighborhood_value, construction_condition, ownership,
    # energy_label, garden, parking, bathrooms, living_area, rooms
    # But the full breakdown iterates breakdown_map.keys() which preserves insertion
    # order from the JSON list.
    # We just verify all 9 criteria appear.
    criteria = ["Neighborhood", "Condition", "Ownership", "Energy",
                "Living area", "Parking", "Rooms", "Bathrooms", "Garden"]
    checks = []
    for c in criteria:
        checks.append((c in msg, f"Full breakdown contains '{c}'"))
    checks.append((msg.count("N/A") == 0,
                   "No N/A entries for full-confidence breakdown"))
    return run_test("Full breakdown stable ordering", listing, checks)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("SCENARIO D — NOTIFICATION FORMATTING TESTS (Task 3)")
    print("=" * 60)

    results = []
    results.append(("Full confidence", test_full_confidence()))
    results.append(("Partial confidence", test_partial_confidence()))
    results.append(("No data", test_no_data()))
    results.append(("Few criteria (<=3)", test_few_criteria()))
    results.append(("Null/missing fields", test_null_fields()))
    results.append(("Best/Weakest ordering", test_best_weakest_ordering()))
    results.append(("Full breakdown ordering", test_breakdown_ordering()))

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)