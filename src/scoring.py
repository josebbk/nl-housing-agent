"""
Preference-based scoring for Funda listings.

Scores a listing's detail data against user preferences loaded from
config/preferences.json. Each criterion contributes a weighted subscore,
and the final score is renormalized when data is missing for some criteria.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .config import FilterConfig

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PREFERENCES_PATH = _PROJECT_ROOT / "config" / "preferences.json"


@dataclass
class ScoreResult:
    """Result of scoring a listing against preferences."""
    score: int  # 0-100, or None if no data available
    breakdown: list[dict]  # list of {criterion, points_earned, points_possible, matched}
    confidence: str  # "full" | "partial" | "partial_major_missing" | "no_data"
    missing_criteria: list[str]  # list of criterion names with no data

    def to_dict(self) -> dict:
        return asdict(self)


def _load_preferences(path: Path | str | None = None) -> dict:
    """Load preferences from config/preferences.json."""
    path = path or _PREFERENCES_PATH
    with open(path) as f:
        prefs = json.load(f)

    weights = prefs.get("weights", {})
    total = sum(weights.values())
    if total != 100:
        raise ValueError(
            f"config/preferences.json weights sum to {total}, but must sum to "
            f"exactly 100. Check config/preferences.json's \"weights\" object "
            f"for a typo, duplicate, or missing entry, then re-run."
        )

    return prefs


def _score_neighborhood_value(detail: dict, preferences: dict) -> float | None:
    """Score neighborhood value based on price/m² ratio vs neighborhood average.

    Returns a float in [0, 1] or None if neighborhood_avg_price_m2 is missing.

    A listing priced at 80% of the neighborhood avg (good_ratio) scores 1.0.
    A listing priced at 120% of the neighborhood avg (bad_ratio) scores 0.0.
    Linear interpolation between these thresholds.
    """
    price = detail.get("price")
    living_area = detail.get("living_area_m2")
    neighborhood_avg = detail.get("neighborhood_avg_price_m2")

    if not all([price, living_area, neighborhood_avg]):
        return None

    thresholds = preferences.get("neighborhood_value_thresholds", {})
    good_ratio = thresholds.get("good_ratio", 0.8)
    bad_ratio = thresholds.get("bad_ratio", 1.2)

    listing_price_per_m2 = price / living_area
    ratio = listing_price_per_m2 / neighborhood_avg

    if ratio <= good_ratio:
        return 1.0
    elif ratio >= bad_ratio:
        return 0.0
    else:
        # Linear interpolation between good and bad
        return round(1.0 - (ratio - good_ratio) / (bad_ratio - good_ratio), 4)


def _score_construction(detail: dict, preferences: dict) -> float | None:
    """Score construction condition based on year built and insulation.

    Returns a float in [0, 1] or None if **neither** year_built nor
    insulation_score is available.

    Formulas:
      year_score:
        Linear interpolation from 0.0 (built in 1950 or earlier) to 1.0
        (built in 2025 or later). Clamped to [0, 1].
        year_score = min(max((year_built - 1950) / (2025 - 1950), 0), 1)

      insulation_score:
        Already computed by detail_scraper._compute_insulation_score() from
        the raw insulation text. A float in [0, 1].

      final score:
        Always averages both year_score and insulation_score together
        when both are present: (year_score + insulation_score) / 2.
        When only year_built is available: year_score.
        When only insulation_score is available: insulation_score.
    """
    year_built = detail.get("year_built")
    insulation_score = detail.get("insulation_score")

    if year_built is None and insulation_score is None:
        return None

    year_score = None
    insulation_comp_score = None

    if year_built is not None:
        # Newer buildings score higher.
        # Bounds sourced from preferences.json, fall back to 1950/2025.
        bounds = preferences.get("construction_year_range", {})
        floor = bounds.get("floor", 1950)
        cap = bounds.get("cap", 2025)
        year_score = min(max((year_built - floor) / (cap - floor), 0), 1)

    if insulation_score is not None:
        insulation_comp_score = insulation_score

    if year_score is None:
        return round(insulation_comp_score, 4)
    if insulation_comp_score is None:
        return round(year_score, 4)

    return round(year_score * 0.35 + insulation_comp_score * 0.65, 4)


def _score_ownership(detail: dict) -> float | None:
    """Score ownership type as 3 discrete tiers per product.md §12a.

    Returns a float in [0, 1] or None if ownership_type is missing.

    Tiers:
      full ownership          -> 1.0
      erfpacht, no ongoing    -> 0.7
          annual canon
          (paid off /
           eeuwigdurend
           afgekocht)
      erfpacht, with ongoing  -> 0.3
          annual canon
    """
    ownership_type = detail.get("ownership_type")
    if ownership_type is None:
        return None

    if ownership_type == "full":
        return 1.0

    if ownership_type == "erfpacht":
        canon = detail.get("erfpacht_canon_annual")
        if canon is None or canon <= 0:
            return 0.8
        return round(0.8 * (1 - min(canon, 1000) / 1000), 4)

    return None


def _score_energy_label(detail: dict, preferences: dict) -> float | None:
    """Score energy label.

    Returns a float in [0, 1] or None if energy_label is missing.

    Uses the energy_label_scale from preferences to map labels to scores.
    """
    label = detail.get("energy_label")
    if not label:
        return None

    scale = preferences.get("energy_label_scale", ["G", "F", "E", "D", "C", "B", "A", "A+", "A++", "A+++", "A++++"])
    label_normalized = label.strip().upper()

    # Try exact match first
    if label_normalized in scale:
        idx = scale.index(label_normalized)
        return round(math.sqrt(idx / (len(scale) - 1)), 4)

    # Try case-insensitive match
    for i, s in enumerate(scale):
        if s.lower() == label.lower():
            return round(math.sqrt(i / (len(scale) - 1)), 4)

    # Label not in scale — return None (unrecognized value)
    logger.warning("Unknown energy label '%s', using midpoint score", label)
    return None


def _score_garden(detail: dict, preferences: dict) -> float | None:
    """Score garden quality based on presence, size, and orientation.

    Returns a float in [0, 1] or None if garden_present is not available
    (the "Tuin" field was not found on the page).

    A listing where garden_present is explicitly False scores 0.0
    (we know there is no garden).

    Formula (when garden_present is True):
      base_score = 0.5  (baseline for having any garden)

      size_bonus = min(garden_size_m2 / garden_size_cap_m2, 1.0) * 0.3
        garden_size_cap_m2 defaults to 50 (from preferences).
        A garden of 50 m² or more gets the full 0.3 bonus.
        If garden_size_m2 is missing, size_bonus = 0.

      orientation_bonus = 0.2 if garden_orientation matches any entry in
        garden_orientation_bonus (defaults to ["zuiden", "westen"]), else 0.

      final score = min(base_score + size_bonus + orientation_bonus, 1.0)

    Maximum possible score: 0.5 + 0.3 + 0.2 = 1.0
    """
    garden_present = detail.get("garden_present")
    if garden_present is None:
        return None

    if not garden_present:
        return 0.0

    score = 0.5  # Base score for having a garden

    # Size bonus (up to 0.3)
    size_cap = preferences.get("garden_size_cap_m2", 50)
    garden_size = detail.get("garden_size_m2")
    if garden_size is not None:
        score += min(garden_size / size_cap, 1.0) * 0.3

    # Orientation bonus (up to 0.2)
    orientation_bonus = preferences.get("garden_orientation_bonus", ["zuiden", "westen"])
    orientation = detail.get("garden_orientation")
    if orientation:
        for bonus_dir in orientation_bonus:
            if bonus_dir.lower() in orientation.lower():
                score += 0.2
                break

    return round(min(score, 1.0), 4)


def _score_parking(detail: dict) -> float | None:
    """Score parking type.

    Returns a float in [0, 1] or None if parking_type data is unavailable
    (the "Soort parkeergelegenheid" field was not found on the page).

    Scoring:
      "private" (eigen terrein) = 1.0
      "carport"               = 0.7
      "paid" (betaald)        = 0.5
      "public" (openbaar)     = 0.3
      unknown type            = None (data unavailable / unrecognized)

    IMPORTANT: None means "data unavailable" (field not present on the page),
    NOT "no parking". A listing with no parking field is excluded from the
    weighted average via renormalization, not penalized with a zero score.
    """
    parking_type = detail.get("parking_type")
    if parking_type is None:
        return None

    # Known limitation: parking_type can be stored as a combined
    # "TypeA + TypeB" string. Use only the first segment for scoring
    # until the underlying extraction is fixed to store multiple
    # values properly. See docs/site-notes/funda.md.
    primary = parking_type.split("+")[0].strip().lower()

    # Ordered from best to worst. Each tuple is (score, [substrings]).
    # Covers both the extractor's short codes and raw Dutch phrases
    # the extractor doesn't classify.
    tiers = [
        (1.0, ["parkeergarage", "afgesloten terrein"]),
        (0.9, ["private", "eigen terrein"]),
        (0.6, ["parkeervergunningen"]),
        (0.4, ["paid", "betaald"]),
        (0.2, ["public", "openbaar"]),
    ]
    for score, keywords in tiers:
        if any(kw in primary for kw in keywords):
            return score

    return None


def _score_living_area(
    detail: dict, filter_config: FilterConfig, preferences: dict
) -> float | None:
    """Score living area based on the configured minimum filter.

    Returns a float in [0, 1] or None if no living-area filter is configured.

    Linear scale between floor (0.0) and cap (1.0), clamped to [0, 1].
      - floor = filter_config.living_area_min
      - cap = preferences["living_area_thresholds"]["cap"] (from preferences.json)
        falls back to floor + 100 if absent.
    """
    living_area = detail.get("living_area_m2")
    if living_area is None:
        return None

    floor = filter_config.living_area_min
    cap = preferences.get("living_area_thresholds", {}).get("cap")
    if cap is None:
        cap = floor + 100

    score = (living_area - floor) / (cap - floor)
    return round(max(0.0, min(1.0, score)), 4)


def _score_rooms(
    detail: dict, filter_config: FilterConfig, preferences: dict
) -> float | None:
    """Score number of rooms based on the configured bedrooms filter.

    Returns a float in [0, 1] or None if rooms data is missing.

    floor = filter_config.bedrooms_min (default 1 if no filter configured).
    cap = preferences["rooms_thresholds"]["cap"] (from preferences.json),
        falls back to max(8, floor + 4) if absent.

    Linear scale between floor (0.0) and cap (1.0), clamped to [0, 1].
    """
    rooms = detail.get("rooms")
    if rooms is None:
        return None

    floor = filter_config.bedrooms_min
    cap = preferences.get("rooms_thresholds", {}).get("cap")
    if cap is None:
        cap = max(8, floor + 4)

    score = (rooms - floor) / (cap - floor)
    return round(max(0.0, min(1.0, score)), 4)


def _score_garage(detail: dict) -> float | None:
    """Score garage type.

    Returns a float in [0, 1], or None if garage_type is an
    unrecognized (present but unmapped) value.

    Absence of a garage is a CONFIRMED negative (not missing data):
    Funda's page structure omits the entire "Garage" section when a
    listing genuinely has no garage, so garage_type is reliably None
    both when there truly is no garage AND when the field could not
    be parsed. This function treats garage_type is None as an
    explicit 0.0 (no garage), matching the same pattern already used
    for garden_present is False in _score_garden.

    Known limitation: garage_type can be stored as a combined
    "TypeA + TypeB" string. This function uses only the first type
    before "+" for scoring until the underlying extraction is fixed
    to store multiple values properly. See docs/site-notes/funda.md.
    """
    garage_type = detail.get("garage_type")
    if garage_type is None:
        return 0.0  # confirmed no garage — see docstring

    primary = garage_type.split("+")[0].strip().lower()

    # Ordered best to worst, based on convenience/permanence.
    tiers = [
        (1.0, ["inpandige garage"]),
        (0.95, ["aangebouwde garage"]),
        (0.9, ["parkeerkelder"]),
        (0.85, ["garage + carport"]),  # rare pre-split literal match
        (0.75, ["vrijstaande garage"]),
        (0.6, ["garagebox"]),
        (0.5, ["souterrain"]),
        (0.4, ["carport"]),
        (0.3, ["parkeerplaats"]),
        (0.1, ["garage mogelijk"]),
    ]
    for score, keywords in tiers:
        if any(kw in primary for kw in keywords):
            return score

    return None  # unrecognized value (e.g. "Elk soort garage") — missing


def _score_plot_size(detail: dict, preferences: dict) -> float | None:
    """Score plot size (m²). "More is better", linear, capped.

    Returns a float in [0, 1], or None if plot_size_m2 is unavailable.
    plot_size_m2 being None is treated as MISSING data, not a
    negative — we cannot reliably distinguish "apartment with
    genuinely no private plot" from "a plot exists but this field
    failed to parse" using this field alone, so it is excluded from
    renormalization rather than penalized.

    floor = 0 (a plot size of 0 would represent the worst case)
    cap = preferences["plot_size_thresholds"]["cap"], default 200
    if the key is absent from config/preferences.json.
    """
    plot_size = detail.get("plot_size_m2")
    if plot_size is None:
        return None

    cap = preferences.get("plot_size_thresholds", {}).get("cap", 200)
    score = plot_size / cap
    return round(max(0.0, min(1.0, score)), 4)


def _score_balcony(detail: dict) -> float:
    """Score balcony presence. Binary.

    Returns 1.0 if a balcony is present, 0.0 otherwise.

    Unlike most other criteria, this NEVER returns None. Per the
    confirmed field-location ground truth, the "Balkon/dakterras"
    field only appears on the page at all when a balcony genuinely
    exists — absence is a reliable, confirmed negative (no balcony),
    not missing/uncertain data. This mirrors the garden_present /
    garage_type pattern.
    """
    return 1.0 if detail.get("balcony_present") else 0.0


def _score_heating(detail: dict) -> float | None:
    """Score heating type by efficiency/future-proofing.

    Returns a float in [0, 1], or None if heating_type is missing or
    unrecognized. Unlike garage/balcony, every home has SOME form of
    heating — if this field is None, that is much more likely a
    parsing miss than a genuine absence of heating, so it is treated
    as MISSING (excluded from renormalization), not as a negative.
    """
    heating_type = detail.get("heating_type")
    if heating_type is None:
        return None

    scores = {
        "heat_pump": 1.0,
        "district": 0.6,
        "gas_boiler": 0.3,
    }
    return scores.get(heating_type)  # None if not a recognized key


def score_listing(detail: dict, preferences: dict | None = None,
                  filter_config: FilterConfig | None = None) -> ScoreResult:
    """Score a listing against preferences.

    Parameters
    ----------
    detail : dict
        Detail page data from fetch_listing_details(), merged with
        card-scraped data (price, living_area_m2, bedrooms, etc.).
    preferences : dict or None
        Loaded preferences from config/preferences.json. If None, loads
        from the default path.
    filter_config : FilterConfig or None
        Current filter configuration. If None, loads from
        config/filters.json.

    Returns
    -------
    ScoreResult
        score: int 0-100, or None if no data available
        breakdown: list of criterion detail dicts
        confidence: "full" | "partial" | "partial_major_missing" | "no_data"
        missing_criteria: list of criterion names with no data
    """
    if preferences is None:
        preferences = _load_preferences()

    if filter_config is None:
        filter_config = FilterConfig.from_file()

    weights = preferences["weights"]

    subscores = {
        "neighborhood_value": _score_neighborhood_value(detail, preferences),
        "construction_condition": _score_construction(detail, preferences),
        "ownership": _score_ownership(detail),
        "energy_label": _score_energy_label(detail, preferences),
        "garden": _score_garden(detail, preferences),
        "parking": _score_parking(detail),
        "living_area": _score_living_area(detail, filter_config, preferences),
        "rooms": _score_rooms(detail, filter_config, preferences),
        "garage": _score_garage(detail),
        "plot_size": _score_plot_size(detail, preferences),
        "balcony": _score_balcony(detail),
        "heating": _score_heating(detail),
    }

    available = {k: v for k, v in subscores.items() if v is not None}
    missing = [k for k in subscores if subscores[k] is None]

    if not available:
        return ScoreResult(
            score=None,
            breakdown=[],
            confidence="no_data",
            missing_criteria=missing,
        )

    total_weight = sum(weights[k] for k in available)
    score = round(sum(weights[k] * available[k] for k in available) / total_weight * 100)
    top_criterion = max(weights, key=weights.get)
    if top_criterion in missing:
        confidence = "partial_major_missing"
    else:
        confidence = "full" if not missing else "partial"

    breakdown = []
    for k in weights:
        if k in available:
            points_possible = round(weights[k] / total_weight * 100)
            points_earned = round(points_possible * available[k])
        else:
            points_possible = 0
            points_earned = 0
        breakdown.append({
            "criterion": k,
            "points_earned": points_earned,
            "points_possible": points_possible,
            "matched": k in available,
        })

    return ScoreResult(
        score=score,
        breakdown=breakdown,
        confidence=confidence,
        missing_criteria=missing,
    )


def load_preferences(path: Path | str | None = None) -> dict:
    """Public API to load preferences. Used by main.py and scoring.py internally."""
    return _load_preferences(path)