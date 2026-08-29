# product.md — Amsterdam Funda Home-Search Agent

---

## 1. Product Overview

The Amsterdam Funda Home-Search Agent is an automated tool that monitors
residential properties for sale in Amsterdam on **Funda.nl** and notifies the
developer/owner through Telegram when a newly detected listing matches the
agreed housing criteria.

The primary goal is to reduce the need for manually checking Funda and to
deliver potentially suitable new properties to the owner quickly enough to be
useful in the Amsterdam housing market.

### Product scope

The initial product is intentionally limited to:

* Amsterdam
* properties for sale
* Funda.nl
* automated periodic checking
* new-listing detection
* configurable housing criteria
* Telegram notifications

Other real-estate websites are outside the current product scope.

---

## 2. Target User

The primary recipient of the system's notifications is the project owner /
developer who is interested in finding suitable homes in Amsterdam.

The system is not currently intended to be a public consumer application.

It is a personal/internal automation tool.

---

## 3. Core User Outcome

The desired user experience is:

```text
Funda
  ↓
Amsterdam for-sale listings
  ↓
Agent periodically checks listings
  ↓
New listing detected
  ↓
Housing criteria evaluated
  ↓
Suitable?
  ├── No → Store / ignore notification
  │
  └── Yes
       ↓
     Telegram
       ↓
     Owner
```

The owner should receive a Telegram notification for a newly detected listing
that matches the confirmed Phase 1 criteria.

---

## 4. Phase 1 Product Scope

Phase 1 focuses on the minimum useful automated workflow:

1. Scrape current Amsterdam for-sale listings from Funda.
2. Extract the required listing information.
3. Identify listings that have not previously been seen.
4. Store listing information locally.
5. Apply the agreed property filters.
6. Send a Telegram notification for each newly detected matching listing.
7. Run the process periodically.

The first implementation should prioritize reliability and clarity over
advanced features.

---

## 5. Listing Information

The system should attempt to collect the following information for each
listing:

* Funda listing ID
* Funda URL
* address
* neighborhood
* asking price
* living area in m²
* plot size in m², where available
* number of rooms
* number of bedrooms, where available
* property type
* construction year, where available
* energy label, where available
* current status
* first-seen timestamp
* whether a notification was sent

The exact technical extraction method belongs to `architecture.md`.

---

## 6. New Listing Detection

A listing is considered new when its unique Funda listing identifier has not
previously been stored by the agent.

The system should not repeatedly notify the owner about the same listing simply
because the scraper encounters it again during a later run.

The initial deduplication key is the Funda listing ID derived from the listing
URL.

---

## 7. Phase 1 Filtering Criteria

The confirmed Phase 1 filtering criteria are:

* **Price:** €550,000–€750,000 (Confirmed single source of truth for Phase 1)
* **Bedrooms:** at least 3
* **Living area:** at least 100 m²

These values ship as the starting values written in the committed,
human-readable file `config/filters.json` (see `src/config.py`). There is no
code-level fallback: every filter key is equally optional, and a key that is
absent from the file (or set to `null`) becomes `None` on `FilterConfig`,
meaning "no restriction". The €550,000–€750,000 / ≥3 bedrooms / ≥100 m²
criteria are therefore only *the values currently written in the file*, not
defaults injected by the loader. `.env` is reserved for secrets and is not
used for filter configuration.

### Phase 2 preference filters

The base filters above can be narrowed with preferences configured in
`config/filters.json`, which is a single flat JSON object (one key per
filter). Every key is optional: `null` means "no restriction". The
preferences fall into two groups:

**Ranged filters (each has a `_min` and `_max` key):**

* **bedrooms** — `bedrooms_min` (≥3 as shipped in the file) and `bedrooms_max`.
* **living_area** — `living_area_min` (≥100 m² as shipped in the file) and
  `living_area_max`.
* **rooms** — `rooms_min` and `rooms_max` (total rooms, search-level only).
* **plot_size** — `plot_size_min` and `plot_size_max` (m²); also emitted as
  the `plot_area` search-URL parameter.
* **bathrooms** — `bathrooms_min` and `bathrooms_max` (search-level only).
* **garage_capacity** — `garage_capacity_min` and `garage_capacity_max`
  (search-level only).

> **Superseded (filters task):** the `energy_label` ranged preference
> (`energy_label_min` / `energy_label_max`, G (lowest) → A++++ (highest)) was
> removed and replaced by the ordered `energy_labels` list below.

**Single-value filters:**

* **property_type** — only listings of this type match (e.g. `appartement`).
* **transaction_type** — `koop` (for sale, the default) or `huur` (rent).
* **radius_km** — a search radius in kilometres around Amsterdam, sent as a
  separate `radius_search` parameter (the old embedded `selected_area`
  JSON-array encoding is superseded).
* **selected_area** — the area slug to search (e.g. `amsterdam`, or `"nl"`
  for nationwide; no code-level default — `None` when unset), emitted as
  `selected_area`.

**List-valued and other search-level filters:**

* **construction_type** — a multi-value list of construction types using
  Funda's tokens `newly_built` / `resale` (the legacy `new` / `existing` are
  accepted and mapped). `null` means no restriction.
* **object_type** — a multi-value list (`apartment`, `house`).
* **exterior_space_type** — a multi-value list (`balcony`, `terrace`,
  `garden`).
* **exterior_space_garden_orientation** — a multi-value list (`north`,
  `east`, `south`, `west`).
* **zoning** — a multi-value list (`residential`, `recreational`).
* **parking_facility** — a multi-value list of parking-facility types.
* **garage_type** — a multi-value list of garage types.
* **accessibility** — a multi-value list of accessibility features.
* **amenities** — a multi-value list of amenities.
* **energy_labels** — an ordered list of energy labels (e.g. `["A++++",
  "A+++", "A++", "A+", "A", "B", "C", "D", "A+++++"]`) sent to Funda verbatim
  in the configured order. The default order is unusual (`A+++++` appears
  last, after `D`) and is preserved exactly as captured from the
  authoritative source URL — do not silently reorder it.
* **construction_periods** — build-year periods expressed as human-readable
  keys (`"1971-1980"`, `"1981-1990"`, …, `"after_2020"`), mapped to Funda's
  internal codes via `CONSTRUCTION_PERIOD_MAP`.
* **garden** — boolean legacy shorthand; `true` is equivalent to including
  `"garden"` in `exterior_space_type`.
* **garden_size_min** — minimum garden size in m²; only meaningful when a
  garden is selected (`garden=true` or `"garden"` in `exterior_space_type`).
* **availability** — free-string availability filter (e.g. `"available"`).
* **sort** — free-string sort ordering (e.g. `"publish_date_utc_desc"`).

These list-valued, boolean, and free-string filters are **search-level only**
and, like `rooms_min`/`rooms_max`, `radius_km`, `selected_area`, and
`construction_type`, are not applied in the local storage matching query
(`plot_size_min`/`max` is the exception — it is applied in both).

When a preference is unset (`null` in `config/filters.json`), it imposes no
restriction. For ranged filters a `null` bound leaves that side open-ended.
A listing whose optional field is NULL never satisfies an enabled preference
filter.

### Resolution of Price Range Requirement

The price range has been explicitly confirmed by the project owner as **€550,000–€750,000** for Phase 1. This range serves as the active single source of truth across all project documentation. As of Phase 2 the values are configurable by editing `price_min` / `price_max` in `config/filters.json`, with €550,000–€750,000 shipped as the starting values in that file.

---

## 8. Telegram Notification

For a newly detected listing that matches the confirmed Phase 1 criteria,
the agent should send a Telegram notification.

At minimum, the notification should contain:

* address
* asking price
* living area
* number of rooms
* Funda listing URL

The delivered notification (MVP extension, implemented) goes beyond this
minimum and includes:

* bedrooms, neighborhood, price per m²
* plot size, energy label, construction year
* number of stories (shown only when known), with an attic flag
* garden area and parking (always shown, values in English)
* a more precise location: city and street (area/district and postal
  code are not extracted and are never invented), with a "Location On
  Map" link inline on the Location line (the English variant of the
  listing URL with a `/kaart` suffix)
* a `View on Funda` link on its own line after the Bottom line,
  pointing at the English variant of the listing URL (no `/kaart`
  suffix). The English and map URLs are derived at format time from the
  stored canonical URL; the database continues to store the original
  non-English URL unchanged.
* **up to 3 property photos of the same listing**, delivered together
  with the text as one coherent Telegram media message: the
  notification text rides as the caption of the photo (or photo
  album). Photos are best-effort: if fewer than 3 are available or
  some fail to download, the valid ones are still sent; the text
  notification is never withheld because images are missing (it
  degrades to a text-only message).

The notification text follows the owner-approved template: the bold
address as title, then metric-only lines, each following
`EMOJI + English metric name + ":" + value` (e.g. `💰 Price: €599,000`,
`🏠 Living area Size: 133 m² · €4,504/m²`, `🅿️ Parking: Available
(Parkeervergunning)`). The match score is **not displayed** (score
calculation and score-based logic remain unchanged). The address is
kept exactly as provided by the listing. No Dutch property terminology
is displayed outside parentheses: property type (e.g.
`Eengezinswoning`), listing status (e.g. `Beschikbaar`) and ownership
wording (`Eigendom`/`Erfpacht`) are omitted and are not replaced with
invented English translations; other non-numeric values (parking
types) are converted to English at presentation level, keeping the
original Dutch term in parentheses where the scraped value is Dutch.

Fields that a listing does not expose are omitted from the message —
values are never invented or shown as placeholders. The `🏢 Stories`
line is shown only when the story count is known (`{stories}`), with
` + Attic` appended when an attic is present; the line is omitted
entirely when the story count is unknown (an attic without a known
story count is not shown).
Every notification carries `🟢 Pros` / `🔴 Cons` bullet lists (max 5
each) and a one-sentence `Bottom line`: bullets are generated first
from phrases actually present in the extracted property description,
then from the listing's own data — never fabricated.

---

## 9. Notification Rules

The intended notification rule is:

```text
Listing is new
        AND
Listing matches all confirmed Phase 1 filters
        ↓
Send Telegram notification
```

A listing that does not match the filters may still be stored so that the
system knows it has already been seen.

The same listing should not generate repeated "new listing" notifications on
every scheduled run.

### Re-notification on price or status change

> **Superseded (Task 1):** The following behavior was removed. It is preserved
> here for historical context. See `Architecture.md` §"Re-notification on price
> or status change (superseded)" for the architectural impact.

<!-- SUPERSERVED BY TASK 1: The re-notification-on-price-or-status-change
     behavior was removed. `notified` is no longer reset by `insert_listing()`
     when price or status changes. It is only changed by `mark_as_notified()`
     or the filter-change logic (Task 2). -->

When a scraped listing already exists in the database, the system compares
the new scraped values against the stored row.

* **Price changed** (increase or decrease): the listing is updated,
  `notified` is reset to 0, and the listing re-enters the matching and
  notification flow on this run. If it matches the Phase 1 filters, a
  Telegram notification is sent.
* **Status changed** (e.g. from "beschikbaar" to "verkocht" or vice versa):
  the listing is updated, `notified` is reset to 0, and the listing
  re-enters the matching and notification flow on this run.
* **Neither price nor status changed**: the listing's other fields are
  updated in the database, but `notified` is left untouched. No
  re-notification is triggered.

This ensures the owner is alerted when a previously-seen listing becomes
relevant (price drop into range, or status change to available) without
generating duplicate notifications for unchanged listings.

### First-run notification gating after a filter change (Task 2 — generalized)

> **Superseded (Task 4):** The gating condition was generalized from
> "first run after filter change" to "full scan" (`run_is_full_scan`).
> Gating now also triggers when the last successful run was more than
> 3 days ago (staleness fallback).  The mechanics (70-point threshold,
> newly-inserted-only, suppressed listings marked notified=1) are unchanged.

When `config/filters.json` is edited and the scraper is run, the very next
run must not blast a notification for every listing that suddenly matches
the new criteria.  Instead:

* For every **genuinely new listing inserted into the database during that
  first run**, the listing is saved normally, goes through the detail-page
  scraping and scoring workflow, and then:

  * If **`score >= 70`**: send the Telegram notification normally, then set
    `notified = 1`.
  * If **`score < 70`** (or no score is available): do **not** send a
    notification, but set `notified = 1` so it does not linger and get
    notified later purely because it was skipped on this run.

* This gating applies **only** to listings that are actually newly inserted
  into the database during that first run.  Existing (previously stored)
  listings that now match the new filters are **not** treated as new.

* Gating is triggered whenever `run_is_full_scan` is `True`:
  * **Filter change:** the saved filter snapshot differs from the current
    filters (or no snapshot exists — first run ever).
  * **Staleness fallback:** more than 3 days have passed since the last
    successful run (or no successful run has been recorded).
  * **Both:** both conditions are true.

* On delta scans (filters unchanged AND last run within 3 days), behaviour
  is unchanged: all matching unnotified listings are notified normally
  through the existing workflow.

The filter snapshot used to detect changes is stored in the SQLite database
in the `scraper_metadata` table (key: `filter_snapshot`).  It is saved after
each run so that the next run can compare the stored snapshot against the
currently loaded `FilterConfig`.

### Run summary output

When gating is active, the run summary includes:

```
  Scan mode:      FULL (filter changed and stale fallback)
  Full-run gate:  ENABLED
  Newly suppressed (<70):  3
  Newly notified (>=70):   2
```

On a delta scan:

```
  Scan mode:      DELTA (3-day publication filter)
```

---

## 10. Scheduling

> **Superseded (Task 5):** The schedule was changed from approximately every
> 30 minutes to approximately every 5 hours to reduce request frequency and
> align with the delta-scan model.

The initial intended schedule is periodic checking approximately every
5 hours.

The exact scheduling mechanism is an implementation/operations decision and
is currently planned to use cron during Phase 1.

The schedule may be adjusted later if testing shows that:

* Funda detection/blocking becomes an issue
* the frequency is unnecessarily aggressive
* the frequency is too slow for the intended use

---

## 11. Product Constraints

The initial product must respect the following constraints:

### Funda only

The system must not scrape other real-estate platforms unless the project
scope is explicitly expanded.

### No paid scraping infrastructure

The product must not depend on:

* paid proxy services
* paid CAPTCHA solving
* paid scraping APIs
* paid AI/API tiers

without explicit owner approval.

### Low resource usage

The agent runs on a VPS with approximately 4GB RAM and should avoid
unnecessary resource-intensive processing.

### Internal tool

This is an internal/personal automation tool rather than a public SaaS
product.

---

## 12a. Ranking and Scoring

> **Revised (2026-08-20):** This section was updated to reflect the current
> 12-criterion scoring system. The previous version described 9 criteria
> (including bathrooms and amenities, which have since been removed). Four
> new criteria were added: garage, plot size, balcony, and heating type.
> The ownership and energy-label formulas were also changed to continuous
> scales rather than discrete tiers.

Every listing that passes the Phase 1 hard filters is scored before
notification. The score reflects how well a listing matches the owner's
preferences across twelve weighted criteria:

1. **Neighborhood value** — asking price per m² relative to the
   neighborhood average. A listing priced significantly below the average
   scores highest.

2. **Ownership** — whether the property is fully owned or subject to
   erfpacht (ground lease). Rather than a flat three-tier split, the
   current implementation uses a continuous scale: full ownership scores
   highest; erfpacht with no or zero annual canon scores high; erfpacht
   with a positive canon scales linearly downward as the annual canon
   increases.

3. **Energy label** — from G (lowest) to A++++ (highest). Lower labels
   receive disproportionately more weight than higher labels (a concave
   curve: the score gap between G and F is larger than between A+++ and
   A++++).

4. **Living area** — how far the listing's living area extends beyond
   the configured minimum, scaled linearly to a cap defined in
   `config/preferences.json`.

5. **Construction condition** — building age and insulation quality.
   Insulation quality now contributes more to the score than construction
   year (65% insulation, 35% year). The year bounds are configurable in
   `config/preferences.json`.

6. **Garage** — presence and type of garage. Funda omits the entire
   Garage section when a listing has no garage, so a missing
   garage_type is treated as a confirmed negative (scores as a real 0)
   rather than missing data. Presence of a dedicated garage (inpandige,
   aangebouwde, vrijstaande, etc.) scores progressively higher.

7. **Parking** — type of parking arrangement (private carport, own
   property, permit, paid, public). A combined "TypeA + TypeB" value
   is handled by scoring only the first segment (a known limitation
   documented in `docs/site-notes/funda.md`).

8. **Rooms** — total room count relative to the configured bedrooms
   minimum, scaled linearly to a cap defined in
   `config/preferences.json`.

9. **Plot size** — the size of the building plot in m², scored linearly
   with "more is better" up to a configurable cap. A missing value is
   treated as data unavailable rather than a confirmed negative, since
   it is ambiguous whether a missing value means "no private plot" or
   "failed to parse."

10. **Garden** — presence, size, and orientation (south/west bonus).
    A confirmed absence (garden_present = False) scores 0.

11. **Heating type** — whether the property uses a heat pump, district
    heating, or gas boiler. Every home has some form of heating; if this
    field is missing it is treated as likely a parsing miss (data
    unavailable) rather than a genuine absence of heating.

12. **Balcony** — whether a balcony or rooftop terrace is present.
    Funda only shows this field when one exists, so a missing value is
    treated as a confirmed negative (no balcony), mirroring the garage
    pattern.

**Removed criterion:** "Bathrooms" was removed from scoring. Testing
showed it provided zero differentiation across sampled real listings —
all scored identically on bathrooms — making it empirically
non-discriminating for the Amsterdam market.

Each criterion contributes a weighted subscore. The final score is a
0–100 number, renormalized so that only criteria with available data
contribute to the total. When a criterion has no data, it is excluded
from the calculation rather than penalized.

A confidence flag accompanies each score:

* **Full** — all criteria have data.
* **Partial** — some criteria are missing data (the notification
  indicates which ones).
* **Partial major missing** — the single most heavily-weighted criterion
  (determined dynamically from the weight table, not hardcoded to a
  specific criterion name) is among the missing criteria. This is a
  stronger low-confidence signal than an ordinary partial score, since
  the criterion carrying the most weight could not be evaluated.
* **No data** — no scoring data is available (score shown as
  "unavailable").

The exact weights and keyword dictionaries are configurable via
`config/preferences.json` and are not considered product-scope changes
when adjusted.

---

## 12. Phase Roadmap & Definition of Done

### Phase 1 — Basic Monitoring

Goal:

* Funda Amsterdam scraping
* listing extraction
* new-listing detection
* SQLite storage
* confirmed property filtering
* Telegram notification
* periodic execution

#### Phase 1 Definition of Done (DoD)

Phase 1 is considered complete and ready for production monitoring when all of the following criteria are verifiably met:

1. **Scraping & Data Extraction:**
   * Scraper successfully extracts live Amsterdam for-sale listings from Funda without throwing unhandled exceptions.
   * All mandatory listing fields (Funda ID, URL, address, asking price, living area m², total rooms) are parsed correctly and consistently.

2. **Deduplication & Local Storage:**
   * Scraped listings are stored locally in SQLite with all mandatory metadata fields and timestamps.
   * Subsequent scraper runs correctly identify previously seen listings by Funda Listing ID and do not treat them as new.

3. **Filter Accuracy:**
   * Filters strictly evaluate the confirmed criteria: Price €550,000–€750,000, Bedrooms ≥ 3, Living Area ≥ 100 m².
   * Non-matching listings are stored in the database but do NOT trigger Telegram notifications.
   * Matching new listings consistently trigger notifications.

4. **Notification Delivery:**
   * Telegram messages are successfully dispatched to the configured `TELEGRAM_CHAT_ID` using `TELEGRAM_BOT_TOKEN`.
   * Message layout includes all required fields (Address, Price, Living Area, Rooms, Direct Funda URL) and renders cleanly on Telegram clients.

5. **Operational Health & Anti-Bot Safety:**
   * Running the scraper sequentially or via scheduled triggers produces zero duplicate alerts.
   * Request delays and browser resource management comply with the 4GB RAM ceiling and anti-bot pacing rules.
   * Diagnostic logging records execution steps, listings evaluated, alerts sent, and any extraction anomalies.

### Phase 2 — Improved Filtering

Completed:

* Detail-page scraping and preference-based scoring (Section 12a).
* Configurable search filters (Steps 1–4): `FilterConfig` and
  `DEFAULT_FILTERS` in `src/config.py`, loaded from the human-editable
  `config/filters.json` file via `FilterConfig.from_file()`, applied to the
  Funda search and the storage matching query. The Phase 1 filter values
  are shipped as the starting values in `config/filters.json`.
* Stale-listing archival: listings not seen in a scrape for
  `config/retention.json`'s `stale_days` (default 60) are moved to a
  `listings_archive` table rather than deleted, preserving them for
  future historical analysis (Phase 4) while keeping the live `listings`
  table bounded. Independent of filter changes — runs automatically every
  normal run, not wired into `--backfill` or `--seed`.

Remaining (not yet implemented):

* deterministic ranking/ordering of matching listings
* notification formatting expansions (neighborhood, property type, energy
  label, plot size, bedrooms)

Phase 2 is not yet fully complete; the remaining items above are future work.

### Phase 3 — Reliability and Operations

Potential future work:

* stronger scheduling/monitoring
* failure detection
* better operational reporting
* scraper health monitoring

### Phase 4 — Historical Analysis

Potential future work:

* price trends
* listing history
* property comparisons
* historical market analysis

These phases are roadmap ideas and must not be implemented prematurely
without a task explicitly requesting them.

---

## 13. Out of Scope

The following are currently outside the product scope:

* scraping other real-estate websites
* public web dashboards
* user accounts
* multi-user support
* mobile applications
* automatic house purchasing
* automatic bidding
* contacting real-estate agents automatically
* paid proxy/CAPTCHA infrastructure
* predictive property valuation
* advanced machine-learning recommendation systems

These may only be introduced through an explicit scope decision.

---

## 14. Confirmed Product Decisions

The following product decisions have been confirmed and serve as project ground truth:

### Price range
* **Status:** Confirmed.
* **Value:** €550,000–€750,000.
* **Notes:** Serves as the default single source of truth for Phase 1 filtering logic. Configurable in Phase 2 by editing `price_min` / `price_max` in `config/filters.json`; €550,000–€750,000 is shipped as the starting value in that file (no code-level fallback).

---

## 15. Product Source of Truth

When implementation behavior conflicts with this document, the discrepancy
must be investigated rather than silently resolved.

Changes to product scope or requirements must be reflected in this file.

Technical implementation decisions belong in `architecture.md`.

Operational procedures belong in `operations.md`.
