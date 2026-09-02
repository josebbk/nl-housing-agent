# architecture.md — Amsterdam Funda Home-Search Agent

## Overview

A Python-based scraper that runs periodically, extracts current for-sale
Amsterdam listings from Funda using Playwright, detects listings not seen
before, stores them in SQLite, applies the confirmed Phase 1 property filters
(€550,000–€750,000 asking price, ≥3 bedrooms, ≥100 m² living area), and sends
Telegram notifications for newly detected matching listings. As of Phase 2 the
filter values are configurable by editing `config/filters.json` (see
"Phase 2 — Configurable Search Filters"), with the Phase 1 values above
shipped as the starting values in that file.

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

### Neighborhood extraction

The `neighborhood` field is sourced in two stages:

1. **Card level (`scraper.py`)** — populated from the URL slug
   (`/detail/koop/{city}/…`) so the required field is always present at
   the initial `insert_listing()` call, e.g. `purmerend`.
2. **Detail page (`detail_scraper.py`)** — `_extract_neighborhood()`
   parses the detail page's address `<h1>` (street span → postal+city
   span → neighborhood `<a aria-label>`) and returns
   `"{neighborhood} - {postal code} {city}"` (e.g. `Amerika - 1448 XS
   Purmerend`). This value is merged over the card-level slug via the
   existing `listing.update(detail)` flow in
   `main.py::_score_and_persist_listing` and overwrites the stored
   `neighborhood`.

Required-field enforcement is unaffected: the card-level slug is always
present before any detail fetch, and when the detail-page header cannot
be parsed, `_extract_neighborhood()` returns `None` (omitted from the
detail dict via `DetailData.to_dict()`), leaving the card-level value in
place. `neighborhood` remains a freely-overwritable card-level field in
`storage.py::insert_listing()` — it is intentionally not in the
detail-field preservation list, so the enriched value overwrites the
slug on subsequent detail fetches.

### Scoring criteria

The scoring system implements **12 weighted criteria**.

The system was originally designed with 9 criteria. Two criteria
(`living_area` and `rooms`) were added in a prior Phase 2 expansion,
bringing the total to 11. Most recently, `bathrooms` was removed and
four new criteria (`garage`, `plot_size`, `balcony`, `heating`) were
added, bringing the current count to 12.

| # | Criterion | Score source | Data source |
|---|-----------|-------------|-------------|
| 1 | `neighborhood_value` | `_score_neighborhood_value` | `detail` (price, living_area_m2, neighborhood_avg_price_m2) |
| 2 | `ownership` | `_score_ownership` | `detail` (ownership_type, erfpacht_canon_annual) |
| 3 | `energy_label` | `_score_energy_label` | `detail` (energy_label) |
| 4 | `living_area` | `_score_living_area` | `detail` (living_area_m2) + `filter_config` (living_area_min) + preferences (living_area_thresholds.cap) |
| 5 | `construction_condition` | `_score_construction` | `detail` (year_built, insulation_score) + preferences (construction_year_range) |
| 6 | `garage` | `_score_garage` | `detail` (garage_type) |
| 7 | `parking` | `_score_parking` | `detail` (parking_type) |
| 8 | `rooms` | `_score_rooms` | `detail` (rooms) + `filter_config` (bedrooms_min) + preferences (rooms_thresholds.cap) |
| 9 | `plot_size` | `_score_plot_size` | `detail` (plot_size_m2) + preferences (plot_size_thresholds.cap) |
| 10 | `garden` | `_score_garden` | `detail` (garden_present, garden_size_m2, garden_orientation) |
| 11 | `heating` | `_score_heating` | `detail` (heating_type) |
| 12 | `balcony` | `_score_balcony` | `detail` (balcony_present) |

#### Expansion from 9 to 11 criteria (prior revision)

Two new scoring functions were added in a prior Phase 2 expansion:

- **`_score_living_area(detail, filter_config)`** — linear scale between the
  configured living-area minimum (floor → 0.0) and a cap (cap → 1.0). When
  `living_area_thresholds.cap` is absent from preferences, cap = floor + 100.
  If no living-area filter is configured at all, the criterion returns `None`.
  Threshold defaults documented in
  `config/preferences.json` → `living_area_thresholds`.

- **`_score_rooms(detail, filter_config)`** — linear scale between the
  configured bedrooms minimum (floor → 0.0) and cap. If
  `rooms_thresholds.cap` is absent from preferences, cap = max(8, floor + 4).
  Threshold defaults documented in
  `config/preferences.json` → `rooms_thresholds`.

Both functions accept `filter_config` as an explicit parameter, reading the
current filter thresholds from `config/filters.json` via `FilterConfig.from_file()`.
This creates a dependency on the co-worker's `FilterConfig` work in
`src/config.py` / `src/storage.py` / `src/main.py`, which is already merged
into this branch.

#### Further revision to 12 criteria (2026-08-20)

`bathrooms` was removed (empirically non-discriminating — see
`product.md` §12a). Four new criteria were added using fields already
extracted by the detail scraper but unused by scoring:

- **`_score_garage(detail)`** — ranked table of 10 garage types (inpandige
  garage = highest, garage mogelijk = lowest). `garage_type` is `None` is
  treated as a confirmed negative (0.0) because Funda omits the entire
  Garage section when no garage exists. Unrecognized values return `None`
  (missing). Combined "TypeA + TypeB" garage values are handled by scoring
  only the first segment (see Known limitations).
- **`_score_plot_size(detail, preferences)`** — linear "more is better"
  scoring: `plot_size_m2 / cap`, clamped to [0, 1]. `None` returns `None`
  (missing) since it is ambiguous whether a missing value means "no plot"
  or a parse failure.
- **`_score_balcony(detail)`** — binary: 1.0 if `balcony_present` is
  truthy, 0.0 otherwise. Never returns `None`; falsy values are treated as
  a confirmed negative (same reasoning as garage — the page field only
  appears when a balcony exists).
- **`_score_heating(detail)`** — heat pump = 1.0, district heating = 0.6,
  gas boiler = 0.3. `None` returns `None` (missing) — every home has some
  form of heating, so absence is treated as a likely parsing miss.

The ownership formula was changed from a flat 3-tier split to a continuous
scale based on erfpacht canon amount. The energy-label formula was changed
from a linear scale to a concave curve (sqrt of normalized index). The
construction-condition formula was changed from 50/50 year-insulation to
35% year / 65% insulation, with year bounds sourced from
`config/preferences.json` instead of being hardcoded.

#### Missing-vs-negative handling per criterion

Certain criteria treat an absent value as a **CONFIRMED NEGATIVE** (the
score contributes as a real 0 value and is included in the renormalization
calculation), while others treat absence as **MISSING** (excluded from
renormalization). The distinction is based on Funda's page structure:

**Confirmed negative on absence** (page reliably omits the section when
the feature genuinely doesn't exist):

- `garage` — `garage_type` is `None` scores 0.0 (page has no "Garage"
  section).
- `balcony` — `balcony_present` is falsy scores 0.0 (page has no
  "Balkon/dakterras" field).

**Missing on absence** (absence is inconclusive):

- All other criteria (`neighborhood_value`, `ownership`, `energy_label`,
  `living_area`, `construction_condition`, `parking`, `rooms`, `plot_size`,
  `garden`). For `garden`: `garden_present` is explicitly `False` scores 0.0
  (confirmed absence); when the "Tuin" field is not found on the page, the
  detail scraper returns `None` (missing). For `heating`: every home has
  some form of heating, so if `heating_type` is `None` it is treated as more
  likely a parsing miss than a genuine absence.

#### Breakdown reconciliation

Prior to 2026-08-20, the `points_possible` and `points_earned` values in
the breakdown used raw un-renormalized weights, which meant summing
`points_earned` across matched criteria did not reproduce the displayed
score when some criteria were missing. The fix computes
`points_possible` on the same renormalized scale as the final score:

```python
points_possible = round(weights[k] / total_weight * 100)  # renormalized
points_earned = round(points_possible * available[k])
```

Now the sum of `points_earned` across matched criteria always equals the
displayed score.

#### Coverage safeguard: partial_major_missing

The `score_listing()` function includes a fourth confidence value —
`"partial_major_missing"` — as a coverage safeguard. It is set when the
single highest-weighted criterion (determined dynamically via
`max(weights, key=weights.get)`, not hardcoded to any specific criterion
name) is among the missing criteria. This produces a stronger low-confidence
signal than an ordinary partial score, since the criterion carrying the
most weight could not be evaluated.

This confidence label does not alter how the numeric score is computed —
it only affects the confidence flag returned in `ScoreResult`. Because the
top criterion is determined dynamically from the weight table, the
safeguard automatically follows whichever criterion currently holds the
highest weight if the weight table changes in the future.

#### Known limitations

- **Combined parking/garage values:** `parking_type` and `garage_type` can
  be stored as combined "TypeA + TypeB" strings (documented in
  `docs/site-notes/funda.md`). Scoring uses only the first segment before
  "+".
- **Weight-sum validation:** `config/preferences.json` weights must sum to
  exactly 100; `_load_preferences()` raises a `ValueError` if they do not
  (previously logged a warning only).

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
  guessed. As of Task 10 it also returns the raw Dutch listing
  description (`description`, capped at 4000 chars) for the
  notification's Pros/Cons/Bottom line sections; the field is
  in-memory only and is not persisted. It also enriches the
  `neighborhood` field from the detail page's address header as
  `"{neighborhood} - {postal code} {city}"` (see "Neighborhood
  extraction" below).

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
stories INTEGER
has_attic INTEGER
neighborhood_avg_price_m2 REAL
image_urls TEXT
score INTEGER
score_breakdown TEXT
score_confidence TEXT
detail_fetched_at TEXT
```

### Schema migration

`storage.init_db()` is the project's only schema-migration mechanism —
no framework. On every startup it compares the live table against the
expected column set (PRAGMA `table_info`) and adds missing columns with
idempotent, non-destructive `ALTER TABLE ADD COLUMN` statements
(`phase2_columns` list in `storage.py`). This upgraded legacy runtime
databases that predated later features: e.g. the production
`data/funda.db` (which ended at `detail_fetched_at`) gained
`last_seen_at` and `image_urls TEXT` automatically on the first
`init_db()` call after the rich-photo feature. Existing rows keep all
values; new columns start as NULL and are populated on subsequent
detail fetches. The `listings_archive` table always mirrors the
`listings` column set because archival uses `SELECT *`.

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

#### Current format (Task 9 — approved template)

The notification message follows the owner-approved template: bold
address title, metric lines in `EMOJI + English metric name + ":" +
value` form, a "Location On Map" link riding on the Location line, a
"View on Funda" link after the Bottom line, and no Score display. The
text is delivered together with the property photos as one Telegram
media message (see "Property images in notifications"). The format is:

```
<b>{address}</b>

💰 Price: €{price}
🏠 Living area: {living_area} m² · €{price_per_m2}/m²
🌳 Plot Size: {plot_size_m2} m²
🛏 Bedrooms: {bedrooms}
🌿 Garden area: {garden_size_m2} m² / Yes / No
📍 Location: {city} · {street} · <a href="{map_url}">Location On Map</a>
⚡ Energy label: {energy_label}
🏗 Year built: {year_built}
🏢 Stories: {stories} [+ Attic]
🅿️ Parking: {value}

🟢 Pros:
• {bullet}
• ...

🔴 Cons:
• {bullet}
• ...

Bottom line: ...

<a href="{english_url}">View on Funda</a>
```

> **Superseded (Task 11):** the "View on Funda" link previously rode
> inline on the Location line, pointing at the stored canonical URL. It
> was moved to its own line after the Bottom line and now points at the
> English variant of the URL; the Location line instead carries a
> "Location On Map" link to the English URL with a `/kaart` suffix. The
> stored `url` remains the canonical non-English URL — the English and
> map URLs are derived at format time inside `notifier.py` only.

> **Superseded (Task 8):** the previous "Task 7" layout used metric
> lines without names/colons (e.g. `💰 €{price}`), a `✨` key-facts
> line, bullet Best/Weakest sections and a full score breakdown. Task 8
> replaced it with the metric-only format, dropped the Dutch
> terminology (property type, listing status, ownership wording) and
> added property-feature lines. Task 9 (current) replaced it again with
> the approved template above: the Score is no longer displayed and the
> Garage line was removed. Values and score semantics are unchanged;
> only their visual arrangement changed.

**Rules:**

* **Title** — the address on line 1, bold, kept exactly as provided by
  the listing; no prose around it.
* **Metric lines** — `💰 Price`, `🏠 Living area` (living area +
  price/m²), `🌳 Plot Size`, `🛏 Bedrooms`, `🌿 Garden area`,
  `📍 Location`, `⚡ Energy label`, `🏗 Year built`, `🏢 Stories`,
  `🅿️ Parking`.
  Lines whose only content would be missing fields are omitted
  entirely.
* **Score** — intentionally NOT displayed in the notification (Task 9).
  Score calculation, scoring fields, score-based filtering and all
  score-related logic are unchanged; only the notification text no
  longer shows them.
* **Parking** (Task 9) — `🅿️ Parking` value is English. Stored English
  codes map directly (`Private`, `Carport`, `Public`, `Paid`); raw
  Dutch page text renders as `English meaning (original Dutch term)`,
  e.g. `Available (Parkeervergunning)`; a missing value renders `No`.
* **Garden area** — the size in m² when `garden_size_m2` is available,
  otherwise `Yes` (garden present) or `No`.
* **Location** — uses the available components and carries the "Location On
  Map" link (the stored URL's English variant with a `/kaart` suffix)
  inline. The `neighborhood` field is enriched on the detail-page fetch
  to `"{neighborhood} - {postal code} {city}"` (e.g. `Amerika - 1448 XS
  Purmerend`) and is displayed verbatim on the Location line — it is a
  pre-formatted string, no longer a lowercase city slug, so it is no
  longer title-cased (title-casing would mangle the postal code, e.g.
  `XS` → `Xs`). Street is derived from the address by stripping the
  trailing house number and is omitted when no house number can be
  identified (a street is never guessed).
* **View on Funda link** (Task 11) — a `View on Funda` link is appended
  on its own line after the Bottom line, pointing at the English
  variant of the stored URL (no `/kaart` suffix). The URL conversions
  (canonical → `/en/detail/…` and `/en/detail/…/kaart`) happen only in
  `notifier.py` at format time; the database `url` column and the
  entire scraping/storage pipeline keep the canonical non-English URL
  unchanged.
* **Stories** — `🏢 Stories: {stories}` is shown only when `stories` is
  non-null (from "Aantal woonlagen" in the Indeling subsection); the
  value renders `{stories} + Attic` when `has_attic` is truthy. The line
  is omitted entirely when `stories` is absent — an attic without a
  known story count is not displayed.
* **Not displayed** — property type, listing status, ownership wording
  (Dutch terminology such as `Eengezinswoning`, `Beschikbaar`,
  `Erfpacht`), and the Garage line (not part of the approved
  template).
* **Pros / Cons / Bottom line** (Task 10) — every notification
  carries 🟢 Pros and 🔴 Cons (max 5 bullets each) plus a one-sentence
  Bottom line. Bullets come first from Dutch keyword phrases actually
  present in the description extracted by `detail_scraper.py`
  (`description` field; negations like "niet/geen/zonder" suppress a
  match; erfpacht is a con only when the canon is not bought off —
  "afgekocht"), then from the listing's own data (energy label, living
  area, plot size, construction year, garden, garage, parking,
  price/m² vs neighborhood average, price vs search range). If no
  bullets exist at all, honest fallbacks are used ("Matches all your
  core criteria…" / "No notable drawbacks identified in the available
  data"). The Bottom line combines the top two pros and the main con.
  Nothing is fabricated.
* **English-only values** — the energy label is displayed only when it
  matches the valid Funda label pattern (A–G with optional `+`);
  garbled Dutch page text (a known detail-scraper parsing edge case)
  is not shown.

#### Key property metrics (MVP extension)

The header was extended with reliably available listing metrics. Missing
metrics are omitted from the optional lines — never invented or shown as
placeholder values:

```
<b>{address}</b>

💰 Price: €{price}
🏠 Living area: {living_area} m² · €{price_per_m2}/m²
🌳 Plot Size: {plot_size_m2} m²
🛏 Bedrooms: {bedrooms}
🌿 Garden area: {garden_size_m2} m² / Yes / No
📍 Location: {city} · {street} · <a href="{map_url}">Location On Map</a>
⚡ Energy label: {energy_label}
🏗 Year built: {year_built}
🏢 Stories: {stories} [+ Attic]
🅿️ Parking: {value}
```

* **Price per m²** — computed strictly from the two required fields
  (`price / living_area_m2`); rendered only when both are present.
* **Energy label** — its own `⚡ Energy label: …` line, only when
  `energy_label` is non-null and matches the valid label pattern.
* **Plot / Year built** — `🌳 Plot Size: … m²` and `🏗 Year built: …`
  lines appear only when the respective field is non-null
  (`plot_size_m2`, `year_built`).
* **Garden / Parking** — `🌿 Garden area` and `🅿️ Parking` lines are
  always shown; values are English (see the approved-template rules
  above).
* **Property type, status and ownership** — not displayed (Dutch
  terminology, see the approved-template rules above).
* **Score** — not displayed (see the approved-template rules above).

### Property images in notifications

Each listing notification attaches up to **exactly 3 property photos**
belonging to the same listing. The pipeline spans two components:

**Extraction (`detail_scraper.py`)**

* During the existing detail-page Playwright render pass (no extra page
  fetch), a JS collector gathers candidate URLs from
  `meta[property="og:image"]`, gallery `<img>` `src`/`currentSrc`, and the
  largest `srcset` candidate, in document order (hero/facade photo first).
* Candidates are filtered to https URLs on Funda's own photo CDN
  (`cloud.funda.nl` with a `/valentina…` media path). Confirmed live
  2026-08-23; UI assets, foreign hosts (e.g. `*.funda.io` infrastructure)
  and non-image extensions are rejected.
* Size variants of the same photo (`775_1440x960.jpg`,
  `775.jpg?options=width=720`) collapse onto one canonical URL requested
  at `?options=width=1440`; dedupe preserves gallery order; output is
  capped at 10 URLs and returned as `image_urls` in the detail dict.
* Photo extraction failure never fails the detail fetch — the field is
  omitted and the notification degrades to text-only.
* Image URLs are persisted in the `listings.image_urls` column as a
  JSON-encoded TEXT array (NULL when no photos were found). Storage
  serialises on write (`insert_listing`), decodes back into a list on
  read (`fetch_unnotified_matching_listings`), and preserves stored
  URLs on card-level re-inserts via the existing detail-field
  preservation pattern. `notifier.py` also accepts the JSON TEXT form
  defensively (raw DB rows, e.g. from the backfill flow).

> **Superseded note:** an earlier revision of this section stated that
> image URLs ride the in-memory detail→notification data flow only and
> are not stored in the database. That design was replaced by the
> persisted `image_urls` column described above after the runtime
> schema mismatch surfaced; the paragraph is kept for historical
> context.

**Selection, download and delivery (`notifier.py`)**

* `_select_images()` deterministically takes the first 3 unique http(s)
  URLs from `listing["image_urls"]` (gallery order preserved, no
  randomness). Fewer than 3 valid URLs → all valid ones are used.
* Each image is downloaded via urllib into a per-notification temporary
  directory with: 20 s timeout, 10 MB hard cap (streamed enforcement),
  `image/*` Content-Type check, magic-byte validation (JPEG/PNG/WEBP/GIF)
  before writing, so error pages served with a misleading Content-Type are
  rejected. Files are written only after validation (no partial files).
  The temp directory is always removed afterwards.
* Delivery (Task 6): text and photos are delivered TOGETHER as one
  coherent media message. The full rich HTML message is sent as the
  caption of the photo/album: `sendPhoto` for a single image,
  `sendMediaGroup` (multipart, caption = full HTML message on the first
  item, `parse_mode=HTML`) for 2–3 images. No standalone text message is
  sent in this path.
* Fallbacks — each listing still receives exactly one delivered
  notification, never a duplicate standalone text message or duplicate
  image messages:
  * no `image_urls` / all downloads fail → text-only `sendMessage`
    (the pre-Task-6 behaviour);
  * album send fails → the notification falls back to a text-only
    `sendMessage`; no image retry is attempted;
  * the message exceeds Telegram's 1024-character media caption limit
    (checked against a 1000-character safety limit) → text-first
    presentation: `sendMessage` with the full message, then the photos
    as an album captioned with the HTML-escaped address — so no
    information is dropped when the caption cannot carry the text.
* Failure semantics:
  * the authoritative notification (album-with-caption, or the text
    fallback) fails → notification failed; the listing stays unnotified
    and is retried on a later run;
  * any individual image download fails → that image is skipped;
    remaining photos still ship;
  * all downloads fail → text-only notification stands, result is success;
  * album upload fails → the notification degrades to the text-only
    message (logged warning, result follows the text send). Retrying
    image delivery would duplicate the already-delivered notification,
    which the project forbids.

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

## Data Retention & Archival

Listings that are no longer seen in successive scrapes are automatically
moved from the live `listings` table to a `listings_archive` table to keep
the live table bounded while preserving data for historical analysis.

### Schema (already implemented — Tasks 1)

* `listings.last_seen_at` (TEXT, nullable ISO-8601) — stamped to "now" on
  every `insert_listing()` call (both INSERT and UPDATE paths).
* `listings_archive` — an exact-schema mirror of `listings` (same columns).
  Rows are moved into it by the archival function, not copied by
  `insert_listing()`.

### Staleness condition

A listing is stale when:

* `last_seen_at IS NOT NULL` and is older than *now* minus
  `retention.stale_days`, **or**
* `last_seen_at IS NULL` and `first_seen_at` is older than the same
  cutoff (fallback for rows predating the `last_seen_at` column).

The default threshold is **60 days**, configured in
`config/retention.json` (`stale_days` key, loaded by
`RetentionConfig.from_file()` — see `src/config.py`).

### Archival function (`storage.archive_stale_listings()`)

`archive_stale_listings(db_path, retention)` performs an atomic archival
in a single transaction:

1. Counts stale rows matching the staleness condition.
2. If none, logs an info message and returns `0` (not an error).
3. Copies matching rows into `listings_archive` via
   `INSERT OR REPLACE` (handles the edge-case of a listing that was
   previously archived and reappeared in a scrape).
4. Logs each archived listing's `listing_id` and `address`.
5. Deletes the now-archived rows from `listings`.
6. Returns the count of archived rows.

### Integration in `main()`

The archival step is executed **every normal run**, immediately after
the filter-snapshot save (step 6) and before the final summary logging
(step 7).  It is **not** part of `_run_backfill()` or `_run_seed()`.

```
Step 6: save_filter_snapshot(filters, db_path)
Step 6.5: archive_stale_listings(db_path, retention)  ← new
Step 7: _log_run_summary(run_start, stats)
Step 8: save_last_successful_run(...)
```

* Runs identically in `--dry-run` and normal mode (like
  `save_filter_snapshot()`).
* Wrapped in try/except — a failure is logged via
  `logger.error(..., exc_info=True)` and the run continues.
* **Never** triggers `_send_failure_alert_and_exit()` or `sys.exit(1)`.
  Archival is a best-effort housekeeping step, not a run-correctness
  requirement.
* The count is stored in `stats["listings_archived"]` (default 0),
  logged in the run summary under "Archived".

### Design notes

* This mechanism is **filter-agnostic** and **decoupled from
  `run_is_full_scan` / filter-change detection**.  It is a staleness-based
  mechanism, not a filter-context cleanup mechanism.  Whether the run
  detects a filter change or runs a delta scan, archival proceeds
  identically based only on `last_seen_at` age.
* Archived rows remain directly queryable via SQL against the
  `listings_archive` table for historical analysis (Phase 4).
* No CLI flag is needed — archival runs automatically on every normal
  run.

---

## Phase 2 — Configurable Search Filters

Phase 2 makes the search filter criteria configurable at runtime. The owner
edits a single human-readable JSON file — `config/filters.json` — instead of
`.env`. The frozen contract is implemented across four files:
`config/filters.json` (user-editable values), `src/config.py` (single source
of truth for the filter shape and loading), `src/storage.py` (applies the
filters in the matching query), and `src/main.py` (loads and threads the
configuration through the run).

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

The file is a single flat JSON object — one key per filter — where `null`
(or `[]` for multi-value filters) means "no restriction". Every key is
optional: an absent key becomes `None` on `FilterConfig` with no code-level
fallback. The committed file ships the Phase 1 values as starting values:

```json
{
    "note": "Human-editable housing search filters. See the table below and the per-key docs for types and valid values.",
    "price_min": 550000,
    "price_max": 750000,
    "bedrooms_min": 3,
    "living_area_min": 100,
    "bedrooms_max": null,
    "living_area_max": null,
    "rooms_min": null,
    "rooms_max": null,
    "plot_size_min": null,
    "plot_size_max": null,
    "property_type": null,
    "energy_labels": ["A++++", "A+++", "A++", "A+", "A", "B", "C", "D", "A+++++"],
    "transaction_type": null,
    "radius_km": 10,
    "selected_area": "amsterdam",
    "construction_type": null,
    "construction_periods": ["1971-1980", "1981-1990", "1991-2000", "2001-2010", "2011-2020", "after_2020"],
    "object_type": null,
    "bathrooms_min": null,
    "bathrooms_max": null,
    "garage_capacity_min": null,
    "garage_capacity_max": null,
    "exterior_space_type": null,
    "exterior_space_garden_orientation": null,
    "garden": true,
    "garden_size_min": 70,
    "zoning": null,
    "parking_facility": null,
    "garage_type": null,
    "accessibility": null,
    "amenities": null,
    "availability": "available",
    "sort": "publish_date_utc_desc"
}
```

| Key                                  | Type       | Starting value | Meaning                                    |
| ------------------------------------ | ---------- | -------------- | ------------------------------------------ |
| `price_min`                          | int / null | 550000         | Minimum asking price (€)                   |
| `price_max`                          | int / null | 750000         | Maximum asking price (€)                   |
| `bedrooms_min`                       | int / null | 3              | Minimum bedrooms                           |
| `bedrooms_max`                       | int / null | none           | Maximum bedrooms                           |
| `living_area_min`                    | int / null | 100            | Minimum living area (m²)                   |
| `living_area_max`                    | int / null | none           | Maximum living area (m²)                   |
| `rooms_min`                          | int / null | none           | Minimum total rooms                        |
| `rooms_max`                          | int / null | none           | Maximum total rooms                        |
| `plot_size_min`                      | int / null | none           | Minimum plot size (m²); also emitted as `plot_area` on the search URL |
| `plot_size_max`                      | int / null | none           | Maximum plot size (m²)                     |
| `property_type`                      | str / null | none           | Required property type, e.g. `appartement` |
| `energy_labels`                      | list / null | none           | Ordered energy labels sent to Funda verbatim |
| `transaction_type`                   | str / null | none           | `koop` (for sale) or `huur` (rent)         |
| `radius_km`                          | int / null | none           | Search radius (km); emitted as `radius_search` |
| `selected_area`                      | str / null | `amsterdam`    | Area slug (e.g. `amsterdam`, `nl`); emitted as `selected_area` |
| `construction_type`                  | list / null | none           | Construction types: `newly_built`/`resale` (legacy `new`/`existing` mapped) |
| `construction_periods`               | list / null | none           | Human-readable build-year periods (mapped) |
| `object_type`                        | list / null | none           | Object types: `apartment`, `house`         |
| `bathrooms_min`                      | int / null | none           | Minimum bathrooms                          |
| `bathrooms_max`                      | int / null | none           | Maximum bathrooms                          |
| `garage_capacity_min`                | int / null | none           | Minimum garage capacity                    |
| `garage_capacity_max`                | int / null | none           | Maximum garage capacity                    |
| `exterior_space_type`                | list / null | none           | Exterior spaces: `balcony`, `terrace`, `garden` |
| `exterior_space_garden_orientation`  | list / null | none           | Garden orientations: `north`, `east`, `south`, `west` |
| `garden`                             | bool / null | none           | Legacy shorthand: `true` adds `garden` to `exterior_space_type` |
| `garden_size_min`                    | int / null | none           | Minimum garden size (m²); requires a garden selected |
| `zoning`                             | list / null | none           | Zoning: `residential`, `recreational`      |
| `parking_facility`                   | list / null | none           | Parking facility types (see vocabulary below) |
| `garage_type`                        | list / null | none           | Garage types (see vocabulary below)        |
| `accessibility`                      | list / null | none           | Accessibility features (see vocabulary below) |
| `amenities`                          | list / null | none           | Amenities (see vocabulary below)           |
| `availability`                       | str / null | none           | Free-string `availability` value           |
| `sort`                               | str / null | none           | Free-string `sort` value                   |

The `selected_area` key ships as `amsterdam` (preserving the Phase 1
behavior) rather than `null`, but like every other key it has no code-level
default: removing it yields `selected_area=None`. Its value is a plain area
slug; it is never combined with `radius_km`.

The `construction_type` field was **converted from a single value to a
multi-value list**. Funda's current tokens are `newly_built` and `resale`;
the old `existing`/`new` vocabulary referred to the same concept and is
accepted (and mapped to `resale`/`newly_built`) for backward compatibility.
A bare string is accepted as a one-element list. See the "conflict
resolution" note below for the reasoning.

> **Superseded:** the `energy_label_min` / `energy_label_max` keys were
> removed and replaced by the single ordered `energy_labels` list. The
> radius no longer uses the embedded-JSON-array encoding (see below).

The `energy_labels` default order (`A++++, A+++, A++, A+, A, B, C, D,
A+++++`) is preserved exactly as it appeared in the authoritative source
URL, not sorted ordinally. This ordering is unusual (`A+++++` appears last,
after `D`) and should be investigated later to confirm whether Funda's
search endpoint is actually order-sensitive, or whether this was an
artifact of how the URL was originally captured. Do not silently reorder
it.

The `null` filter values mean "no preference filter". Missing keys become
`None` (no restriction) — there is no code-level fallback default; the values
shown above are only what the committed file currently contains. Unknown keys
are rejected with a clear error so a typo cannot silently change behavior.

**Ranged vs single-value filters.** Most numeric filters are **ranged**
(`*_min` / `*_max` pairs). On the search URL a bound set to `null` is
open-ended (e.g. `floor_area=100-` when only `living_area_min` is set).
On the storage query a `null` bound disables that side of the range.

Several filters are **single-value** (no range makes sense):

* `transaction_type` — maps to Funda's offering type (`koop`/`huur`).
  When unset the scraper defaults to `koop`, preserving for-sale behavior.
* `radius_km` — a single positive integer. It is emitted as its own
  `radius_search={radius_km}` query parameter; `selected_area` stays a plain
  area slug (e.g. `amsterdam`).

  > **Superseded:** the earlier embedded-radius encoding — where
  > `radius_km` was folded into `selected_area` as a JSON-array string
  > `["amsterdam,{N}km"]` — was removed. `selected_area` is now always a
  > plain slug and the radius travels in `radius_search` instead.
* `construction_type` — a multi-value list of construction types using
  Funda's current tokens `newly_built` / `resale` (the legacy `new` /
  `existing` are accepted and mapped). Set to `null` for no restriction.
* `selected_area` — a plain area slug (shipped as `amsterdam`), emitted as
  `selected_area=…` only when set; when `None` the parameter is omitted
  entirely so Funda applies its own default. Distinct from `radius_km` (which
  is its own `radius_search` parameter).
* `energy_labels` — an ordered list of energy labels, sent to Funda
  verbatim as `energy_label=…` (each label percent-encoded, comma-joined).
  Search-level only.
* `construction_periods` — a list of human-readable build-year period keys,
  mapped to Funda's internal codes (see `CONSTRUCTION_PERIOD_MAP` below).
  Search-level only.
* `garden` — a boolean legacy shorthand; `true` is equivalent to including
  `"garden"` in `exterior_space_type`. The URL builder merges the two into a
  single `exterior_space_type` parameter (see the reconciliation note below).
* `garden_size_min` — a non-negative integer; emits
  `exterior_space_garden_size={min}-` and requires a garden to be selected
  (`garden=true` or `"garden"` in `exterior_space_type`).
* `object_type` — a multi-value list (`apartment`, `house`), emitted as
  `object_type=…`.
* `bathrooms` — ranged (`bathrooms_min`/`bathrooms_max`), emitted as
  `bathrooms=…`.
* `garage_capacity` — ranged (`garage_capacity_min`/`garage_capacity_max`),
  emitted as `garage_capacity=…`.
* `exterior_space_type` — a multi-value list (`balcony`, `terrace`, `garden`).
* `exterior_space_garden_orientation` — a multi-value list (`north`, `east`,
  `south`, `west`).
* `zoning` — a multi-value list (`residential`, `recreational`).
* `parking_facility` — a multi-value list (see vocabulary table below).
* `garage_type` — a multi-value list (see vocabulary table below).
* `accessibility` — a multi-value list (see vocabulary table below).
* `amenities` — a multi-value list (see vocabulary table below).
* `availability` — a free-string value emitted as `availability=…`.
* `sort` — a free-string value emitted as `sort=…`.

### `src/config.py` — FilterConfig

```python
@dataclass(frozen=True)
class FilterConfig:
    price_min: int | None = None
    price_max: int | None = None
    bedrooms_min: int | None = None
    living_area_min: int | None = None
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
    selected_area: str | None = None
    construction_type: list[str] | None = None
    construction_periods: list[str] | None = None
    object_type: list[str] | None = None
    bathrooms_min: int | None = None
    bathrooms_max: int | None = None
    garage_capacity_min: int | None = None
    garage_capacity_max: int | None = None
    exterior_space_type: list[str] | None = None
    exterior_space_garden_orientation: list[str] | None = None
    garden: bool | None = None
    garden_size_min: int | None = None
    zoning: list[str] | None = None
    parking_facility: list[str] | None = None
    garage_type: list[str] | None = None
    accessibility: list[str] | None = None
    amenities: list[str] | None = None
    availability: str | None = None
    sort: str | None = None
```

> **Superseded:** `energy_label_min` / `energy_label_max` were removed from
> `FilterConfig` (fields, validation, and the uppercase-normalization logic)
> and replaced by the ordered `energy_labels` list.

`CONSTRUCTION_PERIOD_MAP` (module-level dict in `src/config.py`) translates
the human-readable period keys used in `config/filters.json` to Funda's
internal codes:

| Human-readable key | Funda code              |
| ------------------ | ----------------------- |
| `"1971-1980"`      | `from_1971_to_1980`     |
| `"1981-1990"`      | `from_1981_to_1990`     |
| `"1991-2000"`      | `from_1991_to_2000`     |
| `"2001-2010"`      | `from_2001_to_2010`     |
| `"2011-2020"`      | `from_2011_to_2020`     |
| `"after_2020"`     | `after_2020`            |

Every configured key must exist in this map; an invalid key raises a
`ValueError` listing the invalid key(s) and the valid options. The mapping
to Funda codes happens in `main.py` (and in the URL-building test), not in
`FilterConfig` itself.

The multi-value filters added by the full-search-parameter-coverage task
carry Funda's English wire tokens verbatim (they are already
human-readable, so no mapping table is needed), but each is validated
against a fixed vocabulary. Values are stripped, lowercased, ordered as
configured, and de-duplicated. The valid tokens are:

| Key                                 | Valid tokens                                                                                         |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `object_type`                       | `apartment`, `house`                                                                                 |
| `construction_type`                 | `newly_built`, `resale` (legacy `new`/`existing` mapped)                                             |
| `exterior_space_type`               | `balcony`, `terrace`, `garden`                                                                       |
| `exterior_space_garden_orientation` | `north`, `east`, `south`, `west`                                                                     |
| `zoning`                            | `residential`, `recreational`                                                                        |
| `parking_facility`                  | `on_private_property`, `on_enclosed_property`, `public_parking`, `paid_parking`, `parking_garage`, `parking_permits` |
| `garage_type`                       | `lean_to`, `lock_up`, `garage_and_carport`, `built_in`, `underground`, `basement`, `detached`, `garage_possible`, `carport`, `parking_space`, `all_garages` |
| `accessibility`                     | `lift`, `single_storey`, `accessible_for_the_disabled`, `accessible_for_the_elderly`, `adapted_home`, `ground_floor` |
| `amenities`                         | `renewable_energy`, `central_heating_boiler`, `swimming_pool`, `bathtub`, `fireplace`, `fixer_upper`, `double_occupancy` |

> **Flagged for owner verification:** these vocabularies were captured from
> the authoritative reference URL. They could not be re-validated against
> the live Funda search UI in this environment (Funda served an Akamai
> bot-check), so the owner should confirm the token set is complete before
> relying on values not listed above.

#### `construction_type` conflict resolution

The original `construction_type` field was a single categorical value
(`existing` / `new`). The authoritative reference URL shows Funda's actual
parameter as **multi-value** (`construction_type=newly_built,resale`). The
two vocabularies describe the same concept (`existing` ≈ resale build,
`new` ≈ newly built), so rather than silently pick one the field was:

* converted to a multi-value list,
* switched to Funda's current tokens (`newly_built`, `resale`),
* made backward-compatible: a bare string is accepted as a one-element list,
  and the legacy `existing`/`new` tokens are mapped to `resale`/`newly_built`.

#### `exterior_space_type` / `garden` reconciliation

`exterior_space_type` (multi-value: `balcony`, `terrace`, `garden`) overlaps
with the pre-existing boolean `garden` field (`garden=true` emitted
`exterior_space_type=garden`). To avoid two ways of expressing the same
filter, `garden` is retained as a **legacy shorthand**: the URL builder
merges `garden=true` with the `exterior_space_type` list into a single
`exterior_space_type` parameter (de-duplicated, order-preserving).
`garden_size_min` still applies only when a garden is actually selected
(`garden=true` or `"garden"` in `exterior_space_type`). This preserves the
existing default behavior (`garden: true` + `garden_size_min: 70`) exactly.

* `FilterConfig` is immutable (`frozen=True`); validation runs at
  construction in `__post_init__`.
* `DEFAULT_FILTERS` is a module-level `FilterConfig` with every field `None`
  (representing "no restriction at all"). With no code-level fallback
  defaults, the Phase 1 criteria live only as values written in
  `config/filters.json`.
* `FilterConfig.from_file()` loads `config/filters.json` from a
  project-root-relative path, so execution from cron, systemd, or tmux does
  not depend on the process working directory. The file is a single flat
  JSON object; every key is optional and an absent key becomes `None` (no
  restriction) with no fallback. Unknown keys and invalid values raise a
  clear `ValueError` rather than being silently coerced.

Validation rules:

* integer fields must be integers when set (floats and booleans rejected);
  invalid values raise `ValueError`; `None` fields are not validated
* for each ranged pair (`price`, `bedrooms`, `living_area`, `rooms`,
  `plot_size`, `bathrooms`, `garage_capacity`), when both bounds are set the
  minimum must be `<=` the maximum
* numeric minimums (`price_min`, `bedrooms_min`, `living_area_min`,
  `plot_size_min`) must be non-negative when set
* `radius_km`, when set, must be a positive integer (rejects `0`, negatives,
  non-integers, and booleans)
* `transaction_type`, when set, must be `koop` or `huur` (lowercase)
* `construction_type`, when set, must be a non-empty list of strings (a bare
  string is accepted as a one-element list); every token must be
  `newly_built` or `resale`, with the legacy `new`/`existing` mapped
  accordingly
* `selected_area`, when set, must be a non-empty string
* `property_type`, when set, must be a non-empty string
* `energy_labels`, when set, must be a non-empty list of non-empty strings.
  No further validation against a fixed vocabulary — Funda accepts whatever
  is configured, and the list is passed verbatim in the configured order.
* `construction_periods`, when set, must be a non-empty list of strings, and
  every key must exist in `CONSTRUCTION_PERIOD_MAP` (an invalid key raises
  `ValueError` listing the invalid key(s) and the valid options)
* the new multi-value filters (`object_type`, `exterior_space_type`,
  `exterior_space_garden_orientation`, `zoning`, `parking_facility`,
  `garage_type`, `accessibility`, `amenities`), when set, must be a
  non-empty list of non-empty strings, and every token must exist in the
  corresponding vocabulary table above (an invalid token raises `ValueError`)
* `garden`, when set, must be a boolean
* `garden_size_min`, when set, must be a non-negative integer and requires a
  garden to be selected (`garden=true` or `"garden"` in
  `exterior_space_type`); otherwise it raises `ValueError`
* `availability` and `sort`, when set, must be non-empty strings (free-form,
  no fixed vocabulary)
* documentation keys (`note`, `_comment`) are tolerated (ignored) by the
  loader; any other unknown key still raises `ValueError`
* the file must be a JSON object; invalid JSON or an unreadable/missing file
  raises `ValueError`

> **Superseded:** the `energy_label_min`/`energy_label_max` validation
> (non-empty string + uppercase normalization) was removed with those fields.

### `src/storage.py` — parameterized matching query

```python
fetch_unnotified_matching_listings(
    db_path: Path | str = DEFAULT_DB_PATH,
    filters: FilterConfig = DEFAULT_FILTERS,
) -> list[dict]
```

* The signature is backward compatible — existing callers that pass only
  `db_path` get no restriction (an all-`None` `DEFAULT_FILTERS`).
* The base condition `notified = 0` is always applied. Each of the four core
  bounds — `price >= ?`/`<= ?`, `bedrooms >= ?`, `living_area_m2 >= ?` — is
  only applied when its filter value is not `None`, so an all-`None`
  `FilterConfig` matches every unnotified listing.
* Optional preferences are applied only when not `None`:
  * `bedrooms_max` → `bedrooms <= ?`
  * `living_area_max` → `living_area_m2 <= ?`
  * `property_type` → `property_type = ?` (exact match)
  * `plot_size_min` → `plot_size_m2 >= ?`
  * `plot_size_max` → `plot_size_m2 <= ?`

> **Superseded (filters task):** the DB-level energy-label filter
> (`UPPER(energy_label) IN (…)`, driven by `energy_label_min`/`max`) was
> removed from `fetch_unnotified_matching_listings()` and from
> `main.py::_run_backfill()`, and is **intentionally not replaced**. Funda
> now enforces the energy-label filter server-side via the `energy_label`
> search parameter. The `_acceptable_energy_labels()` helper was removed
> with it. `config/preferences.json`'s `energy_label_scale` key and
> `scoring.py` were **not** touched — that scale is used by the scoring
> formula and is unrelated to the search filter.
* `rooms_min`/`rooms_max`, `radius_km`, `selected_area`, `construction_type`,
  `energy_labels`, `construction_periods`, `object_type`, `bathrooms_min`/`max`,
  `garage_capacity_min`/`max`, `exterior_space_type`,
  `exterior_space_garden_orientation`, `zoning`, `parking_facility`,
  `garage_type`, `accessibility`, `amenities`, `garden`, `garden_size_min`,
  `availability`, and `sort` are **search-level** filters: they are applied
  to the Funda search URL, not the storage query (see `main.py`
  orchestration below). `plot_size_min`/`max` is **both** storage-level
  (`plot_size_m2 >= ?` / `<= ?`) and search-level (emitted as `plot_area`).
* NULL semantics: a listing with a NULL value for an optional field never
  satisfies an enabled preference filter (standard SQL comparison).

### `src/main.py` — orchestration integration

* `main()` loads `filters = FilterConfig.from_file()` once at run start.
* The **search-level** filters are passed to `scrape_funda(...)`:
  `price_min`, `price_max`, `living_area_min`/`max`, `bedrooms_min`/`max`,
  `rooms_min`/`max`, `radius_km`, `selected_area` (as `area`),
  `construction_type`, `energy_labels`, `construction_periods` (mapped via
  `CONSTRUCTION_PERIOD_MAP`), `object_type`, `plot_size_min`/`max` (as
  `plot_area`), `bathrooms_min`/`max`, `garage_capacity_min`/`max`,
  `exterior_space_type`, `exterior_space_garden_orientation`, `zoning`,
  `parking_facility`, `garage_type`, `accessibility`, `amenities`, `garden`,
  `garden_size_min`, `availability`, `sort`, and `offering_type` (derived
  from `transaction_type`, defaulting to `koop`).
* The same `filters` object is passed to
  `fetch_unnotified_matching_listings(db_path, filters=filters)`, so the
  storage query uses exactly the loaded configuration. The **storage-level**
  filters (`property_type`, `plot_size_min`/`max`, `bedrooms_max`,
  `living_area_max`) are applied in the matching query.
* Search-level filters (`rooms_min`/`max`, `radius_km`, `selected_area`,
  `construction_type`, `energy_labels`, `construction_periods`, `object_type`,
  `bathrooms_min`/`max`, `garage_capacity_min`/`max`, `exterior_space_type`,
  `exterior_space_garden_orientation`, `zoning`, `parking_facility`,
  `garage_type`, `accessibility`, `amenities`, `garden`, `garden_size_min`,
  `availability`, `sort`) are **not** passed to the storage query, which does
  not accept those parameters. (`plot_size_min`/`max` is the exception — it
  is used both in storage and on the search URL.)
* All Phase 1 orchestration (init_db, insert, notify, mark-as-notified,
  dry-run, exit codes, logging) is unchanged.

#### Orchestration stages

`main()` is decomposed into small, ordered stage helpers so the run
pipeline reads as a sequence of named steps. This is a structural
refactor only — every external behavior (flags, exit codes, log wording,
DB writes, gating) is unchanged:

| Stage | Helper | Concern |
| ----- | ------ | ------- |
| 1 | `_load_configuration()` | Loads `FilterConfig` + `RetentionConfig`; invalid config aborts before any scraping |
| 2 | `_determine_scan_mode()` | Snapshot comparison + staleness fallback → `ScanMode` |
| — | `_resolve_scan_parameters()` | Pure mapping: full scan → no publication filter + 5 pages; delta scan → 3-day publication filter + 15 pages |
| 3 | `_setup_logging()` | File + console handlers |
| 4–5 | init_db / `scrape_funda(...)` | Database init, scrape with scan-mode parameters |
| 6 | `_insert_listings_into_storage()` | Shared persistence stage (standard run + seed); returns classified `InsertResult` |
| 7 | `fetch_unnotified_matching_listings(...)` | Matching via storage (full filter config applied there) |
| 8 | `_score_and_persist_listing()` | Shared detail-fetch/score/persist core (standard run, seed, backfill) |
| 9 | `_apply_full_scan_gate()` + notify | Task 2 gating at `GATING_THRESHOLD = 70`, then notification (rich message + up to 3 property photos; see "Property images in notifications") |
| 10 | `_finalise_run()` | Filter snapshot save, stale-listing archival, run summary, last-successful-run |

Pipeline order (`configuration → scrape → persist → match`) and the
scan-parameter mapping are pinned by `tests/test_orchestration.py`.

---

## Phase 1 Filtering Criteria

The confirmed Phase 1 filtering criteria are the project's reference
criteria (the values shipped in `config/filters.json`):

* **Price:** €550,000–€750,000 (Confirmed)
* **Bedrooms:** ≥3
* **Living area:** ≥100 m²

These values are shipped as the starting values written in the committed
`config/filters.json`. They are not code-level defaults: `DEFAULT_FILTERS` in
`src/config.py` has every field `None`, and an absent key in the filter file
yields `None` (no restriction), not a fallback to these values.

### Resolution of Legacy Requirements

The price range has been explicitly confirmed as **€550,000–€750,000** by the project owner. This range is shipped as the starting value in `config/filters.json`, configurable by editing `price_min` / `price_max` there.

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
| `stories`        | INTEGER           | Number of floors ("Aantal woonlagen"); NULL when absent on the detail page |
| `has_attic`      | INTEGER           | Boolean 0/1: "zolder" present in "Aantal woonlagen"; always a concrete 0/1 after any detail fetch, never NULL |
| `property_type`  | TEXT              | Property type; NULL when not derivable from the card |
| `year_built`     | INTEGER           | Nullable                                             |
| `energy_label`   | TEXT              | Nullable                                             |
| `status`         | TEXT              | Available / under offer / sold / etc.; NULL at card level |
| `first_seen_at`  | TEXT              | ISO 8601 timestamp                                |
| `last_seen_at`   | TEXT              | Nullable. ISO 8601 timestamp stamped to "now" on
                      every `insert_listing()` call (both INSERT
                      and UPDATE paths). Used for future staleness
                      detection.                          |
| `notified`       | INTEGER           | Boolean: 0/1                                      |

### `listings_archive`

| Column           | Type              | Notes                                             |
| ---------------- | ----------------- | ------------------------------------------------- |
| (same as `listings`) |               | Same-schema mirror table for archived stale        |
|                  |                   | listings. Currently unused — population by archival |
|                  |                   | logic is a future task.                            |

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

#### Forum topics (additive)

The notifier exposes two non-destructive building blocks, added for the
one-off "Diemen — Funda Matches" topic task (see `Operations.md` §24):

* `create_forum_topic(name)` — calls Telegram `createForumTopic` on the
  configured supergroup and returns the new topic's `message_thread_id` (or
  `None` on failure). Requires the bot to be a supergroup admin with
  `can_manage_topics`. The token is never logged; HTTP 401/403 are reported
  as a permissions issue.
* `send_listing_notification(listing, thread_id=None)` — when `thread_id` is
  supplied, the rich notification (HTML caption + best-effort photos album)
  is posted to that forum topic instead of the environment-default listing
  topic. When `thread_id` is omitted behaviour is unchanged.

These add no scheduled-pipeline behaviour change; the default topic
(ID) selection (`_get_listing_topic_id`) remains the source of the topic for
the normal flow.

#### One-off operational modules

Ad-hoc, owner-requested flows that must not run the scheduled pipeline, mark
listings notified, or touch `config/filters.json` are implemented as small
standalone modules in `src/` (e.g. `src/diemen_topic.py`). They reuse the
existing scraper / storage / detail / scoring / notifier building blocks
unchanged, mirror `main.py`'s filter→scrape mapping, and expose a `--dry-run`
mode plus explicit aborts (no topic created / nothing sent) when a preflight
(failed or empty scrape, topic-creation failure) fails.

For a topic that needs a seed-batch plus ongoing live delivery isolated from
the global pipeline, the module keeps its own **separate "sent" ledger** (a
`diemen_sent` table distinct from the global `notified` flag). This is what
lets a dedicated flow dedup and go live without ever reading or resetting the
old notification state, and without posting to the general chat / other
topics. `src/diemen_topic.py` demonstrates this with `--mode seed` (post
existing matching rows) and `--mode live` (post only new matches), both
targeting a single fixed topic id. No scheduler is wired to it yet; live mode
is a runnable entrypoint ready to be scheduled later.

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
`FilterConfig.from_file()`), with the Phase 1 values shipped as the starting
values in that file (no code-level fallback; see "Phase 2 — Configurable
Search Filters" above). No configuration framework or new dependency was
introduced; `.env` remains reserved for secrets.

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
