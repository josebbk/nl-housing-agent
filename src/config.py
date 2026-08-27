"""Phase 2 configurable search filters (single source of truth).

``FilterConfig`` is the frozen, immutable container for the housing search
criteria. Values are loaded from the human-editable filter file
``config/filters.json`` (project-root-relative) via ``from_file()``; the
Phase 1 hardcoded filter values serve as the defaults. Secrets and
environment-specific sensitive values stay in ``.env`` (loaded by
``src/notifier.py``), never in the filter file.

The filter file is organised into two sections so it stays readable for a
non-developer owner: ``"required"`` holds the four Phase 1 base criteria
(with their current values), ``"optional"`` lists every optional preference
key where ``null`` means "no restriction". A key must appear in its own
section. The older flat layout (all keys at the top level) is still
accepted for backward compatibility.

Only this module defines the filter defaults. ``storage.py`` and ``main.py``
must import ``DEFAULT_FILTERS`` / ``FilterConfig`` from here rather than
redefining them.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FILTERS_PATH = _PROJECT_ROOT / "config" / "filters.json"

# Phase 1 filter defaults (frozen Phase 2 contract).
_PHASE1_PRICE_MIN = 550_000
_PHASE1_PRICE_MAX = 750_000
_PHASE1_BEDROOMS_MIN = 3
_PHASE1_LIVING_AREA_MIN = 100

# Recognised keys in config/filters.json, split into the two sections of the
# human-friendly file layout. Unknown keys are rejected so a typo cannot
# silently fall back to the defaults.
_REQUIRED_SECTION = "required"
_OPTIONAL_SECTION = "optional"

_REQUIRED_FILTER_KEYS = (
    "price_min",
    "price_max",
    "bedrooms_min",
    "living_area_min",
)

_OPTIONAL_FILTER_KEYS = (
    "bedrooms_max",
    "living_area_max",
    "rooms_min",
    "rooms_max",
    "plot_size_min",
    "plot_size_max",
    "property_type",
    "energy_labels",
    "transaction_type",
    "radius_km",
    "construction_type",
    "construction_periods",
    "garden",
    "garden_size_min",
    "availability",
    "sort",
)

_FILTER_KEYS = _REQUIRED_FILTER_KEYS + _OPTIONAL_FILTER_KEYS

# The energy_labels default order (A++++, A+++, A++, A+, A, B, C, D, A+++++)
# is preserved exactly as it appeared in the authoritative source URL, not
# sorted ordinally. This ordering is unusual (A+++++ appears last, after D)
# and should be investigated later to confirm whether Funda's search endpoint
# is actually order-sensitive, or whether this was an artifact of how the URL
# was originally captured. Do not silently reorder it.

# Human-readable construction-period keys (used in ``config/filters.json``)
# mapped to Funda's internal parameter values. The URL builder (scraper.py)
# consumes this map to translate the configured keys into
# ``construction_period=<code>`` parameters.
CONSTRUCTION_PERIOD_MAP = {
    "1971-1980": "from_1971_to_1980",
    "1981-1990": "from_1981_to_1990",
    "1991-2000": "from_1991_to_2000",
    "2001-2010": "from_2001_to_2010",
    "2011-2020": "from_2011_to_2020",
    "after_2020": "after_2020",
}


def _flatten_filter_file(raw: dict, path: Path) -> dict:
    """Normalise a parsed filter file into a flat ``{key: value}`` mapping.

    Accepts both supported layouts:

    * the current sectioned layout with ``"required"`` and/or ``"optional"``
      objects as the only top-level keys, and
    * the legacy flat layout where filter keys sit at the top level.

    Raises ``ValueError`` when a section is not a JSON object, when a key
    appears in the wrong section, when an unknown key appears inside a
    section, or when sectioned and flat layouts are mixed.
    """
    if _REQUIRED_SECTION not in raw and _OPTIONAL_SECTION not in raw:
        return raw

    unknown_top = sorted(
        key for key in raw if key not in (_REQUIRED_SECTION, _OPTIONAL_SECTION)
    )
    if unknown_top:
        raise ValueError(
            f"Filter file {path} mixes filter keys ({', '.join(unknown_top)}) "
            f'with the "{_REQUIRED_SECTION}"/"{_OPTIONAL_SECTION}" sections. '
            f"Put every filter key inside its section."
        )

    flat: dict = {}
    for section, allowed in (
        (_REQUIRED_SECTION, frozenset(_REQUIRED_FILTER_KEYS)),
        (_OPTIONAL_SECTION, frozenset(_OPTIONAL_FILTER_KEYS)),
    ):
        if section not in raw:
            continue
        body = raw[section]
        if not isinstance(body, dict):
            raise ValueError(
                f'Filter file {path}: "{section}" must be a JSON object, '
                f"got {type(body).__name__}."
            )
        other = (
            _OPTIONAL_FILTER_KEYS if section == _REQUIRED_SECTION
            else _REQUIRED_FILTER_KEYS
        )
        for key, value in body.items():
            if key in other:
                raise ValueError(
                    f'Filter file {path}: "{key}" belongs in the '
                    f'"{_OPTIONAL_SECTION if section == _REQUIRED_SECTION else _REQUIRED_SECTION}"'
                    f" section, not \"{section}\"."
                )
            if key not in allowed:
                raise ValueError(
                    f"Filter file {path}: unknown key {key!r} in "
                    f'"{section}" section. Expected keys: '
                    f"{', '.join(allowed)}."
                )
            flat[key] = value
    return flat


@dataclass(frozen=True)
class FilterConfig:
    """Immutable housing search filter criteria.

    Optional preferences default to ``None``, meaning "no additional
    preference filter". Direct construction validates the values;
    ``from_file()`` builds an instance from ``config/filters.json``.
    """

    price_min: int
    price_max: int
    bedrooms_min: int
    living_area_min: int
    bedrooms_max: int | None = None
    living_area_max: int | None = None
    rooms_min: int | None = None
    rooms_max: int | None = None
    plot_size_min: int | None = None
    plot_size_max: int | None = None
    property_type: str | None = None
    energy_labels: list[str] | None = None
    transaction_type: str | None = None
    radius_km: int | None = None
    construction_type: str | None = None
    construction_periods: list[str] | None = None
    garden: bool | None = None
    garden_size_min: int | None = None
    availability: str | None = None
    sort: str | None = None

    def __post_init__(self) -> None:
        for name in ("price_min", "price_max", "bedrooms_min", "living_area_min"):
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer, got {value!r}.")

        if self.plot_size_min is not None and type(self.plot_size_min) is not int:
            raise ValueError(
                f"plot_size_min must be an integer or None, got {self.plot_size_min!r}."
            )

        # Optional numeric min/max fields: must be integers or None, and
        # non-negative. Range consistency (min <= max) is validated below.
        for name in (
            "bedrooms_max",
            "living_area_max",
            "rooms_min",
            "rooms_max",
            "plot_size_min",
            "plot_size_max",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise ValueError(f"{name} must be an integer or None, got {value!r}.")
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative, got {value!r}.")

        for name in ("price_min", "bedrooms_min", "living_area_min"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative, got {getattr(self, name)!r}.")

        if self.price_min > self.price_max:
            raise ValueError(
                f"price_min ({self.price_min}) must not exceed price_max ({self.price_max})."
            )

        for lo_name, hi_name in (
            ("bedrooms_min", "bedrooms_max"),
            ("living_area_min", "living_area_max"),
            ("rooms_min", "rooms_max"),
            ("plot_size_min", "plot_size_max"),
        ):
            lo = getattr(self, lo_name)
            hi = getattr(self, hi_name)
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(
                    f"{lo_name} ({lo}) must not exceed {hi_name} ({hi})."
                )

        if self.property_type is not None:
            if not isinstance(self.property_type, str) or not self.property_type.strip():
                raise ValueError(
                    "property_type must be a non-empty string or None, "
                    f"got {self.property_type!r}."
                )

        if self.energy_labels is not None:
            if not isinstance(self.energy_labels, list) or not self.energy_labels:
                raise ValueError(
                    "energy_labels must be a non-empty list of strings or None, "
                    f"got {self.energy_labels!r}."
                )
            for label in self.energy_labels:
                if not isinstance(label, str) or not label.strip():
                    raise ValueError(
                        "energy_labels must contain only non-empty strings, "
                        f"got {label!r}."
                    )

        if self.transaction_type is not None:
            if not isinstance(self.transaction_type, str) or not self.transaction_type.strip():
                raise ValueError(
                    "transaction_type must be a non-empty string or None, "
                    f"got {self.transaction_type!r}."
                )
            normalized = self.transaction_type.strip().lower()
            if normalized not in ("koop", "huur"):
                raise ValueError(
                    "transaction_type must be 'koop' or 'huur' (or None), "
                    f"got {self.transaction_type!r}."
                )
            object.__setattr__(self, "transaction_type", normalized)

        # radius_km maps to the standalone ``radius_search={radius_km}`` query
        # parameter (not an embedded value inside ``selected_area``, which must
        # remain a plain area slug like "amsterdam"). The URL builder in
        # scraper.py is responsible for emitting ``radius_search``.
        if self.radius_km is not None:
            if type(self.radius_km) is not int:
                raise ValueError(
                    f"radius_km must be an integer or None, got {self.radius_km!r}."
                )
            if self.radius_km <= 0:
                raise ValueError(
                    "radius_km must be a positive number of kilometres, "
                    f"got {self.radius_km!r}."
                )

        if self.construction_type is not None:
            if not isinstance(self.construction_type, str) or not self.construction_type.strip():
                raise ValueError(
                    "construction_type must be a non-empty string or None, "
                    f"got {self.construction_type!r}."
                )
            normalized = self.construction_type.strip().lower()
            if normalized not in ("existing", "new"):
                raise ValueError(
                    "construction_type must be 'existing' or 'new' (or None), "
                    f"got {self.construction_type!r}."
                )
            object.__setattr__(self, "construction_type", normalized)

        if self.construction_periods is not None:
            if not isinstance(self.construction_periods, list) or not self.construction_periods:
                raise ValueError(
                    "construction_periods must be a non-empty list of strings "
                    f"or None, got {self.construction_periods!r}."
                )
            invalid = [p for p in self.construction_periods if p not in CONSTRUCTION_PERIOD_MAP]
            if invalid:
                raise ValueError(
                    "construction_periods contains invalid key(s): "
                    f"{', '.join(repr(p) for p in invalid)}. Valid options: "
                    f"{', '.join(sorted(CONSTRUCTION_PERIOD_MAP))}."
                )

        if self.garden is not None and type(self.garden) is not bool:
            raise ValueError(f"garden must be a boolean or None, got {self.garden!r}.")

        if self.garden_size_min is not None:
            if type(self.garden_size_min) is not int:
                raise ValueError(
                    f"garden_size_min must be an integer or None, got {self.garden_size_min!r}."
                )
            if self.garden_size_min < 0:
                raise ValueError(
                    f"garden_size_min must not be negative, got {self.garden_size_min!r}."
                )
            if self.garden is not True:
                raise ValueError(
                    "garden_size_min requires garden=true (exterior_space_type=garden); "
                    f"got garden={self.garden!r} with garden_size_min={self.garden_size_min!r}."
                )

        for name in ("availability", "sort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(
                    f"{name} must be a non-empty string or None, got {value!r}."
                )

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "FilterConfig":
        """Build a ``FilterConfig`` from the human-editable filter file.

        Defaults to ``config/filters.json`` relative to the project root, so
        execution from cron, systemd, or tmux does not depend on the process
        working directory. The file may use the sectioned layout
        (``"required"`` / ``"optional"`` objects) or the legacy flat layout.
        Missing required keys fall back to the Phase 1 defaults; missing
        optional keys become ``None``. Unknown keys and invalid values raise
        ``ValueError`` (never silently coerced).
        """
        path = Path(path) if path is not None else _FILTERS_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Could not read filter file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Filter file {path} is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"Filter file {path} must contain a JSON object, got {type(raw).__name__}."
            )

        flat = _flatten_filter_file(raw, path)

        unknown = sorted(key for key in flat if key not in _FILTER_KEYS)
        if unknown:
            raise ValueError(
                f"Filter file {path} contains unknown key(s): {', '.join(unknown)}. "
                f"Expected keys: {', '.join(_FILTER_KEYS)}."
            )

        return cls(
            price_min=flat.get("price_min", _PHASE1_PRICE_MIN),
            price_max=flat.get("price_max", _PHASE1_PRICE_MAX),
            bedrooms_min=flat.get("bedrooms_min", _PHASE1_BEDROOMS_MIN),
            living_area_min=flat.get("living_area_min", _PHASE1_LIVING_AREA_MIN),
            bedrooms_max=flat.get("bedrooms_max"),
            living_area_max=flat.get("living_area_max"),
            rooms_min=flat.get("rooms_min"),
            rooms_max=flat.get("rooms_max"),
            plot_size_min=flat.get("plot_size_min"),
            plot_size_max=flat.get("plot_size_max"),
            property_type=flat.get("property_type"),
            energy_labels=flat.get("energy_labels"),
            transaction_type=flat.get("transaction_type"),
            radius_km=flat.get("radius_km"),
            construction_type=flat.get("construction_type"),
            construction_periods=flat.get("construction_periods"),
            garden=flat.get("garden"),
            garden_size_min=flat.get("garden_size_min"),
            availability=flat.get("availability"),
            sort=flat.get("sort"),
        )


DEFAULT_FILTERS = FilterConfig(
    price_min=_PHASE1_PRICE_MIN,
    price_max=_PHASE1_PRICE_MAX,
    bedrooms_min=_PHASE1_BEDROOMS_MIN,
    living_area_min=_PHASE1_LIVING_AREA_MIN,
)

# ---------------------------------------------------------------------------
# Retention config (stale-listing archival policy — Task 2 of 4)
#
# This module owns the only retention defaults.  storage.py and main.py must
# import DEFAULT_RETENTION / RetentionConfig from here rather than
# redefining them.
# ---------------------------------------------------------------------------

_RETENTION_PATH = _PROJECT_ROOT / "config" / "retention.json"

_RETENTION_KEYS = ("stale_days",)

_DEFAULT_STALE_DAYS = 60


@dataclass(frozen=True)
class RetentionConfig:
    """Immutable data-retention policy.

    ``from_file()`` loads the human-editable file
    ``config/retention.json``; missing ``stale_days`` falls back to 60.
    """

    stale_days: int

    def __post_init__(self) -> None:
        if type(self.stale_days) is not int:
            raise ValueError(
                f"stale_days must be an integer, got {self.stale_days!r}."
            )
        if self.stale_days <= 0:
            raise ValueError(
                f"stale_days must be positive, got {self.stale_days!r}."
            )

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "RetentionConfig":
        """Build a ``RetentionConfig`` from the human-editable retention file.

        Defaults to ``config/retention.json`` relative to the project root.
        Missing ``stale_days`` falls back to 60; unknown keys raise
        ``ValueError``.
        """
        path = Path(path) if path is not None else _RETENTION_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(
                f"Could not read retention file {path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Retention file {path} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"Retention file {path} must contain a JSON object, "
                f"got {type(raw).__name__}."
            )

        unknown = sorted(key for key in raw if key not in _RETENTION_KEYS)
        if unknown:
            raise ValueError(
                f"Retention file {path} contains unknown key(s): "
                f"{', '.join(unknown)}. Expected keys: "
                f"{', '.join(_RETENTION_KEYS)}."
            )

        return cls(stale_days=raw.get("stale_days", _DEFAULT_STALE_DAYS))


DEFAULT_RETENTION = RetentionConfig(stale_days=60)
