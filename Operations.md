# operations.md — Amsterdam Funda Home-Search Agent

## Purpose

This document describes how to run, test, debug, and operate the Amsterdam Funda Home-Search Agent.

It complements:

* `AGENTS.md` — agent behavior, development constraints, and collaboration rules
* `product.md` — product scope and functional requirements
* `architecture.md` — technical design and architectural decisions

This file focuses on **execution and operations**, not product requirements or implementation details.

---

## 1. Operating Environment

The project runs on a shared Ubuntu VPS.

Current environment assumptions:

* Ubuntu VPS
* 4 GB RAM
* 2 GB swap
* Python 3.12
* Project-specific Python virtual environment: `.venv`
* Git/GitHub access through SSH
* tmux
* outbound-only network access
* no inbound application service is required for Phase 1

The project should run under a dedicated non-root Linux user.

Do not run the application as root unless a future operational requirement explicitly justifies it.

---

## 2. Development and Production Separation

Development and production execution are separate concerns.

### Development

The AI coding agent runs inside the developer's tmux session.

Example:

```text
SSH
└── developer Linux user
    └── tmux
        └── agent-work
            └── OpenCode
```

The `agent-work` session is for:

* working with OpenCode / Gemini CLI
* reading project files
* editing code
* running tests
* running limited/manual scraper tests
* debugging

### Manual scraper/debugging

A second tmux session named `scraper` is reserved for manual scraper execution and debugging.

It is not the production scheduler.

Example:

```bash
tmux new -s scraper
```

The `scraper` session should only be used when interactive execution or debugging is useful.

### Production

Production runs should not depend on an active tmux session.

Phase 1 production execution uses cron.

The intended production flow is:

```text
cron
  ↓
Python entry point
  ↓
scrape Funda
  ↓
store/deduplicate
  ↓
filter
  ↓
notify Telegram
  ↓
exit
```

The process should terminate after completing a run.

---

## 3. Project Virtual Environment & Browser Dependency Rule

Python dependencies must be installed into the project's `.venv`.

Before running the application manually:

```bash
source .venv/bin/activate
```

Verify:

```bash
python --version
which python
```

The expected Python version is Python 3.12.

Do not install project dependencies globally.

### Browser Dependency Rule
**Playwright and browser binaries (Chromium) must ONLY be installed when explicitly requested by a task.**

* Do not install Playwright, Chromium, or OS-level browser dependencies during initial environment setup or as speculative preparation for future tasks.
* Before installing browser dependencies, agents must confirm that the task explicitly requires installation and that the system remains within the 4GB VPS memory ceiling.

---

## 4. Environment Variables and Secrets

Secrets are stored in a `.env` file at the project root.

Expected secret variables:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_FAILURE_TOPIC_ID=
```

`.env` is reserved for secrets and environment-specific sensitive values
(such as Telegram credentials). Search filters are **not** configured here.

### Search filter configuration (`config/filters.json`)

The owner changes normal property-search filters by editing the committed,
human-editable JSON file:

```text
config/filters.json
```

The file is organised into two clearly labelled sections so it stays easy to
read without touching source code:

```json
{
    "note": "Human-editable housing search filters. See the table below for types, defaults, and valid values.",
    "required": {
        "price_min": 550000,
        "price_max": 750000,
        "bedrooms_min": 3,
        "living_area_min": 100
    },
    "optional": {
        "note": "null/[] = no restriction. Multi-value filters are ordered JSON arrays; ranged filters use _min/_max pairs (null = open bound).",
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
}
```

* `required` — the four Phase 1 base criteria, kept at their current values.
* `optional` — every optional preference key; `null` (or `[]` for multi-value
  filters) means "no restriction", so an unused preference can simply be left
  as `null`.

Each key controls the housing search criteria:

| Key                                  | Type       | Default    | Meaning                                        |
| ------------------------------------ | ---------- | ---------- | ---------------------------------------------- |
| `price_min`                          | int        | 550000     | Minimum asking price (€)                       |
| `price_max`                          | int        | 750000     | Maximum asking price (€)                       |
| `bedrooms_min`                       | int        | 3          | Minimum bedrooms                               |
| `bedrooms_max`                       | int / null | none       | Maximum bedrooms                               |
| `living_area_min`                    | int        | 100        | Minimum living area (m²)                       |
| `living_area_max`                    | int / null | none       | Maximum living area (m²)                       |
| `rooms_min`                          | int / null | none       | Minimum total rooms                            |
| `rooms_max`                          | int / null | none       | Maximum total rooms                            |
| `plot_size_min`                      | int / null | none       | Minimum plot size (m²); also emitted as `plot_area` on the search URL |
| `plot_size_max`                      | int / null | none       | Maximum plot size (m²)                         |
| `property_type`                      | str / null | none       | Required property type, e.g. `appartement`     |
| `energy_labels`                      | list / null | none      | Ordered energy labels sent to Funda verbatim   |
| `transaction_type`                   | str / null | none       | `koop` (for sale) or `huur` (rent)             |
| `radius_km`                          | int / null | none       | Search radius (km); emitted as `radius_search` |
| `selected_area`                      | str / null | `amsterdam` | Area slug (e.g. `amsterdam`, `nl`)            |
| `construction_type`                  | list / null | none      | Construction types: `newly_built`/`resale` (legacy `new`/`existing` mapped) |
| `construction_periods`               | list / null | none      | Human-readable build-year periods (mapped)     |
| `object_type`                        | list / null | none       | Object types: `apartment`, `house`             |
| `bathrooms_min`                      | int / null | none       | Minimum bathrooms                              |
| `bathrooms_max`                      | int / null | none       | Maximum bathrooms                              |
| `garage_capacity_min`                | int / null | none       | Minimum garage capacity                        |
| `garage_capacity_max`                | int / null | none       | Maximum garage capacity                        |
| `exterior_space_type`                | list / null | none       | Exterior spaces: `balcony`, `terrace`, `garden` |
| `exterior_space_garden_orientation`  | list / null | none       | Garden orientations: `north`, `east`, `south`, `west` |
| `garden`                             | bool / null | none       | Legacy shorthand: `true` adds `garden` to `exterior_space_type` |
| `garden_size_min`                    | int / null | none       | Minimum garden size (m²); requires a garden selected |
| `zoning`                             | list / null | none       | Zoning: `residential`, `recreational`          |
| `parking_facility`                   | list / null | none       | Parking facility types (see Architecture.md vocabulary table) |
| `garage_type`                        | list / null | none       | Garage types (see Architecture.md vocabulary table) |
| `accessibility`                      | list / null | none       | Accessibility features (see Architecture.md vocabulary table) |
| `amenities`                          | list / null | none       | Amenities (see Architecture.md vocabulary table) |
| `availability`                       | str / null | none       | Free-string `availability` value               |
| `sort`                               | str / null | none       | Free-string `sort` value                       |

> **Superseded:** the `energy_label_min` / `energy_label_max` keys were
> removed and replaced by the single ordered `energy_labels` list. The
> `energy_labels` default order (`A++++, A+++, A++, A+, A, B, C, D,
> A+++++`) is preserved exactly as it appeared in the authoritative source
> URL, not sorted ordinally — do not silently reorder it.

The `null` optional values mean "no preference filter" (an empty `[]` for a
multi-value filter is also rejected, matching the `energy_labels`/`construction_periods`
convention — use `null` to mean "no restriction"). Missing keys fall back to
the Phase 1 defaults (€550,000–€750,000, ≥3 bedrooms, ≥100 m²).
Ranged filters accept `_min`/`_max` pairs; a `null` bound leaves that side
open-ended. Search-level filters are applied to the Funda search URL only (not
the local matching query); these are `rooms_min`/`rooms_max`, `radius_km`,
`selected_area`, `construction_type`, `energy_labels`, `construction_periods`,
`object_type`, `bathrooms_min`/`max`, `garage_capacity_min`/`max`,
`exterior_space_type`, `exterior_space_garden_orientation`, `zoning`,
`parking_facility`, `garage_type`, `accessibility`, `amenities`, `garden`,
`garden_size_min`, `availability`, and `sort`. `plot_size_min`/`max` is used
both in the storage matching query and as the `plot_area` search-URL
parameter. `radius_km` is emitted as its own `radius_search` parameter;
`selected_area` is a plain area slug (default `amsterdam`). `construction_periods`
uses human-readable keys that `src/config.py` maps to Funda's internal codes
via `CONSTRUCTION_PERIOD_MAP`.

The `construction_type` field is multi-value (Funda tokens `newly_built` /
`resale`); the legacy `existing`/`new` are accepted and mapped. The `garden`
boolean is a legacy shorthand for `exterior_space_type` containing `"garden"`.
A `note` (or `_comment`) key is tolerated anywhere in the file for
documentation; any other unknown key, an invalid value, or a key placed in the
wrong section causes the run to fail loudly rather than being silently
coerced. The older flat layout (all filter keys at the top level, no sections)
is still accepted for backward compatibility. The file is resolved from the
project root regardless of the working directory, so cron/systemd/tmux
execution finds it automatically. See `src/config.py` for the authoritative
key list and validation rules.

The real `.env` file must never be committed to Git.

`.gitignore` must exclude it.

A safe template should be committed:

```text
.env.example
```

containing only placeholder values.

Example:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_FAILURE_TOPIC_ID=
```

Never print the Telegram bot token in logs or terminal output.

If a secret is accidentally committed, stop and treat it as a security incident rather than simply deleting it from the latest commit.

### Retention policy configuration (`config/retention.json`)

This file governs the not-yet-implemented stale-listing archival feature
(Task 3 of 4).  It is intentionally a **separate** file from
`config/filters.json` (search criteria) and `config/preferences.json`
(scoring weights) because data retention is an unrelated operational
concern — it controls how long listings are kept before archival, not how
they are found or ranked.

```text
config/retention.json
```

| Key          | Type   | Default | Meaning                                           |
| ------------ | ------ | ------- | ------------------------------------------------- |
| `stale_days` | int    | 60      | Days since last seen before a listing is archived |

A value of `60` means a listing whose `last_seen_at` is older than 60 days
is eligible for archival.  The value must be a positive integer; `0` and
negative values are rejected.  Missing `stale_days` falls back to 60.
Unknown keys raise `ValueError`.

See `src/config.py` for the authoritative key list and validation rules.

### First-run notification gating after a filter change (Task 2 — generalized)

> **Superseded (Task 4):** The gating condition was generalized from
> "first run after filter change" to "full scan" (`run_is_full_scan`).
> Gating now also triggers when the last successful run was more than
> 3 days ago (staleness fallback).  The mechanics (70-point threshold,
> newly-inserted-only, suppressed listings marked notified=1) are unchanged.

When `config/filters.json` is edited or the scraper hasn't run successfully
in over 3 days, the next scraper run will **not** blast a notification for
every listing that suddenly matches the criteria.

**What triggers gating:**

Gating is triggered whenever `run_is_full_scan` is `True`:
* **Filter change:** the saved filter snapshot differs from the current
  filters (or no snapshot exists — first run ever).
* **Staleness fallback:** more than 3 days have passed since the last
  successful run (or no successful run has been recorded).
* **Both:** both conditions are true.

**What happens:**

1. The scraper loads the new filters from `config/filters.json`.
2. It compares them against the previously saved filter snapshot stored in
   the SQLite database (`scraper_metadata` table, key `filter_snapshot`).
3. If the snapshot is absent (first run ever) or differs from the loaded
   filters, the run is treated as "first run after filter change."
4. During this run, every **genuinely newly inserted** listing goes through
   the normal detail-page scraping and scoring workflow.  The score is then
   compared against a fixed threshold of **70**:
   * `score >= 70` → Telegram notification sent, `notified = 1`
   * `score < 70` → notification suppressed, `notified = 1`
5. After the run completes, the new filter snapshot is saved to the
   database.  Subsequent runs (with unchanged filters) behave normally.

**Important:** Only listings that are actually newly inserted into the
database during that first run are gated.  Existing listings that now match
the new filters are **not** treated as new and are not affected by this
gating.

**Run summary output:** when gating is active, the run summary includes:

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

**To change filters:**

1. Edit `config/filters.json` with your new criteria.
2. Run the scraper normally (`python -m src.main`).
3. Check the run summary for the gating output.
4. On subsequent runs, the gating is disabled automatically.

**No manual intervention needed.** The filter snapshot is managed
automatically by the scraper.  The `--dry-run`, `--seed`, and `--backfill`
flags are unaffected by this gating logic.

---

## 5. Database and Runtime Data

The application uses SQLite.

Expected database location:

```text
data/funda.db
```

The database is runtime data and should not be committed to Git.

The database stores listings and allows the scraper to distinguish previously seen listings from newly discovered listings.

### Automatic schema migration

On every startup (`init_db()` in `src/storage.py`) the application
checks the live `listings` table and adds any missing columns with
idempotent, non-destructive `ALTER TABLE ADD COLUMN` statements. No
manual migration step is required after deploying a new feature that
extends the schema — for example, legacy databases created before the
rich-photo feature automatically gained `last_seen_at` and
`image_urls` (JSON TEXT) on their first run afterwards. Existing rows,
notified flags, scores, and all previously stored fields are never
modified by the migration; new columns start as NULL and fill in as
listings are re-fetched. To verify a database's schema:
`sqlite3 data/funda.db "PRAGMA table_info(listings);"`.

Important operational behavior:

```text
Scrape listing
      ↓
Check listing_id
      ↓
Already in database?
   ┌──┴──┐
  YES    NO
   ↓      ↓
Ignore  Insert
          ↓
     Apply filters
          ↓
       Notify
```

The database should be treated as persistent application state.

Do not delete or recreate it casually because doing so would cause previously seen listings to appear new again.

Any destructive database operation requires explicit approval.

### Stale-listing archival

Every normal run (not `--backfill` or `--seed`) automatically archives
stale listings.  A listing is considered stale when its `last_seen_at`
is older than the number of days configured in
`config/retention.json` (`stale_days`, default 60).

**What happens:** stale listings are moved atomically from `listings` to
`listings_archive` — the archive table is an exact-schema mirror of
`listings`.  Archived rows remain directly queryable via SQL for
historical analysis (e.g. `SELECT * FROM listings_archive WHERE ...`).
They are **deleted** from the live `listings` table.

**Run summary output:** the summary includes an "Archived" line showing
how many listings were moved in that run:

```
  Archived:           3
```

If no listings are stale, the line shows `Archived: 0`.

**Automatic, no flag needed:** archival runs on every normal run without
a CLI flag.  It behaves identically in `--dry-run` and normal mode.

**Failure handling:** if archival fails (e.g. SQLite error), the failure
is logged at ERROR level with `exc_info=True` and the run continues.
This is **not** the same as the run-correctness failures documented in
the "Error Handling" section (section 15) — a failed archival does **not**
fail the run, does **not** set `notified` back to 0 on affected listings,
and does **not** trigger a Telegram failure alert.  Only notification
failures and scrape/DB-init failures trigger the Telegram alert.

---

## 6. Logs

The project should maintain application-level logs separately from cron output.

Expected locations:

```text
logs/
├── scraper.log
└── cron.log
```

The directories/files may not exist during initial development and should be created as part of operations setup.

### Application log

`scraper.log` should contain enough information to understand each run without exposing secrets.

At minimum, a normal successful run should record:

* start time
* end time
* number of pages processed
* number of listings scraped
* number of new listings
* number of listings matching the filter
* number of Telegram notifications sent
* errors or warnings

Example conceptual output:

```text
Run started
Pages processed: 4
Listings scraped: 96
New listings: 12
Matching listings: 3
Notifications sent: 3
Run completed
```

Do not log:

* Telegram bot token
* sensitive environment values
* unnecessary personal information

---

## 7. Cron Output

Cron should redirect stdout/stderr to:

```text
logs/cron.log
```

The exact cron configuration should be kept simple and documented.

> **Superseded (Task 5):** The target frequency was changed from approximately
> every 30 minutes to approximately every 5 hours.

Phase 1 target frequency is approximately every 5 hours.

The production scheduler should run the Python application directly rather than depending on:

```text
tmux
screen
OpenCode
Gemini CLI
```

The AI coding environment must never be required for production execution.

---

## 8. Scheduled Execution

> **Superseded (Task 5):** The schedule was changed from every 30 minutes to
> every 5 hours.

The intended Phase 1 schedule is approximately:

```text
Every 5 hours
```

The scraper should:

1. Start.
2. Launch one browser instance.
3. Visit the relevant Amsterdam Funda search.
4. Extract current listings.
5. Compare listing IDs with SQLite.
6. Store newly discovered listings.
7. Apply the confirmed Phase 1 criteria.
8. Send Telegram notifications for matching new listings.
9. Log the result.
10. Close the browser.
11. Exit.

The application must not remain resident between scheduled runs.

---

## 9. Resource Management

The VPS has approximately 4 GB RAM.

The scraper must therefore remain conservative with resources.

Rules:

* Playwright and browser binaries must only be installed when explicitly requested by a task.
* Run at most one browser instance concurrently.
* Avoid unnecessary parallel scraping.
* Do not launch multiple Chromium instances for a single run.
* Prefer incremental processing where practical.
* Close browser/context/page resources after each run.
* Do not keep unnecessary data in memory.
* Avoid aggressive polling.

If a proposed implementation is likely to cause a significant memory spike, stop and flag it before implementing.

---

## 10. Funda Scraping Behavior

Funda is the only supported listing source.

The scraper should behave as an infrequent, normal browser visitor rather than continuously polling the site.

Operational principles:

* Use realistic pacing between page loads.
* Avoid rapid-fire requests.
* Keep scheduled frequency low.
* Use a single browser instance.
* Reuse browser/session state where the architecture permits it.
* Do not aggressively retry after blocking or challenge responses.

If Funda blocks or challenges the scraper:

1. Stop aggressive retries.
2. Record the failure clearly.
3. Diagnose the behavior.
4. Document the finding in `docs/site-notes/funda.md` when a scraper issue is fixed.
5. Do not introduce paid proxies, CAPTCHA-solving services, or other paid workarounds without explicit approval.

---

## 11. Manual Run

Before enabling cron, the scraper must be executable manually.

The intended manual workflow is conceptually:

```bash
source .venv/bin/activate
python -m src.main
```

The exact entry point may change if the project structure changes.

Manual execution is used for:

* initial testing
* debugging
* validating Funda access
* testing extraction
* testing database behavior
* testing notification behavior

Production scheduling should only be configured after manual execution is reliable.

---

## 12. Dry-Run / Limited Testing

A limited or dry-run mode should be preferred while developing.

The purpose is to validate:

* Funda access
* selectors
* parsing
* pagination
* extracted fields
* filtering
* database behavior

without unnecessarily sending real notifications.

When a task requires a new test mode or CLI option, document the behavior in the appropriate source/docs rather than inventing undocumented operational flags.

### CLI flags

`python -m src.main` supports:

* `--dry-run` — runs scraping, storage, and filtering but skips Telegram
  notifications. Listings are stored but never marked as notified, so a later
  real run can still notify them.
* `--db-path PATH` — overrides the SQLite database path. Defaults to
  `data/funda.db` under the project root.
* `--backfill` — one-time backfill for listings with `score IS NULL`.
  Queries listings that pass the active Phase 1 filters, fetches their
  detail pages, scores them with the current 9-criterion system, and
  updates the DB. Notifications are threshold-gated at 80 (configurable
  via `notification_score_threshold` in `config/preferences.json`):
  listings with score >= 80 get a Telegram notification; listings below
  80 have `notified = 1` set but no notification is sent, preventing
  them from re-entering the notification flow through unrelated triggers.
  Uses the same anti-bot pacing as a normal run (sequential, same delays,
  one browser instance). Does NOT run via cron — this is a manual,
  occasional operation.
* `--seed` — full pipeline (scrape, store, score) without sending any
  Telegram notifications. All matching listings are marked `notified = 1`
  so a subsequent normal run only notifies for genuinely new/changed
  listings. Use for initial database population or after a manual DB
  reset. Does NOT run via cron.

### Backfill — when and how to run

The `--backfill` flag is a **manual, occasional** operation. It is not part
of the cron schedule.

**When to run:**

* After a scoring system change (e.g. weight changes, criterion added/removed)
  — to re-score existing listings with the new criteria.
* After initial scoring implementation — to populate scores for all existing
  listings that were scraped before scoring was enabled.
* Ad-hoc: if you notice a batch of listings without scores and want to
  populate them.

**How to run:**

```bash
source .venv/bin/activate
python -m src.main --backfill --db-path data/funda.db
```

Use `--dry-run` first to preview what would happen without sending
notifications or marking listings as notified.

**What it does:**

1. Loads the active Phase 1 filters from `config/filters.json`.
2. Queries the database for listings that pass those filters AND have
   `score IS NULL`.
3. For each: fetches the detail page, scores with the current scoring
   system, persists the score and detail fields to the DB.
4. Threshold-gated notifications: score >= 80 triggers a Telegram
   notification and sets `notified = 1`; score < 80 sets `notified = 1`
   without notifying.
5. Logs a summary: listings found, backfilled, crossed threshold, failures.

**Anti-bot behavior:**

The backfill uses the same sequential detail-fetch + scoring logic as the
normal run path. No parallelism, same delays, one browser instance. It may
take a while for large batches — be patient.

### Seed run (`--seed`)

The `--seed` flag populates the database with full real data for the first
time (or after a manual DB reset). It runs the complete pipeline — scrape,
store, score — but suppresses all Telegram notifications.

**When to use:**

* Initial database population — when setting up the scraper for the first
  time or after a manual DB reset.
* Re-populating after a data loss event.

**When NOT to use:**

* This is not a replacement for normal scheduled runs.
* Do not add this to cron.

**How it differs from other modes:**

| Mode | Scrapes real data | Stores in DB | Scores matches | Sends notifications | Sets notified=1 |
|---|---|---|---|---|---|
| Normal run | Yes | Yes | Yes | Yes (for matching) | Yes (on success) |
| `--dry-run` | Yes | Yes | Yes | No | No (stays 0) |
| `--seed` | Yes | Yes | Yes | No | Yes (all matches) |

The key difference from `--dry-run`: seed sets `notified = 1` for all
matching listings, treating them as "already seen and already notified".
A subsequent normal run will only notify for listings that are genuinely
new or changed after the seed run.

**How to run:**

```bash
source .venv/bin/activate
python -m src.main --seed --db-path data/funda.db
```

**Output:**

The seed run logs a clear summary at the end:

```
SEED RUN COMPLETE
----------------------------------------
  Start:          2026-08-16T...
  End:            2026-08-16T...
  Duration:       312.4s
  Scraped:        96
  New inserted:   24
  Updated:        56
  Matching:       18
  Scored:         18
  Marked notified: 18
  Score failures: 0
----------------------------------------
SEED RUN — All 18 matching listing(s) marked notified=1 without sending
Telegram. A subsequent normal run will only notify for listings that are
genuinely new or changed after this point.
```

### Exit codes

`python -m src.main` exits with:

* `0` — successful run (notifications delivered, or dry-run).
* `1` — failed run. Possible causes: database initialisation failure, scraper
  exception, a scrape that returned 0 listings (may indicate an Akamai/reCAPTCHA
  challenge or a Funda page-structure change), a storage read failure, or one or
  more failed Telegram notifications.

A failed notification never marks a listing as notified, so a later run retries
it safely. The run is also treated as failed when the scrape returns 0 listings
so a Funda anti-bot interstitial is not silently reported as an empty
successful run.

### Failure alerts

When a run exits with code 1, the script sends a Telegram failure alert to a
dedicated topic (configured via `TELEGRAM_FAILURE_TOPIC_ID` in `.env`). The
alert contains a short reason derived from the recorded errors, e.g.:

```
⚠️ Funda scraper run failed: Scrape: Connection reset by peer. Check logs/cron.log and logs/scraper.log.
```

The alert is sent **after** the run summary is logged, so the logs contain
full diagnostic information.

The alert send is wrapped in try/except — if Telegram is unreachable or the
alert fails for any reason, the script still exits with code 1 as expected.
A failed alert send is logged but never raises.

Skipped listings (missing required fields) are logged at INFO level and counted
in the run summary under "Skipped". They do **not** trigger a failure alert.

---

## 13. Telegram Testing

Telegram notification should be tested separately from scraping where possible.

The test sequence should be:

```text
Scraper
   ↓
Extract listing
   ↓
Filter
   ↓
Notifier
   ↓
Telegram
```

A test notification should confirm:

* Telegram credentials are valid.
* The correct chat receives the message.
* The message contains the expected listing information.
* The Funda URL is present.
* No secret is included in the message.
* When the listing carries `image_urls` (detail-page scrape), the
  notification text and up to 3 photos of that listing arrive together
  as ONE media message (the text rides as the photo/album caption);
  with no `image_urls`, a text-only notification is delivered.

Real notification testing should not be mixed into every scraper debugging run.

---

## 14. End-to-End Verification

Before enabling production scheduling, perform an end-to-end test.

Expected flow:

```text
Funda
 ↓
Playwright
 ↓
Listing extraction
 ↓
SQLite
 ↓
New listing detection
 ↓
Confirmed filters
 ↓
Telegram
```

At minimum, verify:

### First run

New listings are inserted into SQLite.

Matching new listings produce Telegram notifications.

### Second run

The same listings are recognized as already known.

They must not generate duplicate notifications simply because the scraper encountered them again.

Conceptually:

```text
Run 1:
New listing → INSERT → NOTIFY

Run 2:
Existing listing → IGNORE → NO duplicate notification
```

This is one of the most important Phase 1 operational tests.

### Phase 2 — Scoring verification

Before enabling production scheduling with scoring enabled, perform these
additional verification steps:

1. **Dry-run review of scored output** — run with `--dry-run` and inspect
   the logs and database. Confirm:
   * breakdown math is correct (weighted average renormalized properly)
   * missing criteria are excluded from renormalization (not penalized)
   * `score_breakdown` is valid JSON in the database
   * scores fall in the expected 0–100 range

2. **Confidence flag on missing neighborhood data** — confirm the
   confidence flag is stored correctly on listings where
   `neighborhood_avg_price_m2` is `None` (confirmed absent on some real
   listings). The database should store `score_confidence = "partial"`
   (the score itself is no longer displayed in the Telegram
   notification).

3. **Live run** — a real run with notifications enabled, confirming the
   approved template renders correctly in the Telegram message (score
   must not appear).

Only after all three steps pass is the scoring feature considered
operationally verified.

---

## 15. Error Handling

A failure in one run must not result in aggressive retry behavior.

Errors should be classified where practical:

### Funda access failure

Examples:

* page does not load
* browser is blocked
* challenge page appears
* unexpected HTTP/navigation behavior

Action:

* log the failure
* stop or fail the run cleanly
* avoid rapid retries
* investigate before changing scraping behavior

### Parsing failure

Examples:

* selector no longer matches
* listing card structure changed
* expected field is missing

Action:

* log the affected behavior
* diagnose the root cause
* fix the scraper
* add a Learning Loop entry to `docs/site-notes/funda.md`

### Database failure

Examples:

* database unavailable
* SQLite write error
* schema mismatch

Action:

* log the error
* do not silently mark listings as processed
* investigate before resuming normal production runs

### Telegram failure

Examples:

* invalid credentials
* network failure
* Telegram API error

Action:

* log the failure without exposing credentials
* do not falsely report a notification as sent
* preserve enough information to retry safely later

### Image delivery failure

Property photos are best-effort and are delivered together with the
notification text as one media message (see Architecture.md →
"Property images in notifications"):

* a single image download failure is logged as a warning and that image
  is skipped; the remaining photos still ship;
* when all downloads fail, the notification degrades to a text-only
  message which stands — the listing IS marked notified; retrying would
  duplicate the delivered message;
* when the album upload fails, the notification falls back to a
  text-only message; this is logged as a warning; no image retry is
  attempted (no duplicate image messages);
* when the text is too long for a Telegram media caption, the full text
  is delivered first and the photos follow as an album captioned with
  the address — no information is dropped;
* downloaded images live only in a temporary directory
  (`funda-images-*` under the system temp dir) which is always removed
  after sending; images are never stored permanently;
* relevant warnings appear in `logs/scraper.log`
  ("Skipping unavailable image…", "Photo album upload failed…").

---

## 16. Funda Learning Loop

Whenever a Funda scraper problem is diagnosed and fixed, update:

```text
docs/site-notes/funda.md
```

The entry should contain:

* date
* symptom
* diagnosis
* fix
* optional pattern/warning

Example structure:

```markdown
### YYYY-MM-DD — <short description>

- **Symptom:** ...
- **Diagnosis:** ...
- **Fix:** ...
- **Pattern/Warning:** ...
```

This prevents future sessions and future agents from rediscovering the same Funda behavior from scratch.

---

## 17. Git and Shared Repository Operations

The repository is shared between two developers using different coding agents.

* Yousef uses Gemini CLI.
* Rashid uses OpenCode CLI.

Neither agent should assume that the working tree or remote branch belongs exclusively to it.

Before making changes:

```bash
git status
git branch --show-current
git remote -v
```

Before starting substantial work, make sure the local branch is understood and that recent remote changes have been considered.

Do not overwrite another developer's work.

### Push permission

`git push` requires explicit permission according to `AGENTS.md`.

A local commit may be created when appropriate, but pushing it to the remote repository requires confirmation.

### Documentation changes

Changes to:

```text
AGENTS.md
product.md
architecture.md
operations.md
```

should be committed together when they represent one coherent documentation update.

---

## 18. Deployment Principle

Phase 1 should avoid unnecessary deployment complexity.

The production scraper is a scheduled outbound process.

There is no requirement for:

* public HTTP server
* inbound port
* dashboard
* web API
* persistent application server

Therefore, do not add an inbound service merely to make the scraper run.

The basic production architecture remains:

```text
cron
 ↓
Python process
 ↓
Funda
 ↓
SQLite
 ↓
Telegram
```

---

## 19. Backups

The main persistent runtime state is:

```text
data/funda.db
```

SQLite does not provide automatic backup or replication in this architecture.

Backups are therefore an operational responsibility.

For Phase 1, backups are not part of the scraper's core functionality, but the database should be periodically copied if the stored history becomes important.

Do not introduce a backup service or external paid storage without an explicit requirement.

---

## 20. Operational Checklist

### Before first production run

* [ ] Git repository is correctly configured.
* [ ] Correct branch/workflow is being used.
* [ ] `.venv` is available.
* [ ] Required dependencies are installed.
* [ ] `.env` exists locally.
* [ ] `.env` is ignored by Git.
* [ ] Telegram credentials are configured.
* [ ] Funda access has been manually tested.
* [ ] Listing extraction has been tested.
* [ ] SQLite database creation has been tested.
* [ ] Deduplication has been tested.
* [ ] Filtering criteria have been confirmed.
* [ ] Telegram notification has been tested.
* [ ] End-to-end execution has succeeded.
* [ ] Logs are working.
* [ ] Cron configuration has been reviewed.

### After enabling cron

Check:

```text
logs/scraper.log
logs/cron.log
```

Verify:

* runs are occurring at the expected interval
* listings are being extracted
* new listings are being detected
* duplicate notifications are not occurring
* Telegram notifications are delivered
* no recurring Funda errors are appearing

---

## 21. Troubleshooting Order

When something fails, do not immediately modify multiple components.

Check in this order:

```text
1. Is the scheduled process running?
        ↓
2. Is Python/.venv correct?
        ↓
3. Can the browser reach Funda?
        ↓
4. Can listing cards be found?
        ↓
5. Are fields extracted correctly?
        ↓
6. Is SQLite working?
        ↓
7. Is deduplication working?
        ↓
8. Are filters correct?
        ↓
9. Is Telegram configured/reachable?
        ↓
10. Is the notification being sent?
```

This keeps failures isolated and makes debugging easier.

---

## 22. Current Operational Boundaries

The following are intentionally outside the current Phase 1 operations scope:

* public dashboard
* web API
* multi-site scraping
* multiple concurrent browsers
* paid proxy infrastructure
* paid CAPTCHA solving
* inbound network services
* complex orchestration platforms
* distributed databases
* high-availability infrastructure

These should only be introduced if a future product or architecture decision explicitly requires them.

---

## 23. Future Operational Decisions

The following remain open for future phases:

* log rotation strategy
* automated failure monitoring
* database backup automation
* more robust scheduling if cron becomes insufficient
* improved alerting for scraper failures
* operational metrics
* recovery behavior after extended Funda outages

These should be decided when the project reaches the relevant phase rather than prematurely adding infrastructure now.
