"""Phase 2 configurable search filters (single source of truth).

``FilterConfig`` is the frozen, immutable container for the housing search
criteria. Values are loaded from the human-editable filter file
``config/filters.json`` (project-root-relative) via ``from_file()``; the
Phase 1 hardcoded filter values serve as the defaults. Secrets and
environment-specific sensitive values stay in ``.env`` (loaded by
``src/notifier.py``), never in the filter file.

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

# Recognised keys in config/filters.json. Unknown keys are rejected so a typo
# cannot silently fall back to the defaults.
_FILTER_KEYS = (
    "price_min",
    "price_max",
    "bedrooms_min",
    "living_area_min",
    "property_type",
    "plot_size_min",
    "energy_label_min",
)


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
    property_type: str | None = None
    plot_size_min: int | None = None
    energy_label_min: str | None = None

    def __post_init__(self) -> None:
        for name in ("price_min", "price_max", "bedrooms_min", "living_area_min"):
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer, got {value!r}.")

        if self.plot_size_min is not None and type(self.plot_size_min) is not int:
            raise ValueError(
                f"plot_size_min must be an integer or None, got {self.plot_size_min!r}."
            )

        for name in ("price_min", "bedrooms_min", "living_area_min"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative, got {getattr(self, name)!r}.")
        if self.plot_size_min is not None and self.plot_size_min < 0:
            raise ValueError(
                f"plot_size_min must not be negative, got {self.plot_size_min!r}."
            )

        if self.price_min > self.price_max:
            raise ValueError(
                f"price_min ({self.price_min}) must not exceed price_max ({self.price_max})."
            )

        if self.property_type is not None:
            if not isinstance(self.property_type, str) or not self.property_type.strip():
                raise ValueError(
                    "property_type must be a non-empty string or None, "
                    f"got {self.property_type!r}."
                )

        if self.energy_label_min is not None:
            if not isinstance(self.energy_label_min, str) or not self.energy_label_min.strip():
                raise ValueError(
                    "energy_label_min must be a non-empty string or None, "
                    f"got {self.energy_label_min!r}."
                )
            # Follow the project convention (scoring.py): uppercase-normalized.
            object.__setattr__(self, "energy_label_min", self.energy_label_min.strip().upper())

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "FilterConfig":
        """Build a ``FilterConfig`` from the human-editable filter file.

        Defaults to ``config/filters.json`` relative to the project root, so
        execution from cron, systemd, or tmux does not depend on the process
        working directory. Missing required keys fall back to the Phase 1
        defaults; missing optional keys become ``None``. Unknown keys and
        invalid values raise ``ValueError`` (never silently coerced).
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

        unknown = sorted(key for key in raw if key not in _FILTER_KEYS)
        if unknown:
            raise ValueError(
                f"Filter file {path} contains unknown key(s): {', '.join(unknown)}. "
                f"Expected keys: {', '.join(_FILTER_KEYS)}."
            )

        return cls(
            price_min=raw.get("price_min", _PHASE1_PRICE_MIN),
            price_max=raw.get("price_max", _PHASE1_PRICE_MAX),
            bedrooms_min=raw.get("bedrooms_min", _PHASE1_BEDROOMS_MIN),
            living_area_min=raw.get("living_area_min", _PHASE1_LIVING_AREA_MIN),
            property_type=raw.get("property_type"),
            plot_size_min=raw.get("plot_size_min"),
            energy_label_min=raw.get("energy_label_min"),
        )


DEFAULT_FILTERS = FilterConfig(
    price_min=_PHASE1_PRICE_MIN,
    price_max=_PHASE1_PRICE_MAX,
    bedrooms_min=_PHASE1_BEDROOMS_MIN,
    living_area_min=_PHASE1_LIVING_AREA_MIN,
)
