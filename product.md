# product.md — Amsterdam Funda Home-Search Agent

## Overview

A personal tool that scrapes Funda.nl for-sale listings in Amsterdam,
detects new listings as they're published, and sends the developer a
Telegram notification so they can act on new homes for sale quickly —
without manually refreshing Funda search pages.

## Who this is for

Single user (the developer), searching for a home to buy in Amsterdam.
Not a product for other users, not a commercial service. No multi-user
support, accounts, or billing are in scope.

## Goal

Be notified of new Funda "for sale" listings in Amsterdam as close to
publish-time as practical, so the developer has a speed advantage over
manually checking the site — a meaningful edge in a competitive housing
market where good listings can attract offers within days.

## Scope

**In scope:**
- Funda.nl only (see `AGENTS.md` — single-site by design, no multi-site
  abstractions)
- **For-sale listings only** (koop) — not rentals (huur)
- **Amsterdam, city-wide** — no neighborhood restriction for now
- Detecting *new* listings (not previously seen by the scraper)
- Sending a Telegram notification per new listing found
- Storing scraped listing data in a structured, queryable form (exact
  storage mechanism is an `architecture.md` decision, not a product
  decision)

**Out of scope (for now — may revisit later):**
- Rental listings
- Any site other than Funda
- Neighborhood-specific filtering
- Price/size/room filtering (deferred to Phase 2 — see Roadmap)
- Multi-user support, web dashboard, or public-facing product
- Any paid infrastructure (proxies, captcha solving, paid APIs) per the
  hard rule in `AGENTS.md`

## Data to capture per listing

Minimum fields the scraper must extract for each Funda listing:

| Field | Notes |
|---|---|
| Listing ID / URL | Unique identifier — used for dedup (has this listing been seen before?) |
| Address | Street + house number |
| Neighborhood/area | As listed on Funda (not filtered on yet, but useful later) |
| Asking price | Numeric, EUR |
| Living area (m²) | |
| Plot size (m²) | If applicable (not all listings, e.g. apartments) |
| Number of rooms | Total rooms |
| Number of bedrooms | If Funda distinguishes this from total rooms |
| Property type | Apartment (appartement), house (woonhuis), etc. |
| Year built | If available |
| Energy label | If available |
| Listing status | Available, under offer ("onder bod"), sold, etc. |
| First-seen date/time | Set by the scraper, not Funda — this is when *our system* first recorded it, used to determine "new" |
| Listing published date | If Funda exposes this directly (may differ from first-seen if the scraper isn't running continuously) |

Exact scraping method (HTML parsing vs. API-like endpoints Funda may use
internally, whether Playwright is needed) is decided in `architecture.md`,
not here.

## Notifications

- **Channel:** Telegram bot, messaging the developer directly
- **Trigger (MVP):** A listing detected as new (not previously seen)
  triggers a notification only if it matches all Phase 1 filter criteria:
  price €550,000–€750,000, ≥3 bedrooms, ≥100 m² living area (see Roadmap).
  Listings that don't match are still stored, just not notified.
- **Message content (minimum):** Address, price, size, rooms, link to the
  Funda listing
- Bot setup details (token management, chat ID, message formatting) belong
  in `architecture.md` and/or `operations.md`, not here

## Roadmap

### Phase 1 — MVP (current target)
- Scrape Funda for-sale Amsterdam listings
- Store listings in structured storage with the fields above
- Detect new listings via dedup against previously stored listings
- Apply hardcoded Phase 1 filter criteria — all listings stored regardless,
  but notifications only fire for listings matching **all** of:
  - Price between €550,000 and €750,000 (inclusive)
  - Minimum 3 bedrooms
  - Minimum 100 m² living area
- Send a Telegram notification for every new listing matching all three
  criteria above
- Runs on manual/scheduled trigger (scheduling mechanism decided in
  `architecture.md`)

**Definition of done for Phase 1:** Running the scraper against live Funda
Amsterdam search results reliably produces a Telegram message for each
genuinely new listing priced €550,000–€750,000 with at least 3 bedrooms and
at least 100 m² living area, with no duplicate alerts for previously-seen
listings, and no missed matching listings on a normal run.

### Phase 2 — Filtering
- Replace the hardcoded Phase 1 criteria with configurable search criteria
  (the same fields — price range, min bedrooms, min size — but adjustable
  without code changes, e.g. via a config file)
- Neighborhood and other criteria not yet covered by Phase 1 (property
  type, energy label, etc.) become available as filter options
- Exact config mechanism decided in `architecture.md`

### Phase 3 — Reliable automation
- Move from manual/ad-hoc runs to real scheduled execution (e.g. cron, or
  a persistent process in the `scraper` tmux session)
- Add basic monitoring: know if a scheduled run failed or Funda's page
  structure broke the scraper (ties into the Learning Loop in `AGENTS.md`)

### Phase 4 — Nice-to-haves (not committed, revisit after Phase 1–3 work)
- Neighborhood-specific filtering
- Price-change tracking on already-seen listings (e.g. price drops)
- Simple history view/dashboard of everything scraped so far
- Listing status tracking (e.g. flag when a previously-seen listing goes
  "under offer" or is delisted)

## Open decisions (intentionally deferred, not forgotten)

- Concrete filter criteria (budget, size, rooms) — will be defined once
  Phase 1 is working and real data is available to calibrate against
- Storage technology, dedup mechanism, scraping method, scheduling
  mechanism — all `architecture.md` decisions, out of scope for this file
