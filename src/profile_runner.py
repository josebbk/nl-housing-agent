"""Generic isolated Funda profile runner.

A "profile" is a named, self-contained Funda search/notification target:

    profile
      -> filter configuration (a JSON file loaded via FilterConfig)
      -> profile-specific SQLite DB (a private "sent" ledger only)
      -> Funda search (scrape_funda)
      -> profile-specific matching (_matches_filters with its own area slugs)
      -> rich notification (send_listing_notification)
      -> profile-specific Telegram forum topic (its own message_thread_id)

Each profile is fully isolated from every other profile and from the global
``main.py`` pipeline: it never touches ``config/filters.json``, never reads the
global ``notified`` flag, and never posts to the general chat / other topics.

Profiles are declared in ``config/topic_profiles.json`` (name, filter file,
DB path, topic id, area slugs). The Diemen topic is driven by its own
``src/diemen_topic.py`` (this module reuses the same building blocks via
``diemen_topic.apply_live``); new profiles are driven by this runner.

For a brand-new profile the initial "seed" is simply the first live run: with
an empty sent-ledger, ``apply_live`` posts every currently-matching listing
and records them in the ledger, so subsequent runs post only new matches.

Run:

    python -m src.profile_runner --profile abcoude --mode live
    python -m src.profile_runner --profile ouderkerk-aan-de-amstel --dry-run
"""

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import FilterConfig
from .diemen_topic import apply_live, _init_diemen_sent, _diemen_sent_ids

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROFILES_PATH = _PROJECT_ROOT / "config" / "topic_profiles.json"


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    topic_name: str
    filters_path: Path
    db_path: Path
    topic_id: str
    area_slugs: tuple


def load_profiles(path: Path | str | None = None) -> dict:
    """Load the profile registry from config/topic_profiles.json."""
    path = Path(path or _DEFAULT_PROFILES_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles: dict = {}
    for key, cfg in data.items():
        profiles[key] = Profile(
            key=key,
            name=cfg["name"],
            topic_name=cfg.get("topic_name", cfg["name"]),
            filters_path=_PROJECT_ROOT / cfg["filters"],
            db_path=_PROJECT_ROOT / cfg["db"],
            topic_id=str(cfg.get("topic_id", "")),
            area_slugs=tuple(cfg.get("area_slugs", [])),
        )
    return profiles


def run_live(profile: Profile, dry_run: bool = False) -> int:
    """Scrape with the profile's filters and post NEW matches to its topic.

    The profile's own sent-ledger (in its own DB) is the dedup source, so a
    fresh profile posts all currently-matching listings on the first run (the
    seed) and only new listings afterwards.
    """
    filters = FilterConfig.from_file(profile.filters_path)
    logger.info(
        "Profile '%s': filters=%s db=%s topic_id=%s area_slugs=%s",
        profile.key, profile.filters_path.name, profile.db_path,
        profile.topic_id, list(profile.area_slugs),
    )
    return apply_live(
        filters, profile.db_path, profile.topic_id,
        dry_run=dry_run, area_slugs=profile.area_slugs,
    )


def run_seed(profile: Profile, dry_run: bool = False) -> int:
    """Initial seed: the first live run (empty ledger => posts all matches).

    Kept as a distinct entry point so the seed step is explicit and
    distinguishable from ongoing live runs, while sharing the same dedup
    ledger (no repeated seed notifications on later runs).
    """
    logger.info("Seed (first live run) for profile '%s'.", profile.key)
    return run_live(profile, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an isolated Funda profile (scrape -> match -> notify)."
    )
    parser.add_argument("--profile", required=True,
                        help="Profile key from config/topic_profiles.json.")
    parser.add_argument("--mode", choices=("live", "seed"), default="live",
                        help="live = post NEW matches; seed = first run that "
                             "posts all current matches (default: live).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape/select only; send no Telegram messages.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    profiles = load_profiles()
    if args.profile not in profiles:
        raise SystemExit(
            f"Unknown profile '{args.profile}'. Known: {sorted(profiles)}"
        )
    profile = profiles[args.profile]

    if args.mode == "seed":
        posted = run_seed(profile, dry_run=args.dry_run)
    else:
        posted = run_live(profile, dry_run=args.dry_run)
    logger.info("Profile '%s' done: %d listing(s) would be/posted.",
                profile.key, posted)


if __name__ == "__main__":
    main()
