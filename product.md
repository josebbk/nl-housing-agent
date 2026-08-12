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

### Resolution of Price Range Requirement

The price range has been explicitly confirmed by the project owner as **€550,000–€750,000** for Phase 1. This range serves as the active single source of truth across all project documentation.

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

A future version may include additional useful information such as:

* bedrooms
* neighborhood
* property type
* energy label
* construction year
* plot size

These additional fields are not required for the initial notification unless
the implementation naturally supports them.

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

---

## 10. Scheduling

The initial intended schedule is periodic checking approximately every
30 minutes.

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

Potential future work:

* configurable filters
* more detailed property preferences
* better notification formatting
* additional ranking/scoring

Phase 2 details are not finalized.

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
* **Notes:** Serves as the single source of truth for Phase 1 filtering logic. May be made configurable or adjusted in Phase 2.

---

## 15. Product Source of Truth

When implementation behavior conflicts with this document, the discrepancy
must be investigated rather than silently resolved.

Changes to product scope or requirements must be reflected in this file.

Technical implementation decisions belong in `architecture.md`.

Operational procedures belong in `operations.md`.
