"""
Preference-based scoring for Funda listings.

Scores a listing's detail data against user preferences loaded from
config/preferences.json. Each criterion contributes a weighted subscore,
and the final score is renormalized when data is missing for some criteria.
"""

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PREFERENCES_PATH = _PROJECT_ROOT / "config" / "preferences.json"


@dataclass
class ScoreResult:
    """Result of scoring a listing against preferences."""
    score: int  # 0-100, or None if no data available
    breakdown: list[dict]  # list of {criterion, points_earned, points_possible, matched}
    confidence: str  # "full" | "partial" | "no_data"
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
        logger.warning(
            "Preference weights sum to %d (expected 100). This is not enforced "
            "but may produce unexpected scores.",
            total,
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

    Returns a float in [0, 1] or None if neither year_built nor insulation_score
    is available. Combines both signals with 50/50 weight.
    """
    year_built = detail.get("year_built")
    insulation_score = detail.get("insulation_score")

    if year_built is None and insulation_score is None:
        return None

    year_score = None
    insulation_comp_score = None

    if year_built is not None:
        # Newer buildings score higher.
        # 2025+ = 1.0, pre-1950 = 0.0, linear in between.
        year_score = min(max((year_built - 1950) / (2025 - 1950), 0), 1)

    if insulation_score is not None:
        insulation_comp_score = insulation_score

    if year_score is None:
        return round(insulation_comp_score, 4)
    if insulation_comp_score is None:
        return round(year_score, 4)

    return round((year_score + insulation_comp_score) / 2, 4)


def _score_ownership(detail: dict) -> float | None:
    """Score ownership type.

    Returns a float in [0, 1] or None if ownership_type is missing.

    - "full" = 1.0
    - "erfpacht" with no canon = 0.7
    - "erfpacht" with canon = 0.7 - (canon / price * 5), clamped to [0, 1]
    """
    ownership_type = detail.get("ownership_type")
    if ownership_type is None:
        return None

    if ownership_type == "full":
        return 1.0

    if ownership_type == "erfpacht":
        canon = detail.get("erfpacht_canon_annual")
        price = detail.get("price")

        if canon is None or price is None or price == 0:
            return 0.7  # No canon info, assume moderate

        canon_ratio = canon / price
        # A canon of 2% of price = 0.7 - 0.1 = 0.6
        # A canon of 5% of price = 0.7 - 0.25 = 0.45
        score = 0.7 - (canon_ratio * 5)
        return round(max(0, min(1, score)), 4)

    return 0.0


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
        return round(idx / (len(scale) - 1), 4)

    # Try case-insensitive match
    for i, s in enumerate(scale):
        if s.lower() == label.lower():
            return round(i / (len(scale) - 1), 4)

    # Label not in scale — return middle value
    logger.warning("Unknown energy label '%s', using midpoint score", label)
    return 0.5


def _score_amenities(detail: dict, preferences: dict) -> float | None:
    """Score amenities based on keyword matches.

    Returns a float in [0, 1] or None if amenities_raw is missing.

    Counts how many tracked keywords appear in the raw amenities text.
    Score = matched / len(tracked_keywords), capped at 1.0.
    """
    amenities_raw = detail.get("amenities_raw")
    if not amenities_raw:
        return None

    tracked = preferences.get("amenities_tracked", [])
    if not tracked:
        return None

    raw_lower = amenities_raw.lower()
    matched = [kw for kw in tracked if kw.lower() in raw_lower]

    # Store matched keywords in the detail dict for later use
    detail["amenities_matched"] = matched

    return round(min(len(matched) / len(tracked), 1.0), 4)


def _score_garden(detail: dict, preferences: dict) -> float | None:
    """Score garden based on presence, size, and orientation.

    Returns a float in [0, 1] or None if garden_present is not available.
    A listing with no garden field at all returns 0.0 (not None).
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

    Returns a float in [0, 1] or None if parking_type is missing.

    - "private" = 1.0
    - "carport" = 0.7
    - "public" = 0.3
    - "paid" = 0.5
    - None (no parking field) = 0.0 (known, and no parking)
    """
    parking_type = detail.get("parking_type")
    if parking_type is None:
        # No parking field at all — we know there's no parking
        return 0.0

    parking_scores = {
        "private": 1.0,
        "carport": 0.7,
        "paid": 0.5,
        "public": 0.3,
    }

    return parking_scores.get(parking_type, 0.0)


def _score_bathrooms(detail: dict) -> float | None:
    """Score number of bathrooms.

    Returns a float in [0, 1] or None if bathrooms is missing.

    Normalized against a maximum of 3 bathrooms.
    """
    bathrooms = detail.get("bathrooms")
    if bathrooms is None:
        return None

    return round(min(bathrooms / 3, 1.0), 4)


def score_listing(detail: dict, preferences: dict | None = None) -> ScoreResult:
    """Score a listing against preferences.

    Parameters
    ----------
    detail : dict
        Detail page data from fetch_listing_details().
    preferences : dict or None
        Loaded preferences from config/preferences.json. If None, loads
        from the default path.

    Returns
    -------
    ScoreResult
        score: int 0-100, or None if no data available
        breakdown: list of criterion detail dicts
        confidence: "full" | "partial" | "no_data"
        missing_criteria: list of criterion names with no data
    """
    if preferences is None:
        preferences = _load_preferences()

    weights = preferences["weights"]

    subscores = {
        "neighborhood_value": _score_neighborhood_value(detail, preferences),
        "construction_condition": _score_construction(detail, preferences),
        "ownership": _score_ownership(detail),
        "energy_label": _score_energy_label(detail, preferences),
        "amenities": _score_amenities(detail, preferences),
        "garden": _score_garden(detail, preferences),
        "parking": _score_parking(detail),
        "bathrooms": _score_bathrooms(detail),
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
    confidence = "full" if not missing else "partial"

    breakdown = [
        {
            "criterion": k,
            "points_earned": round(weights[k] * available[k]) if k in available else 0,
            "points_possible": weights[k],
            "matched": k in available,
        }
        for k in weights
    ]

    return ScoreResult(
        score=score,
        breakdown=breakdown,
        confidence=confidence,
        missing_criteria=missing,
    )


def load_preferences(path: Path | str | None = None) -> dict:
    """Public API to load preferences. Used by main.py and scoring.py internally."""
    return _load_preferences(path)