# architecture.md — Amsterdam Funda Home-Search Agent

## Overview

A Python-based scraper that runs periodically, extracts current for-sale
Amsterdam listings from Funda using Playwright, detects listings not seen
before, stores them in SQLite, applies the confirmed Phase 1 property filters
(€550,000–€750,000 asking price, ≥3 bedrooms, ≥100 m² living area), and sends
Telegram notifications for newly detected matching listings. As of Phase 2 the
filter values are configurable by editing `config/filters.json` (see
"Phase 2 — Configurable Search Filters"), with the Phase 1 values above
as the defaults.

The product-level requirements are defined in `product.md`.

---

## Shared Development Model

The project is developed by two developers who may use different AI coding
agents.

Possible development environments include:

* Gemini CLI
* OpenCode CLI

Both agents work against the same Git repository.

The repository documentation is the shared source of truth.

AI CLI choice must not change the project's architecture or product
requirements.

The developers use separate Linux users on the VPS to avoid mixing user-level
environments and permissions.

tmux is used to keep development and debugging sessions persistent.

---

## Tech Stack

| Component            | Choice                                                                     | Why                                                                                                                 |
| -------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Language             | Python 3.12, in the project `.venv`                                        | Already provisioned; strong scraping ecosystem                                                                      |
| Browser automation   | Playwright (Chromium, headless) + urllib-based HTML fetch               | Funda is JavaScript-rendered and uses Akamai bot-protection that blocks direct Playwright navigation from datacenter IPs. The scraper fetches page HTML with `urllib` using realistic browser headers, then loads it into Playwright via a `data:` URL for JavaScript rendering. `playwright-stealth` was tried but did not bypass Akamai. No virtual display (Xvfb) is required — native headless mode renders entirely in memory. |
| Storage              | SQLite                                                                     | Single-machine project; no database server required                                                                 |
| Notifications        | Telegram Bot API via direct HTTPS calls / suitable Python Telegram library | Bot + token already exist                                                                                           |
| Scheduling           | cron (Phase 1)                                                             | Simple periodic execution without a persistent application process                                                  |
| Secrets              | `.env` + `python-dotenv`                                                   | Keeps Telegram credentials out of Git                                                                               |
| Terminal persistence | tmux                                                                       | Keeps development/manual execution sessions alive across SSH disconnects                                            |
| Version control      | Git/GitHub                                                                 | Shared repository and collaboration between developers                                                              |

---

## Playwright & Browser Dependency Rule

**Playwright and browser binaries (Chromium) must ONLY be installed when explicitly requested by a task.**

* Do not install Playwright, Chromium, or OS-level browser dependencies during initial environment setup or as speculative preparation for future tasks.
* Before installing browser dependencies, agents must confirm that the task explicitly requires installation and that the system remains within the 4GB VPS memory ceiling.
* Once installed, Playwright execution must be limited to a single browser instance at a time to prevent Out-Of-Memory (OOM) crashes.

---

## Scraping Strategy

### Why Playwright specifically

The current architecture uses Playwright/Chromium because Funda's pages rely
on browser-side rendering and because a browser-based approach is more
appropriate for the site's behavior than assuming a simple `requests` +
BeautifulSoup implementation will always work.

The project does not assume that an internal Funda API exists or will remain
stable.

Playwright should therefore be treated as the current architectural choice,
subject to real-world testing.

---

## Anti-Bot Considerations

No paid proxy or CAPTCHA-solving service is permitted without explicit
approval.

Mitigation should rely on low-impact behavior:

* realistic pacing
* randomized delays where appropriate
* low-frequency scheduled runs
* one browser instance at a time
* persistent browser context where technically appropriate
* avoiding aggressive retries
* avoiding unnecessary parallelism

If Funda blocks or challenges the scraper:

1. Do not aggressively retry.
2. Diagnose the behavior.
3. Record the issue in `docs/site-notes/funda.md`.
4. Do not silently introduce a paid workaround.

---

## Scrape Flow

A normal scraper run should follow this general sequence:

1. Start a single Chromium instance via Playwright.
2. Navigate to the Funda Amsterdam for-sale search results.
3. Process the result pages incrementally.
4. Extract listing data defined in `product.md`.
5. Derive the unique listing ID from the Funda URL.
6. Check whether the listing already exists in SQLite.
7. Insert new listings.
8. For newly detected listings, apply the confirmed Phase 1 filter criteria.
9. Queue matching listings for Telegram notification.
10. Send queued notifications after scraping is complete.
11. Record useful run statistics.
12. Close the browser and exit.

Scraping and notification should remain separate phases of a run so that
scraping failures and notification failures are easier to diagnose.

### Pagination — dynamic page count

The scraper no longer scrapes a fixed number of pages.  Before scraping,
it fetches page 1 and extracts the total listing count from the rendered
page text (the "N koopwoningen" line).  The number of pages to scrape is
computed as `ceil(total_count / 15)` and capped at the `max_pages`
argument.  If the computed page count exceeds `max_pages`, a **WARNING**
is logged (instead of INFO) to make safety-ceiling hits visible.  If the
count cannot be extracted, the scraper falls back to the caller-provided
`max_pages` unchanged — this preserves existing behaviour for that
fallback path.  See `scraper.py::scrape_funda` and
`scraper.py::extract_total_listing_count` for implementation details.

---

## Scan Mode — Full Scan vs Delta Scan

The normal (non-backfill, non-seed) run path uses a **scan-mode** decision
to choose between a full scan and a delta scan.  This affects three things:

1. Which listings are scraped (publication-date filter)
2. How many pages are scraped (page-count ceiling)
3. Whether first-run notification gating is applied

### Triggering conditions

At the top of `main()`, `run_start` is computed **once** (before any
"now" reference).  Then two independent checks run:

1. **Filter-change detection:** `get_filter_snapshot(db_path)` returns the
   previously saved `FilterConfig.__dict__`.  If the snapshot is `None`
   (never saved) or differs from the currently loaded filters,
   `is_first_run_after_filter_change` is `True`.

2. **Staleness detection:** `get_last_successful_run(db_path)` returns an
   ISO-8601 UTC timestamp of the last successful run.  If it is `None`
   (never recorded) or if `(run_start - parsed_timestamp) > timedelta(days=3)`,
   `is_stale_fallback` is `True`.

```python
run_is_full_scan = is_first_run_after_filter_change or is_stale_fallback
```

### Four parameter combinations

| `is_first_run_after_filter_change` | `is_stale_fallback` | `run_is_full_scan` | `publication_date_days` | `max_pages` | Gating |
|---|---|---|---|---|---|
| True | False | **Full** | `None` (all dates) | 5 | **Enabled** |
| False | True | **Full** | `None` (all dates) | 5 | **Enabled** |
| True | True | **Full** | `None` (all dates) | 5 | **Enabled** |
| False | False | **Delta** | 3 (last 3 days) | 15 | Disabled |

### Safety-ceiling logging

When the computed page count (from total listing count) exceeds the
`max_pages` argument, `scraper.py` logs a **WARNING** instead of INFO:

```
Total listing count: 120 → computed pages: 8, TRUNCATING to max_pages=5 (safety ceiling hit).
```

This makes it obvious if the 5-page full-scan cap or 15-page delta-scan
ceiling is ever actually being hit.

### Notification gating

When `run_is_full_scan` is `True`, the existing first-run notification
gating (70-point score threshold for newly inserted listings) is applied.
This replaces the previous condition which gated only on
`is_first_run_after_filter_change`.  Gating mechanics are unchanged:

* `score >= 70` → send notification, set `notified = 1`
* `score < 70` → suppress notification, set `notified = 1`

When `run_is_full_scan` is `False` (delta scan), all matching unnotified
listings are notified normally — no gating.

### `last_successful_run` write timing

`save_last_successful_run()` is called **only** at the point where the run
has genuinely completed successfully: after the final summary logging, and
**only** if `stats["notifications_failed"] == 0`.  The timestamp used is
`datetime.now(timezone.utc)` (actual completion time), not the original
`run_start`.  This timestamp is never written inside
`_send_failure_alert_and_exit()` or on any early-exit failure path (DB init
failure, scrape failure, 0-listings failure, required-field failures).

### Backfill and seed runs

`_run_backfill()` and `_run_seed()` are **not** modified by this scan-mode
logic.  They continue to call `scrape_funda(..., max_pages=5)` with no
`publication_date_days` argument, exactly as before.

---

## Phase 2 — Detail-Page Scraping & Scoring

After Phase 1 filtering identifies matching listings, each listing
undergoes a detail-page fetch and preference-based scoring before
notification.

### Data flow

```
Phase 1 filter match
        ↓
   fetch_listing_details(url)
        ↓
   merge detail fields INTO listing dict
        ↓
   score_listing(listing, preferences, filter_config)
        ↓
   persist detail fields + score to DB row
        ↓
   notification with score breakdown
```

**Important:** Detail fields are merged into the listing dict (`listing.update(detail)`),
not the other way around (`detail.update(listing)`). The listing dict comes from the
database row (`dict(row)`), which contains all columns including phase2 fields that are
NULL. If `detail.update(listing)` were used instead, the NULL DB columns would overwrite
the scraped detail values, resulting in all detail fields being NULL in the database.

### Scoring criteria

The scoring system implements **9 weighted criteria**:

| # | Criterion | Score source | Data source |
|---|-----------|-------------|-------------|
| 1 | `neighborhood_value` | `_score_neighborhood_value` | `detail` (price, living_area_m2, neighborhood_avg_price_m2) |
| 2 | `ownership` | `_score_ownership` | `detail` (ownership_type, erfpacht_canon_annual) |
| 3 | `energy_label` | `_score_energy_label` | `detail` (energy_label) |
| 4 | `living_area` | `_score_living_area` | `detail` (living_area_m2) + `filter_config` (living_area_min) |
| 5 | `construction_condition` | `_score_construction` | `detail` (year_built, insulation_score) |
| 6 | `parking` | `_score_parking` | `detail` (parking_type) |
| 7 | `rooms` | `_score_rooms` | `detail` (rooms) + `filter_config` (bedrooms_min) |
| 8 | `bathrooms` | `_score_bathrooms` | `detail` (bathrooms) |
| 9 | `garden` | `_score_garden` | `detail` (garden_present, garden_size_m2, garden_orientation) |

#### New criteria: living_area and rooms

Two new scoring functions were added in the Phase 2 expansion:

- **`_score_living_area(detail, filter_config)`** — linear scale between the
  configured living-area minimum (floor → 0.0) and a cap (cap → 1.0). When only
  a minimum is configured (no maximum), cap = floor + 100. If no living-area
  filter is configured at all, the criterion returns `None` and is excluded from
  scoring. Threshold defaults documented in
  `config/preferences.json` → `living_area_thresholds`.

- **`_score_rooms(detail, filter_config)`** — linear scale between the
  configured bedrooms minimum (floor → 0.0) and cap = max(8, floor + 4).
  Threshold defaults documented in
  `config/preferences.json` → `rooms_thresholds`.

Both functions accept `filter_config` as an explicit parameter, reading the
current filter thresholds from `config/filters.json` via `FilterConfig.from_file()`.
This creates a dependency on the co-worker's `FilterConfig` work in
`src/config.py` / `src/storage.py` / `src/main.py`, which is already merged
into this branch.

#### Filter-config dependency

The `score_listing()` function signature is:

```python
score_listing(detail: dict, preferences: dict | None = None,
              filter_config: FilterConfig | None = None) -> ScoreResult
```

When `filter_config` is `None`, it loads from `config/filters.json` as a
fallback. In production (`main.py`), the `filters` object loaded at run start
is passed explicitly, ensuring scoring uses the same filter values as the
Phase 1 filter matching.

### New modules

* **`src/detail_scraper.py`** — Fetches and parses a single Funda listing
  detail page. Reuses the same `urllib` → `data:` URL → Playwright
  rendering technique from `scraper.py` to bypass Akamai bot-protection.
  Returns a dict with all detail fields (Section 3 of the spec). Any
  field that cannot be parsed is set to `None` — never omitted, never
  guessed.

* **`src/scoring.py`** — Scores a listing's detail data against user
  preferences loaded from `config/preferences.json`. Implements the
  renormalization algorithm: when a criterion has no data, it is
  excluded from the weighted average rather than penalized. Returns a
  `ScoreResult` with score (0–100), breakdown, confidence flag, and
  missing criteria list.

* **`config/preferences.json`** — Hand-editable weights + keyword
  dictionaries. Weights sum to 100 by convention.

### Keyword-dictionary-over-enum design decision

Fields like `Voorzieningen`, `Isolatie`, `Soort garage`, and
`Soort parkeergelegenheid` are **free text written by individual listing
agents**, not a fixed set of values. Verified against 3 real listings
(Amsterdam, Mill, Wijchen): `Voorzieningen` alone produced three
completely non-overlapping lists across three listings.

Therefore, the scoring uses **substring/keyword matching** against the
dictionaries in `config/preferences.json`, case-insensitive, and ignores
anything not in the dictionary rather than failing.

Fields confirmed genuinely structured (safe for direct parsing):
`Energielabel` (ordinal), `Bouwjaar` (int), `Vraagprijs`,
`Vraagprijs per m²`, all `Oppervlakten` fields,
`Aantal kamers`/`Aantal badkamers` (consistent phrasing, regex-safe).

### Schema additions

New nullable columns on `listings` (added via `ALTER TABLE`):

```
ownership_type TEXT
erfpacht_canon_annual REAL
garden_present INTEGER
garden_type TEXT
garden_size_m2 INTEGER
garden_orientation TEXT
balcony_present INTEGER
building_bound_outdoor_m2 INTEGER
garage_type TEXT
parking_type TEXT
insulation_raw TEXT
insulation_score REAL
heating_type TEXT
boiler_year INTEGER
bathrooms INTEGER
neighborhood_avg_price_m2 REAL
score INTEGER
score_breakdown TEXT
score_confidence TEXT
detail_fetched_at TEXT
```

### Browser usage

Each detail-page fetch creates its own Playwright browser instance,
reusing the same Akamai-bypass technique. Detail fetching happens
sequentially after the main scrape completes, so no concurrent browser
instances are ever open.

### Notification format

> **Superseded (Task 3):** The following format is preserved for historical
> context. See the current format below for the live specification.

```
🏠 {address} — €{price} — {living_area}m² — {bedrooms} bed
Score: {score}/100{confidence_flag}
  ✓ neighborhood_value: 18/22
  ✗ ownership: 0/15
🔗 {url}
```

The confidence flag shows `"⚠ partial data ({missing criteria})"` when
data is incomplete, and `Score: unavailable` when no scoring data is
available.

#### Current format (Task 3 — redesigned)

The notification message was redesigned to provide a structured, scannable
score breakdown. The format is:

```
🏠 {address}
€{price} · {living_area} m² · {bedrooms} bedrooms
{property_type} · {neighborhood}

⭐ {score}/100
⚠️ Adjusted · {missing criteria} data unavailable

🟢 Best
• {Criterion} — {earned}/{possible}
• {Criterion} — {earned}/{possible}
• {Criterion} — {earned}/{possible}

🔴 Weakest
• {Criterion} — {earned}/{possible}
• {Criterion} — {earned}/{possible}
• {Criterion} — {earned}/{possible}

📊 Full score breakdown
{Criterion} {earned}/{possible} · {Criterion} {earned}/{possible}
{Criterion} {earned}/{possible} · {Criterion} {earned}/{possible}
...

{Criterion} N/A

🔗 {url}
```

**Rules:**

* **Header** — address on line 1, price/area/bedrooms on line 2,
  property type and neighborhood on line 3.
* **Score** — `⭐ {score}/100` with bold score value.
* **Adjusted line** — shown only when `score_confidence == "partial"`,
  listing the display names of criteria with `matched: false`.
  Omitted entirely when confidence is `"full"`.
* **No-data** — when `score_confidence == "no_data"` or `score` is `None`,
  shows `Score: unavailable` (existing convention).
* **Best / Weakest** — top 3 and bottom 3 matched criteria sorted by
  `points_earned` (absolute contribution to total score).
  If ≤ 3 matched criteria, only Best is shown.
  If 4–6 matched criteria, Best (top 3) and Weakest (bottom 3) may overlap.
* **Full score breakdown** — every criterion from `config/preferences.json`
  weights listed two per line, with `N/A` for unmatched criteria.
* **Criterion display names** — raw keys (e.g. `neighborhood_value`) are
  mapped to human-readable labels (e.g. "Neighborhood") in
  `notifier.py::_CRITERION_LABELS`. This mapping is presentation-only.

### Notification score threshold

A `notification_score_threshold` value (default 80) is configured in
`config/preferences.json`. During the backfill run:

* Listings with `score >= threshold` receive a Telegram notification and
  have `notified = 1` set.
* Listings with `score < threshold` have `notified = 1` set but do NOT
  receive a notification. This prevents them from re-entering the
  notification flow through unrelated triggers (e.g. a future price
  change that resets `notified = 0`).

This threshold is only applied during backfill. The normal run path
(not yet implemented with scoring-based filtering) does not use this
threshold — all matching listings are notified regardless of score.

### First-run-after-filter-change notification gating (Task 2 — generalized)

> **Superseded (Task 4):** The gating condition was generalized from
> `is_first_run_after_filter_change` to `run_is_full_scan`.  The mechanics
> (70-point threshold, newly-inserted-only, suppressed listings marked
> notified=1) are unchanged.  See "Scan Mode — Full Scan vs Delta Scan"
> above for the full scan-mode logic.

When `config/filters.json` is edited, the next scraper run must not
notification-blast every listing that suddenly matches the new criteria.

**Detection:** a `scraper_metadata` table (created via `CREATE TABLE IF NOT
EXISTS`) stores the previous filter snapshot as JSON under the key
`filter_snapshot`.  At run start, `get_filter_snapshot()` compares the
stored snapshot against the currently loaded `FilterConfig.__dict__`.  If
absent or different, `is_first_run_after_filter_change` is `True`.

**Gating logic (in `main.py`):**

1. After the normal scrape → insert → detail-page fetch → scoring flow,
   the notification loop checks each scored listing.
2. For listings where `newly_inserted` is `True` **and** `run_is_full_scan`
   is `True` (filter change **or** staleness fallback):
   * `score >= 70` → send notification, set `notified = 1`
   * `score < 70` → suppress notification, set `notified = 1`
3. After the notification pass completes, `save_filter_snapshot()` writes
   the current `FilterConfig.__dict__` to the metadata table.

**The 70-point threshold** is a fixed value used only for full-run gating.
It does not alter `score_listing()`, scoring weights, score persistence,
or normal scoring behaviour.  The delta-scan run path (non-full-run)
notifies all matching listings through the existing workflow regardless of
score.

**Storage:** the `scraper_metadata` table has two columns:

```
key      TEXT PRIMARY KEY
value    TEXT NOT NULL
```

The `get_filter_snapshot()` function reads the JSON-encoded `FilterConfig`
from this table; `save_filter_snapshot()` writes it.  This follows the same
pattern used for Phase 2 schema migrations (`ALTER TABLE` / `CREATE TABLE
IF NOT EXISTS`).

---

## Phase 2 — Configurable Search Filters

Phase 2 makes the search filter criteria configurable at runtime while
preserving the Phase 1 behavior as the default. The owner edits a single
human-readable JSON file — `config/filters.json` — instead of `.env`. The
frozen contract is implemented across four files: `config/filters.json`
(user-editable values), `src/config.py` (single source of truth for filter
defaults and loading), `src/storage.py` (applies the filters in the matching
query), and `src/main.py` (loads and threads the configuration through the
run).

```text
config/filters.json
        ↓
src/config.py   (FilterConfig.from_file)
        ↓
src/main.py
        ↓
src/scraper.py / src/storage.py
```

`.env` is reserved for secrets and environment-specific sensitive values
(Telegram credentials) and is no longer used for search filters.

### `config/filters.json` — user-editable filter file

The default file is committed with the Phase 1 values:

```json
{
  "price_min": 550000,
  "price_max": 750000,
  "bedrooms_min": 3,
  "living_area_min": 100,
  "property_type": null,
  "plot_size_min": null,
  "energy_label_min": null
}
```

| Key               | Type       | Default | Meaning                                     |
| ----------------- | ---------- | ------- | ------------------------------------------- |
| `price_min`       | int        | 550000  | Minimum asking price (€)                    |
| `price_max`       | int        | 750000  | Maximum asking price (€)                    |
| `bedrooms_min`    | int        | 3       | Minimum bedrooms                            |
| `living_area_min` | int        | 100     | Minimum living area (m²)                    |
| `property_type`   | str / null | none    | Required property type, e.g. `appartement`  |
| `plot_size_min`   | int / null | none    | Minimum plot size (m²)                      |
| `energy_label_min`| str / null | none    | Minimum energy label, e.g. `B`              |

The `null` optional values mean "no preference filter". Missing keys fall
back to the defaults shown above. Unknown keys are rejected so a typo cannot
silently change behavior.

### `src/config.py` — FilterConfig

```python
@dataclass(frozen=True)
class FilterConfig:
    price_min: int
    price_max: int
    bedrooms_min: int
    living_area_min: int
    property_type: str | None = None
    plot_size_min: int | None = None
    energy_label_min: str | None = None
```

* `FilterConfig` is immutable (`frozen=True`); validation runs at
  construction in `__post_init__`.
* `DEFAULT_FILTERS` is a module-level `FilterConfig` holding the Phase 1
  defaults (`price_min=550000`, `price_max=750000`, `bedrooms_min=3`,
  `living_area_min=100`, all optional preferences `None`). It is the single
  source of truth for filter defaults.
* `FilterConfig.from_file()` loads `config/filters.json` from a
  project-root-relative path, so execution from cron, systemd, or tmux does
  not depend on the process working directory. Missing required keys fall
  back to the Phase 1 defaults; missing optional keys become `None`; unknown
  keys and invalid values raise a clear `ValueError` rather than being
  silently coerced.

Validation rules:

* integer fields must be integers (floats and booleans rejected); invalid
  values raise `ValueError`
* `price_min <= price_max`
* numeric minimums (`price_min`, `bedrooms_min`, `living_area_min`,
  `plot_size_min`) must be non-negative
* `property_type`, when set, must be a non-empty string
* `energy_label_min`, when set, must be a non-empty string and is normalized
  to uppercase (e.g. `b` → `B`), following the `scoring.py` convention
* the file must be a JSON object; invalid JSON or an unreadable/missing file
  raises `ValueError`

### `src/storage.py` — parameterized matching query

```python
fetch_unnotified_matching_listings(
    db_path: Path | str = DEFAULT_DB_PATH,
    filters: FilterConfig = DEFAULT_FILTERS,
) -> list[dict]
```

* The signature is backward compatible — existing callers that pass only
  `db_path` keep the Phase 1 behavior via `DEFAULT_FILTERS`.
* The base conditions are always applied: `notified = 0`, price within
  `[price_min, price_max]`, `bedrooms >= bedrooms_min`, and
  `living_area_m2 >= living_area_min`.
* Optional preferences are applied only when not `None`:
  * `property_type` → `property_type = ?` (exact match)
  * `plot_size_min` → `plot_size_m2 >= ?`
  * `energy_label_min` → `UPPER(energy_label) IN (…)` where the accepted set
    is every label at least as good as the minimum on the project
    energy-label scale (`config/preferences.json` → `energy_label_scale`,
    `["G","F","E","D","C","B","A","A+","A++","A+++","A++++"]`, worst → best).
    A configured minimum that is not on the scale raises `ValueError`.
* NULL semantics: a listing with a NULL value for an optional field never
  satisfies an enabled preference filter (standard SQL comparison).

### `src/main.py` — orchestration integration

* `main()` loads `filters = FilterConfig.from_file()` once at run start.
* The configured `price_min`, `price_max`, `living_area_min`, and
  `bedrooms_min` are passed to `scrape_funda(...)`.
* The same `filters` object is passed to
  `fetch_unnotified_matching_listings(db_path, filters=filters)`, so the
  storage query uses exactly the loaded configuration.
* The optional preferences (`property_type`, `plot_size_min`,
  `energy_label_min`) are storage-level filters and are **not** passed to
  `scrape_funda()`, which does not accept those parameters.
* All Phase 1 orchestration (init_db, insert, notify, mark-as-notified,
  dry-run, exit codes, logging) is unchanged.

---

## Phase 1 Filtering Criteria

The confirmed Phase 1 filtering criteria serve as the default source of truth for filter defaults across all project documentation:

* **Price:** €550,000–€750,000 (Confirmed)
* **Bedrooms:** ≥3
* **Living area:** ≥100 m²

These values are also the Phase 2 defaults (`DEFAULT_FILTERS` in
`src/config.py` and the committed `config/filters.json`) and remain in effect
whenever a filter key is absent from the filter file.

### Resolution of Legacy Requirements

The price range has been explicitly confirmed as **€550,000–€750,000** by the project owner. This range remains the default scraper behavior, configurable by editing `price_min` / `price_max` in `config/filters.json`.

---

## Data Storage

### Recommendation: SQLite

SQLite is appropriate for this project because it is:

* file-based
* easy to back up
* zero-server
* suitable for a single scraper process
* sufficient for Phase 1 data volumes
* useful for future querying and historical analysis

The database is expected to live at:

```text
data/funda.db
```

---

## Database Schema

### `listings`

| Column           | Type              | Notes                                             |
| ---------------- | ----------------- | ------------------------------------------------- |
| `listing_id`     | TEXT, PRIMARY KEY | Derived from Funda listing URL; deduplication key |
| `url`            | TEXT NOT NULL     | Full Funda listing URL                            |
| `address`        | TEXT NOT NULL     | Listing address                                   |
| `neighborhood`   | TEXT NOT NULL     | Neighborhood where available                      |
| `price`          | INTEGER NOT NULL  | Asking price in EUR                               |
| `living_area_m2` | INTEGER NOT NULL  | Living area in m²                                 |
| `bedrooms`       | INTEGER NOT NULL  | Number of bedrooms                                |
| `plot_size_m2`   | INTEGER           | Nullable                                             |
| `rooms`          | INTEGER           | Number of rooms; NULL (only available on detail pages) |
| `property_type`  | TEXT              | Property type; NULL when not derivable from the card |
| `year_built`     | INTEGER           | Nullable                                             |
| `energy_label`   | TEXT              | Nullable                                             |
| `status`         | TEXT              | Available / under offer / sold / etc.; NULL at card level |
| `first_seen_at`  | TEXT              | ISO 8601 timestamp                                |
| `notified`       | INTEGER           | Boolean: 0/1                                      |

### Required fields enforcement (supersedes earlier "bedrooms nullable" decision)

The six fields `url`, `address`, `neighborhood`, `price`, `living_area_m2`, and
`bedrooms` are **required** — listings missing any of these are discarded and
the entire scraper run is treated as failed (exit code 1, Telegram failure
alert sent), same as the "0 listings / possible block" case.

This supersedes the earlier decision in this document that marked `bedrooms` as
nullable and that missing required fields should be silently logged and skipped.
The prior "discard silently, just log" behavior was replaced because these six
fields are the basis of Phase 1 filtering; if they cannot be extracted it means
the scraper's extraction logic is broken (e.g. stale selectors or Funda HTML
changes), and silently discarding listings hides this failure from the operator.
A run that silently discards listings gives a false sense of health. The new
behavior ensures that extraction failures are surfaced immediately via the
existing cron failure-alert Telegram mechanism.

The remaining fields (`plot_size_m2`, `rooms`, `year_built`, `energy_label`,
`status`) stay nullable — they are supplementary and do not affect filtering.

---

## Deduplication

The listing ID derived from the Funda URL is the primary deduplication key.

The intended logic is conceptually:

```text
listing encountered
       ↓
listing_id already exists?
       ├── no → insert as new listing
       │           ↓
       │     evaluate filters
       │           ↓
       │      if matching
       │           ↓
       │      queue notification
       │
└── yes → update all fields,
                    leave notified untouched
```

The database should not generate duplicate new-listing notifications merely
because a listing appears in multiple scheduled runs.

The `insert_listing` function in `storage.py` returns a status per listing:
`"inserted"`, `"updated_unchanged"`, or `"unchanged"`.
`main.py` uses these to track distinct counts of new and updated listings
in the run summary.

> **Superseded (Task 1):** The return value `"updated_renotify"` was removed.
> It previously indicated that price or status had changed and `notified` was
> reset to 0. See the superseded section below for the previous logic.

### Database update semantics (detail-page field preservation)

The `insert_listing()` function is the single convergence point for all
listing data from all callers: the card/results-page scraper, the
detail-page scraper, the backfill run, and the seed run.

**Key principle:** When updating an existing listing, detail-page fields
that are not present in the incoming data must NOT be overwritten with
NULL. This prevents card-level re-inserts from erasing previously stored
detail-page data.

**How it works:**

1. Card-level scrapes (`scraper.py::_extract_listing_data`) return a dict
   with card fields explicitly set (including `"rooms": None` and
   `"year_built": None`) and never include any of the 20 Phase 2
   detail fields.

2. Detail-page scrapes (`detail_scraper.py::fetch_listing_details`) return
   a dict via `DetailData.to_dict()` which filters out None values —
   absent detail fields are indistinguishable from "not scraped" at the
   `insert_listing()` call boundary.

3. In `storage.py::insert_listing()`, for existing listings, a preservation
   loop checks all detail and shared optional fields. When an existing DB
   value is non-None and the incoming data is None, the existing value is
   preserved rather than overwritten.

**Protected fields** (preserved when incoming data is None):
`rooms`, `year_built`, `plot_size_m2`, `property_type`, `energy_label`,
and all 20 Phase 2 fields (`ownership_type`, `erfpacht_canon_annual`,
`garden_present`, `garden_type`, `garden_size_m2`, `garden_orientation`,
`balcony_present`, `building_bound_outdoor_m2`, `garage_type`,
`parking_type`, `insulation_raw`, `insulation_score`, `heating_type`,
`boiler_year`, `bathrooms`, `neighborhood_avg_price_m2`, `score`,
`score_breakdown`, `score_confidence`, `detail_fetched_at`).

**Unprotected fields** (freely overwritten on every run):
`url`, `address`, `neighborhood`, `price`, `living_area_m2`, `bedrooms`.
These are card-level fields that must always reflect the latest scrape.

**Status special case:** The `status` field has its own preservation
logic (checked before the general preservation loop) because a None
status from the card scraper is an artifact of the data source, not a
real status change. The general preservation loop also covers status
as a safety net.

**Implications for main.py step 4.5:** When `listing.update(detail)`
merges detail fields into a listing dict (which originates from a DB row
and contains all phase2 columns), detail fields that are None on the
detail page are filtered out by `to_dict()`. The listing dict retains
its card-level None values for those fields, and the preservation logic
in `insert_listing()` correctly preserves the existing DB values.

---

## Known SQLite Limitations

SQLite locks the database during writes.

This is acceptable for the initial architecture because:

* there is one main scraper process
* execution is periodic
* data volumes are expected to be moderate

It could become a limitation if multiple independent processes start writing
to the database concurrently.

There is currently no need to introduce PostgreSQL.

SQLite also does not provide built-in remote backup/replication. Backup
strategy can be revisited if the project's operational requirements grow.

---

## Scheduling & Execution

### Phase 1 recommendation: cron

> **Superseded (Task 5):** The schedule was changed from approximately every
> 30 minutes to approximately every 5 hours.

The initial scheduling recommendation is approximately every 5 hours.

Reasoning:

* frequent enough to provide useful monitoring
* less aggressive than continuous polling
* compatible with a single-browser sequential architecture
* easy to change later

The scraper should run as a normal scheduled process rather than remaining
permanently resident.

The `agent-work` tmux session is for AI-assisted development.

The `scraper` tmux session is reserved for manual execution and debugging.

Production scheduling should not depend on either tmux session.

---

## Notifications — Telegram

The project uses an existing Telegram bot.

Credentials are stored in `.env`.

Expected environment variables:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

`.env` must never be committed to Git.

A `.env.example` should document the required variable names without exposing
real credentials.

### Notification behavior

For every newly detected listing that matches all confirmed Phase 1 criteria,
the system should queue a Telegram notification.

The message should contain at minimum:

* address
* price
* living area
* rooms
* Funda URL

Notifications should be sent after the scraping phase has completed.

This separation keeps the scraper and notification logic easier to test and
debug independently.

#### Re-notification on price or status change

> **Superseded (Task 1):** The following behavior was removed. It is preserved
> here for historical context. See `product.md` §"Re-notification on price or
> status change (superseded)" for the product-level impact.

When `insert_listing` encounters an existing listing, it compares the new
scraped price and status against the stored values:

* If **price or status changed**: the listing row is updated, `notified` is
  reset to 0, and the listing re-enters the filter/notification flow on the
  current run. If it matches the Phase 1 criteria, a new notification is
  sent.
* If **neither price nor status changed**: other fields are updated but
  `notified` is left as-is. No re-notification is triggered.

This means the system can alert the owner about a previously-seen listing
that becomes relevant (e.g. a price drop into the target range) without
generating duplicate notifications for unchanged listings.

#### Current behavior (post-Task 1)

`insert_listing` no longer compares price or status to decide whether to
reset `notified`.  When an existing listing is updated, `notified` is always
preserved as-is.  `notified` is only modified by `mark_as_notified()` or
the filter-change logic (Task 2).

---

## Secrets Management

Secrets must remain outside version control.

Expected local file:

```text
.env
```

Expected committed example:

```text
.env.example
```

Actual credentials must never appear in:

* source code
* Git commits
* logs
* documentation
* AI prompts
* screenshots shared with developers

---

## Project Structure

The proposed structure is:

```text
project-root/
├── AGENTS.md
├── product.md
├── architecture.md
├── operations.md
├── .env                    # git-ignored
├── .env.example
├── .gitignore
├── requirements.txt
├── data/
│   └── funda.db            # git-ignored
├── logs/
│   ├── scraper.log         # git-ignored
│   └── cron.log            # git-ignored
├── docs/
│   └── site-notes/
│       └── funda.md
├── src/
│   ├── config.py
│   ├── scraper.py
│   ├── storage.py
│   ├── notifier.py
│   └── main.py
└── tests/
```

This is a proposed Phase 1 structure and should not be treated as evidence
that every file already exists.

---

## Logging & Error Handling

Each run should record at minimum:

* number of listings scraped
* number of new listings
* number of listings matching the filter
* number of notifications sent
* errors encountered
* useful timing information where appropriate

### Funda parsing failures

If a Funda page structure, selector, or extraction assumption breaks and the
issue is diagnosed and fixed, the learning must be recorded in:

```text
docs/site-notes/funda.md
```

This is mandatory according to `AGENTS.md`.

### Full run failures

A complete run failure should be logged clearly enough to identify whether
the failure came from:

* Funda navigation
* browser startup
* parsing
* SQLite
* filtering
* Telegram
* network behavior
* scheduling/environment

Exact log rotation and monitoring behavior belongs to `operations.md`.

---

## Resource Constraints

The VPS has approximately:

```text
4GB RAM
2GB swap
```

The architecture therefore intentionally uses:

* Playwright/Chromium installation deferred until explicitly required by a task
* one browser instance at a time
* sequential processing
* SQLite instead of a database server
* periodic process execution instead of a permanently running browser

Avoid unnecessary concurrency.

---

## Open Decisions

The following decisions remain open or require further testing:

### 1. Phase 1 Price Range (Resolved)
* **Status:** Resolved / Confirmed.
* **Value:** €550,000–€750,000.

### 2. Exact Playwright behavior — RESOLVED (updated 2026-08-13)

Original open question: the project should determine through real-world testing whether standard Playwright behavior is sufficient for Funda, and stealth plugins were not to be introduced without a demonstrated technical need and explicit approval.

Resolution: standard headless Playwright alone was not sufficient — Akamai bot-protection blocks direct Playwright navigation from datacenter IPs. `playwright-stealth` was tested but also failed to bypass Akamai. The working approach is a two-step process: (1) fetch page HTML with `urllib` using realistic browser headers (`Sec-Fetch-*`, `Accept-Language`, etc.), then (2) load the HTML into Playwright via a `data:` URL for JavaScript rendering. This bypasses Akamai because the HTTP request comes from `urllib` (not Playwright's Chromium). See `docs/site-notes/funda.md` for details.

Do not introduce stealth plugins or other anti-detection tooling unless a
specific technical need is demonstrated and the dependency is approved.

### 3. Exact logging and monitoring

The exact logging destination, rotation, and failure-monitoring mechanism
remain an `operations.md` decision.

### 4. Phase 2 filter configuration — RESOLVED

Filter thresholds are now configurable by editing `config/filters.json`,
loaded via `src/config.py` (`FilterConfig`, `DEFAULT_FILTERS`,
`FilterConfig.from_file()`), with the Phase 1 values preserved as defaults
(see "Phase 2 — Configurable Search Filters" above). No configuration
framework or new dependency was introduced; `.env` remains reserved for
secrets.

*Note:* Ranking/scoring configuration is resolved via
`config/preferences.json` (see "Phase 2 — Detail-Page Scraping & Scoring"
above). This is distinct from the filter-threshold configuration. Do not
conflate the two.

### 5. Scheduling evolution

cron is the Phase 1 starting point.

A more robust scheduler can be considered in a later phase if operational
experience demonstrates a need.

### 6. Publication-date filter capability (Task 3 — implemented, wired)

`build_search_url()` and `scrape_funda()` in `scraper.py` accept an optional
`publication_date_days` parameter (values: 1, 3, 5, 10, 30, or None). When
None the URL output is byte-for-byte identical to the pre-existing behaviour.
Invalid values raise `ValueError`.

This capability **is wired into the normal run path** in `main()`.  The
scan-mode decision selects the parameter:

* **Full scan** (`run_is_full_scan == True`): `publication_date_days = None`
  (no publication filter, matches existing behaviour).
* **Delta scan** (`run_is_full_scan == False`): `publication_date_days = 3`
  (only listings published within the last 3 days).

The parameter is placed between `price` and `floor_area` in the URL
parameter order, matching the confirmed real Funda URL example. Query
parameter order on Funda is treated as order-independent (Funda's search
endpoint does not depend on parameter ordering).

---

## Architectural Principles

The project should follow these principles:

1. Keep the architecture simple.
2. Build only what Phase 1 requires.
3. Funda is the only supported scraping target.
4. Avoid paid infrastructure.
5. Avoid unnecessary concurrency.
6. Keep resource usage compatible with the 4GB VPS (including deferred browser installation).
7. Separate scraping, storage, filtering, notification, and orchestration.
8. Keep secrets outside Git.
9. Preserve project knowledge in documentation.
10. Keep documentation synchronized with implementation.
11. Do not silently resolve contradictory requirements.
12. Ensure Gemini CLI and OpenCode CLI can continue the same project through
    the shared repository and documentation.
