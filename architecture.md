# architecture.md — Amsterdam Funda Home-Search Agent

## Overview

A Python-based scraper that runs periodically, extracts current for-sale
Amsterdam listings from Funda using Playwright, detects listings not seen
before, stores them, and sends a Telegram notification for new listings at
or below €500,000 (see `product.md` for the product-level Phase 1 scope).

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.12, in the project `.venv` | Already provisioned; strong scraping ecosystem |
| Browser automation | Playwright (Chromium) | Funda is JS-rendered, and headless browser automation is the practical way to behave like a real browser and avoid trivial bot detection — not just a rendering requirement (see below) |
| Storage | SQLite | See "Data Storage" — recommended, file-based, no server process needed |
| Notifications | Telegram Bot API (direct HTTPS calls via `requests`, or `python-telegram-bot`) | Bot + token already exist |
| Scheduling | cron (Phase 1) | Simplest reliable way to run a periodic script without a persistent process; see "Scheduling" |
| Secrets | `.env` file + `python-dotenv`, git-ignored | Keeps the Telegram token out of version control |

## Scraping Strategy

### Why Playwright specifically
Funda renders listing data via JavaScript, but the more important reason
for Playwright over a lighter approach (`requests` + BeautifulSoup, or
reverse-engineering an internal API endpoint) is that Funda actively
defends against non-browser traffic. Playwright drives a real Chromium
instance, which is the most straightforward way to present as a genuine
browser and avoid the anti-scraping measures a simpler HTTP client would
trip immediately.

### Anti-bot considerations (no paid services — per `AGENTS.md`)
Since paid proxy/captcha services are off the table, mitigation relies on
*behaving like a real, infrequent human visitor* rather than on
infrastructure:
- **Realistic pacing:** avoid rapid-fire requests; add randomized delays
  between page loads within a single scrape run.
- **Low frequency:** infrequent scheduled runs (see Scheduling) rather than
  continuous polling — both reduces detection risk and respects Funda's
  servers.
- **Single browser instance at a time** — already required by `AGENTS.md`'s
  resource ceiling rule, and it also happens to look more human than
  parallel sessions.
- **Persistent browser context** (reusing cookies/session state between
  runs where possible) rather than presenting as a brand-new, cookie-less
  visitor every single run.
- If Funda blocks or challenges the scraper despite this, that is a
  Learning Loop event — log it per the `AGENTS.md` learning loop process,
  do not silently retry aggressively or reach for a paid workaround without
  flagging it to the developer first.

### Scrape flow (per run)
1. Launch a single headless Chromium instance via Playwright.
2. Navigate to the Funda Amsterdam "for sale" search results (paginated).
3. For each listing card, extract the data fields defined in `product.md`.
4. Compare each listing's unique ID (derived from its Funda URL) against
   the SQLite `listings` table.
5. For listings not already in the table: insert them, and if the listing
   matches all Phase 1 filter criteria (price €550,000–€750,000, ≥3
   bedrooms, ≥100 m² living area — see `product.md`), queue a Telegram
   notification.
6. Send queued notifications.
7. Close the browser. The process exits — nothing stays resident between
   runs.

## Data Storage

**Recommendation: SQLite**, via a single file (e.g. `data/funda.db`).
Reasoning: this is a solo, single-machine project — a full database server
(PostgreSQL) is unnecessary operational overhead, while plain JSON/CSV
files make "has this listing been seen before?" lookups and future
querying (price trends, etc. — Phase 4) awkward. SQLite gives real query
capability with zero server management, and is trivial to back up (it's
one file).

### Schema — `listings` table

| Column | Type | Notes |
|---|---|---|
| `listing_id` | TEXT, PRIMARY KEY | Derived from the Funda listing URL — the dedup key |
| `url` | TEXT | Full Funda listing URL |
| `address` | TEXT | |
| `neighborhood` | TEXT | |
| `price` | INTEGER | EUR, asking price |
| `living_area_m2` | INTEGER | |
| `plot_size_m2` | INTEGER | Nullable — not all listings have this |
| `rooms` | INTEGER | |
| `bedrooms` | INTEGER | Nullable |
| `property_type` | TEXT | |
| `year_built` | INTEGER | Nullable |
| `energy_label` | TEXT | Nullable |
| `status` | TEXT | Available / under offer / sold / etc. |
| `first_seen_at` | TEXT (ISO 8601 timestamp) | Set by the scraper on insert |
| `notified` | INTEGER (boolean, 0/1) | Whether a Telegram notification was sent for this listing |

Dedup logic is a simple `INSERT OR IGNORE` / existence check on
`listing_id` before treating a scraped listing as "new."

### Known limitations (acceptable now, revisit if the project grows)
- SQLite locks the whole file during a write — fine for one scraper process
  running every 30 minutes, but would become a bottleneck if a second
  process (e.g. a future dashboard) wrote to it concurrently.
- It's a local file with no network access — fine as long as everything
  runs on this one VPS; would not work if the project ever split across
  multiple machines.
- No built-in backup/replication — the developer is responsible for
  periodically copying `data/funda.db` if backups matter. Not urgent at
  Phase 1 data volumes.

## Scheduling & Execution

**Recommendation: cron, checking every 30 minutes**, as a starting point.
Reasoning:
- Frequent enough to give a real speed advantage on new listings (per the
  goal in `product.md`), without polling so aggressively that it looks
  automated or hammers Funda's servers.
- A headless browser process that launches, runs briefly, and exits is
  fine on 4GB RAM at this frequency — the resource concern in `AGENTS.md`
  is about *concurrent* instances, not periodic sequential ones.
- Easy to adjust later — this is a single cron schedule value, not a
  structural decision. If 30 minutes proves too aggressive (detection
  issues) or too slow (missed listings), it's a one-line change.

cron runs the scraper script directly (not inside the `agent-work` tmux
session — that session is for the AI coding agent, not production
execution). The `scraper` tmux session reserved in the environment setup
remains available for manual/interactive runs and debugging, but is not
the Phase 1 production execution mechanism.

Phase 3 (per `product.md`) may revisit this in favor of a more robust
scheduler if cron proves insufficient, but cron is the right starting
point for a solo project at this scale.

## Notifications — Telegram

- Bot and API token already created (via BotFather).
- Token stored in a `.env` file at the project root, loaded via
  `python-dotenv`. `.env` must be added to `.gitignore` — never committed.
- One message sent per newly-detected listing matching all Phase 1 filter
  criteria (price €550,000–€750,000, ≥3 bedrooms, ≥100 m² living area),
  containing at minimum: address, price, size, rooms, and the Funda listing
  link (per `product.md`).
- Notification sending happens after all new listings for the run are
  identified, not interleaved with scraping — keeps the scrape phase and
  the notify phase cleanly separated for easier debugging.

## Secrets Management

- `.env` file: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `.env` is git-ignored; a `.env.example` (with blank/placeholder values)
  should be committed instead, so the structure is documented without
  leaking secrets.

## Project Structure (proposed)

```
project-root/
├── AGENTS.md
├── product.md
├── architecture.md
├── operations.md              (Phase: written next)
├── .env                       (git-ignored — real secrets)
├── .env.example                (committed — placeholder structure)
├── .gitignore
├── requirements.txt
├── data/
│   └── funda.db                (SQLite — git-ignored, it's runtime data)
├── logs/
│   ├── scraper.log              (application-level logging — git-ignored)
│   └── cron.log                 (raw stdout/stderr from cron runs — git-ignored)
├── docs/
│   └── site-notes/
│       └── funda.md            (Learning Loop, per AGENTS.md)
├── src/
│   ├── scraper.py               (Playwright scrape logic)
│   ├── storage.py               (SQLite read/write, dedup)
│   ├── notifier.py              (Telegram sending)
│   └── main.py                  (entry point — orchestrates a full run)
└── tests/
```

## Logging & Error Handling

- Each run logs: number of listings scraped, number new, number notified,
  and any errors encountered (e.g. a page structure that didn't parse as
  expected).
- A parsing failure on a listing (missing expected field, broken selector)
  must trigger a Learning Loop entry in `docs/site-notes/funda.md` per
  `AGENTS.md` — this is where scraper breakage gets diagnosed and
  remembered.
- A full run failure (e.g. Funda blocked the browser entirely) should log
  clearly enough that checking cron logs makes the cause obvious — exact
  logging destination/format is an `operations.md` decision.

## Open Decisions (deferred, not forgotten)

- Exact Playwright stealth techniques (e.g. whether a stealth plugin is
  needed, or default Playwright is sufficient) — to be determined once
  real-world testing against Funda begins; flag in the Learning Loop if
  detection issues arise.
- Log file location/rotation, monitoring for failed cron runs — deferred to
  `operations.md`.
- Config file format for Phase 2 filtering criteria — deferred until
  Phase 2 begins.
