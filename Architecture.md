# architecture.md — Amsterdam Funda Home-Search Agent

## Overview

A Python-based scraper that runs periodically, extracts current for-sale
Amsterdam listings from Funda using Playwright, detects listings not seen
before, stores them in SQLite, applies the confirmed Phase 1 property filters
(€550,000–€750,000 asking price, ≥3 bedrooms, ≥100 m² living area), and sends
Telegram notifications for newly detected matching listings.

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

---

## Phase 1 Filtering Criteria

The confirmed Phase 1 filtering criteria serve as the single source of truth across all project documentation:

* **Price:** €550,000–€750,000 (Confirmed)
* **Bedrooms:** ≥3
* **Living area:** ≥100 m²

### Resolution of Legacy Requirements

The price range has been explicitly confirmed as **€550,000–€750,000** by the project owner. The scraper implementation must strictly enforce this range.

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
`bedrooms` are **required** — listings missing any of these are skipped and
logged at INFO level, not inserted into the database.

This supersedes the earlier decision in this document that marked `bedrooms` as
nullable. These six fields are the basis of Phase 1 filtering; allowing them to
be NULL would let incomplete listings into the database where they could not
match filters but would still occupy space and complicate future analysis.

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
      ├── yes → not a new listing
      │
      └── no → insert
                  ↓
            evaluate filters
                  ↓
             if matching
                  ↓
          queue notification
```

The database should not generate duplicate new-listing notifications merely
because a listing appears in multiple scheduled runs.

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

The initial scheduling recommendation is approximately every 30 minutes.

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

### 4. Phase 2 filter configuration

A configurable filter system is deferred until Phase 2.

Do not build a complex configuration framework prematurely.

### 5. Scheduling evolution

cron is the Phase 1 starting point.

A more robust scheduler can be considered in a later phase if operational
experience demonstrates a need.

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
