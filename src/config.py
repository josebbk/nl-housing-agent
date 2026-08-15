"""Phase 2 configurable search filters (single source of truth).

``FilterConfig`` is the frozen, immutable container for the housing search
criteria. The Phase 1 hardcoded filter values live here as defaults, and
``from_env()`` overrides them from ``FUNDA_*`` environment variables loaded
through the project's existing python-dotenv convention (see ``src/notifier.py``).

Only this module defines the filter defaults. ``storage.py`` and ``main.py``
must import ``DEFAULT_FILTERS`` / ``FilterConfig`` from here rather than
redefining them.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Environment variable names (frozen Phase 2 contract).
_ENV_PRICE_MIN = "FUNDA_PRICE_MIN"
_ENV_PRICE_MAX = "FUNDA_PRICE_MAX"
_ENV_BEDROOMS_MIN = "FUNDA_BEDROOMS_MIN"
_ENV_LIVING_AREA_MIN = "FUNDA_LIVING_AREA_MIN"
_ENV_PROPERTY_TYPE = "FUNDA_PROPERTY_TYPE"
_ENV_PLOT_SIZE_MIN = "FUNDA_PLOT_SIZE_MIN"
_ENV_ENERGY_LABEL_MIN = "FUNDA_ENERGY_LABEL_MIN"

# Phase 1 filter defaults (frozen Phase 2 contract).
_PHASE1_PRICE_MIN = 550_000
_PHASE1_PRICE_MAX = 750_000
_PHASE1_BEDROOMS_MIN = 3
_PHASE1_LIVING_AREA_MIN = 100


def _load_env() -> None:
    """Load .env from the project root if present (project convention)."""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
        logger.debug("Loaded .env from %s", _ENV_PATH)
    else:
        logger.debug("No .env file at %s; using process environment only.", _ENV_PATH)


def _parse_int(name: str, raw: str) -> int:
    """Parse an integer, raising a clear ValueError on invalid input."""
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"Environment variable {name} must be an integer, got {raw!r}."
        ) from None


def _get_int_env(name: str, default: int) -> int:
    """Read an integer env var; missing/empty falls back to the default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return _parse_int(name, raw)


def _get_optional_int_env(name: str) -> int | None:
    """Read an optional integer env var; missing/empty -> None."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return _parse_int(name, raw)


def _get_optional_str_env(name: str) -> str | None:
    """Read an optional string env var; missing/empty -> None."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


@dataclass(frozen=True)
class FilterConfig:
    """Immutable housing search filter criteria.

    Optional preferences default to ``None``, meaning "no additional
    preference filter". Direct construction validates the values;
    ``from_env()`` builds an instance from the environment.
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
    def from_env(cls) -> "FilterConfig":
        """Build a ``FilterConfig`` from the environment.

        Missing or empty required variables fall back to the Phase 1 defaults;
        empty optional variables become ``None``. Invalid values raise
        ``ValueError`` (never silently coerced).
        """
        _load_env()
        return cls(
            price_min=_get_int_env(_ENV_PRICE_MIN, _PHASE1_PRICE_MIN),
            price_max=_get_int_env(_ENV_PRICE_MAX, _PHASE1_PRICE_MAX),
            bedrooms_min=_get_int_env(_ENV_BEDROOMS_MIN, _PHASE1_BEDROOMS_MIN),
            living_area_min=_get_int_env(_ENV_LIVING_AREA_MIN, _PHASE1_LIVING_AREA_MIN),
            property_type=_get_optional_str_env(_ENV_PROPERTY_TYPE),
            plot_size_min=_get_optional_int_env(_ENV_PLOT_SIZE_MIN),
            energy_label_min=_get_optional_str_env(_ENV_ENERGY_LABEL_MIN),
        )


DEFAULT_FILTERS = FilterConfig(
    price_min=_PHASE1_PRICE_MIN,
    price_max=_PHASE1_PRICE_MAX,
    bedrooms_min=_PHASE1_BEDROOMS_MIN,
    living_area_min=_PHASE1_LIVING_AREA_MIN,
)
