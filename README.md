# Amsterdam Funda Home-Search Agent

## 1. Project Overview

This project is a personal automation tool that monitors **Funda.nl** (the largest Dutch real-estate listing platform) for new residential properties for sale in **Amsterdam**, evaluates them against configurable housing criteria, scores them against the owner's preferences, and sends a **Telegram notification** for listings worth reviewing.

It exists to remove the need to manually refresh Funda search results throughout the day, and to surface promising listings fast enough to be useful in a competitive housing market. It is a single-user, internal tool — not a public product (see [§10](#10-future-expansion-toward-a-public-user-facing-bot) for what that would require).

Development has been carried out across sessions using different AI coding CLIs (Gemini CLI, OpenCode CLI) against this shared repository; `AGENTS.md` is the canonical behavioral contract those agents follow.

For deeper detail beyond this overview, see:
- `product.md` — product scope, requirements, roadmap
- `architecture.md` — full technical design, schema, and design-decision history
- `Operations.md` — how to run, operate, and troubleshoot the system
- `AGENTS.md` — rules for AI coding agents working on this repo
- `docs/site-notes/funda.md` — running log of Funda scraper breakages and fixes

---

## 2. System Architecture

| Component | Responsibility | Key details |
|---|---|---|
| `src/scraper.py` | Scrapes Funda search-result pages (card-level data) | Playwright/Chromium; bypasses Akamai bot-protection by fetching HTML via `urllib` with browser-like headers first, then loading it into Playwright via a `data:` URL for JS rendering |
| `src/detail_scraper.py` | Fetches and parses a single listing's detail page | Same Akamai-bypass technique; extracts ~20 nullable fields (ownership, garden, garage, insulation, etc.) via section/keyword parsing, never guesses a missing value |
| `src/scoring.py` | Scores a listing against owner preferences | 12 weighted criteria, renormalized when data is missing; returns score (0–100), breakdown, and a confidence flag |
| `src/storage.py` | SQLite persistence, dedup, retention | `listings` table, `listings_archive`, `scraper_metadata` (filter snapshot + last successful run) |
| `src/config.py` | Loads and validates filter/retention config | `FilterConfig`, `RetentionConfig` — single source of truth for defaults |
| `src/notifier.py` | Formats and sends Telegram messages | Score breakdown, best/weakest criteria, failure-alert channel |
| `src/main.py` | Orchestrates a full run | Ties scraping → storage → filtering → detail/scoring → notification → archival together; also implements `--dry-run`, `--backfill`, `--seed` |
| `config/filters.json` | Human-editable search/filter criteria | Loaded by `FilterConfig.from_file()` |
| `config/preferences.json` | Scoring weights and keyword dictionaries | Loaded by `scoring.load_preferences()` |
| `config/retention.json` | Stale-listing archival policy | Loaded by `RetentionConfig.from_file()` |
| `.env` | Secrets only (Telegram token/chat/topic IDs) | Never used for search filters or scoring weights |

---

## 3. Execution Flow

```
cron (or manual run)
        ↓
scrape_funda()  — Playwright, one browser instance, paced page loads
        ↓
insert_listing() for every scraped listing  — SQLite dedup by listing_id
        ↓
fetch_unnotified_matching_listings()  — Phase 1 filters applied (config/filters.json)
        ↓
for each matching, unnotified listing:
    fetch_listing_details(url)  — detail-page fetch (only here, not for every card)
    score_listing(...)          — 12-criterion weighted score
    persist detail + score to DB row
        ↓
send Telegram notification (unless suppressed by full-scan gating, see §5)
        ↓
archive_stale_listings()  — housekeeping, independent of the above
        ↓
save filter snapshot + last_successful_run timestamp
```

Scraping and notification are deliberately separate phases so failures in one are easy to diagnose independently of the other.

### Scan mode: full scan vs. delta scan

Each run first decides whether it's a **full scan** or a **delta scan**, which affects both how much is scraped and whether notification gating applies:

| Trigger | `run_is_full_scan` | Scrape behavior | Gating |
|---|---|---|---|
| `config/filters.json` changed since last run (or no snapshot recorded yet) | True | No publication-date filter, 5-page cap | Enabled |
| Last successful run was >3 days ago | True | No publication-date filter, 5-page cap | Enabled |
| Neither of the above | False (delta) | Last-3-days publication filter, 15-page cap | Disabled |

---

## 4. First Run vs. Subsequent Runs

**First run** (no prior filter snapshot, no prior successful run) is always a **full scan**:
- All Amsterdam listings matching the current `config/filters.json` criteria are scraped (up to 5 pages) and inserted into SQLite.
- Every listing that is genuinely **newly inserted** during this run, and passes the Phase 1 filters, gets a detail-page fetch and a score.
- **Gating applies:** because this is a full scan, newly inserted listings are only actually notified if `score >= 70`. Listings scoring below 70 are still marked `notified = 1` (so they don't linger and get notified later for an unrelated reason), but no Telegram message is sent for them.
- This prevents a "notification blast" the moment the system is switched on or the filters are changed — without this gate, every already-existing matching listing on Funda would trigger a message at once.
- A filter snapshot and a `last_successful_run` timestamp are recorded at the end, which is what makes the *next* run recognize itself as a delta scan (assuming filters are unchanged and the run completes within 3 days).

**Subsequent runs** (delta scans, the normal case):
- Only listings published in roughly the last 3 days are scraped (fewer pages needed).
- Previously seen listings are recognized by `listing_id` and are **not** re-treated as new; card-level fields are refreatched and updated, but `notified` is left untouched unless explicitly reset.
- Genuinely new listings matching the filters go through the same detail-fetch + scoring step as before, but **no 70-point gate applies** — any matching new listing is notified regardless of score.
- Detail-page/score data, once fetched, is preserved across future card-only re-inserts (`insert_listing()` never overwrites a populated Phase 2 field with `NULL`), so rescoring later (e.g. after a weight change) can reuse stored data instead of re-fetching every listing's detail page.
- If more than 3 days pass without a successful run, the *next* run reverts to full-scan behavior (including the 70-point gate) as a safety fallback.

---

## 5. Filtering

Phase 1 hard filters (price, bedrooms, living area, plus several optional preferences) are defined in **`config/filters.json`** and loaded via `FilterConfig.from_file()` in `src/config.py`. Defaults (used for any key that's absent) are €550,000–€750,000, ≥3 bedrooms, ≥100 m².

Filters split into two groups:
- **Search-level filters** — passed straight to the Funda search URL (`price_min/max`, `living_area_min/max`, `bedrooms_min/max`, `rooms_min/max`, `radius_km`, `construction_type`, `transaction_type`).
- **Storage-level filters** — applied in the SQLite matching query in `storage.py` (`property_type`, `plot_size_min/max`, `energy_label_min/max`, plus the same price/bedrooms/living-area bounds as a second check).

A listing with a `NULL` value for an optional filtered field never satisfies that filter. Unknown keys or invalid values in `filters.json` raise a clear error rather than being silently ignored.

### Can filters be changed without touching code?

**Yes.** `config/filters.json` is a plain, human-editable JSON file — no source code or deployment change is required. To change criteria: edit the relevant key(s) in `config/filters.json`, save, and run the scraper normally (`python -m src.main`). The system detects the change automatically (this triggers the full-scan/gating behavior described in §4 for that one run), and behaves normally afterward. See `Operations.md` §4 for the full key reference and validation rules.

---

## 6. Scoring

Every listing that passes the Phase 1 filters is scored by `src/scoring.py` before notification, using weights and keyword dictionaries from **`config/preferences.json`**. The current system uses **12 weighted criteria** (neighborhood value, ownership, energy label, living area, construction condition, garage, parking, rooms, plot size, garden, heating, balcony — full detail in `architecture.md` §"Phase 2 — Detail-Page Scraping & Scoring").

Key mechanics:
- Each criterion contributes a subscore in `[0, 1]` or `None` if its underlying data is unavailable.
- The final 0–100 score is **renormalized** across only the criteria that had data — missing data is excluded from the average, never treated as a 0.
- A confidence flag (`full` / `partial` / `partial_major_missing` / `no_data`) accompanies every score, so the notification can flag when it's working off incomplete information.
- Weights must sum to exactly 100; `preferences.json` fails loudly at startup if they don't.

The scoring system's current practical accuracy, based on informal observation against hand-verified sample listings, is **roughly 70%** — this is not a statistically validated metric, just the project's current working estimate of how often the score reflects genuine listing quality. It should be treated as a rough signal to prioritize review, not a definitive ranking.

---

## 7. Notifications

`src/notifier.py` builds and sends the Telegram message via the Bot API (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env`). Each message includes the address, price, living area, bedrooms, property type/neighborhood, the score with its confidence flag, the best/weakest scoring criteria, a full per-criterion breakdown, and the Funda URL. A separate failure-alert channel (`TELEGRAM_FAILURE_TOPIC_ID`) reports run failures (scrape errors, DB errors, notification delivery failures) without spamming the main chat.

A listing is only ever notified once: `notified` is set to `1` on successful delivery and is never reset except via the (currently unused) filter-change re-entry logic — repeated scrapes of an already-seen listing do not re-trigger a message.

---

## 8. Database / State Management

SQLite (`data/funda.db`) holds three tables:

- **`listings`** — one row per Funda listing, keyed by `listing_id` (derived from the Funda URL). Holds card-level fields (always overwritten on every scrape), Phase 2 detail/score fields (preserved across card-only re-inserts, only overwritten by a fresh detail fetch), `first_seen_at`/`last_seen_at` timestamps, and `notified`.
- **`listings_archive`** — an exact-schema mirror of `listings`. Listings whose `last_seen_at` (or `first_seen_at` as a fallback) is older than `config/retention.json`'s `stale_days` (default 60) are moved here automatically every normal run, keeping the live table bounded while preserving history.
- **`scraper_metadata`** — a small key/value table storing the last saved filter snapshot (for change detection) and the last successful run timestamp (for staleness fallback).

Deduplication is purely by `listing_id`; there is no cross-listing merge or fuzzy matching.

---

## 9. Configuration Summary

| File | Purpose | Editable by hand? |
|---|---|---|
| `config/filters.json` | Search/matching criteria (price, bedrooms, area, etc.) | Yes |
| `config/preferences.json` | Scoring weights, keyword dictionaries, thresholds | Yes |
| `config/retention.json` | Stale-listing archival threshold (`stale_days`) | Yes |
| `.env` | Telegram secrets only | Yes (never committed; `.env.example` documents the keys) |

---

## 10. Current Limitations (through Phase 2)

**Implemented:** Funda scraping with Akamai bypass, dedup, Phase 1 configurable filtering, detail-page scraping, 12-criterion scoring with renormalization, Telegram notifications with score breakdowns, full-scan/delta-scan detection with first-run notification gating, and stale-listing archival.

**Known limitations / unfinished work:**
- **No deterministic ranking/ordering** of multiple matching listings within a single run — notifications are sent in the order listings are processed, not sorted by score.
- **Combined field values:** `parking_type` and `garage_type` can come back as combined `"TypeA + TypeB"` strings; scoring only considers the first segment (documented in `docs/site-notes/funda.md`).
- **`amenities` and `bathrooms` criteria were removed** from scoring — amenities because keyword extraction proved unreliable and required disproportionate maintenance effort; bathrooms because it showed zero differentiation across sampled listings. Both are historical, not currently used.
- **Manual-only operations** (`--backfill`, `--seed`) exist for rescoring or initial population but are not part of the scheduled run and must be triggered by hand.
- **Sequential, single-browser architecture** by design (VPS memory constraints) — detail-page fetches happen one at a time, so a run with many newly matching listings can take a while.
- **Scoring accuracy is an informal estimate (~70%)**, not something backed by systematic evaluation against a labeled dataset.
- **Reliance on a specific Akamai-bypass technique** (urllib fetch → `data:` URL → Playwright render) that could break if Funda changes its bot-detection approach; this has already required iteration and is tracked in the site-notes learning log.

### "Feature" spotlight: it's all in Dutch

Every field this system scrapes and scores — `Eigendomssituatie`, `Isolatie`, `Soort garage`, `Ligging tuin`, and friends — is parsed straight from Funda's **Dutch-language** page text, not an English translation. Field names, keyword dictionaries (`erfpacht`, `volle eigendom`, `dubbel glas`, `warmtepomp`, `noordwesten`...), and all the regex boundary-matching in `detail_scraper.py` are written directly against Dutch vocabulary and grammar.

This was not a deliberate localization decision — it happened because the scraper was pointed at funda.nl without an eye on the language toggle — but in the spirit of "it's not a bug, it's a feature": it does mean the extraction logic is closer to the source of truth (no lossy translation layer between Funda's actual field labels and the parser), at the cost of every keyword list, regex, and section heading needing to be authored and debugged in a second language along the way. Future maintainers editing `config/preferences.json`'s keyword dictionaries or `detail_scraper.py`'s field-matching patterns should keep this in mind — contributions in English will need a Dutch pass before they'll match anything on the actual page.

---

## 11. Future Expansion: Toward a Public/User-Facing Bot

The current system is built for a single owner with a single, hand-edited configuration. Turning it into a multi-user product would require several architectural changes:

**Hardcoded filters.** `config/filters.json` is a single, repository-wide file loaded once per run via `FilterConfig.from_file()`. This already proves filters *can* be config-driven rather than hardcoded in source — but it's still one file for one user. Supporting per-user price ranges, bedroom requirements, etc. would require either one `FilterConfig` per user persisted in the database (rather than a static file) or a `filters` table keyed by user ID, plus a way for `main.py` to iterate over users rather than loading a single global config.

**Hardcoded notification threshold.** The 70-point first-run/full-scan gating threshold is currently a literal constant (`gating_threshold = 70`) in `src/main.py`. For a public bot this would need to become either a global setting in a config file (simplest change) or, more usefully, a per-user preference stored alongside that user's filters — since different users will want different sensitivity to "how good does a score need to be before I'm bothered."

**Multi-user architecture.** Beyond filters and thresholds, supporting concurrent users would require: per-user Telegram chat IDs (today there is one `TELEGRAM_CHAT_ID` in `.env`); per-user `notified` state (today deduplication is global per `listing_id`, which would need to become per `(user_id, listing_id)`); some form of user identification/registration flow; safeguards so one user's filter change doesn't trigger full-scan gating or re-scraping for every other user; and consideration of Funda request-rate limits and anti-bot pacing across many users hitting the same source concurrently — the current single-browser, sequential, low-frequency design was built around one user's needs and would not scale directly to many.

None of the above is implemented today; this section describes the gap between the current single-user tool and a hypothetical future product, not planned work.

---

## 12. Project Structure

```
project-root/
├── AGENTS.md
├── product.md
├── architecture.md
├── Operations.md
├── README.md
├── .env                    # git-ignored
├── .env.example
├── config/
│   ├── filters.json
│   ├── preferences.json
│   ├── preferences-notes.md
│   └── retention.json
├── data/
│   └── funda.db            # git-ignored
├── logs/
│   ├── scraper.log         # git-ignored
│   └── cron.log            # git-ignored
├── docs/
│   ├── tasks/
│   └── site-notes/
│       └── funda.md
├── src/
│   ├── config.py
│   ├── scraper.py
│   ├── detail_scraper.py
│   ├── scoring.py
│   ├── storage.py
│   ├── notifier.py
│   └── main.py
└── tests/
```

---

## 13. Running the System

See `Operations.md` for the full reference (environment setup, cron schedule, CLI flags, troubleshooting order). In short:

```bash
source .venv/bin/activate
python -m src.main                 # normal run
python -m src.main --dry-run       # scrape/store/score, skip Telegram
python -m src.main --backfill      # rescore listings with score IS NULL
python -m src.main --seed          # full pipeline, no notifications, initial DB population
```

Production execution is intended to run via cron roughly every 5 hours, terminating after each run rather than staying resident.

---

## 14. Development / Maintenance Notes

- `AGENTS.md` is the authoritative behavioral contract for any AI coding agent (Gemini CLI, OpenCode CLI, or others) working on this repository — it governs Git workflow, resource limits, scope discipline (Funda only), and the mandatory Funda "Learning Loop" documentation requirement.
- Whenever a Funda scraper breakage is diagnosed and fixed, the fix must be logged in `docs/site-notes/funda.md` — read that file before debugging any extraction issue, since the fix may already be documented.
- Documentation (`product.md`, `architecture.md`, `Operations.md`) is treated as part of the implementation and must stay in sync with code changes; this README summarizes but does not replace them.

## Developers & Contributors:
Yousef Babaki: https://github.com/josebbk
Rashid Nazari: https://github.com/arashid02-n
