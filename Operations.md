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

Expected variables:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

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
```

Never print the Telegram bot token in logs or terminal output.

If a secret is accidentally committed, stop and treat it as a security incident rather than simply deleting it from the latest commit.

---

## 5. Database and Runtime Data

The application uses SQLite.

Expected database location:

```text
data/funda.db
```

The database is runtime data and should not be committed to Git.

The database stores listings and allows the scraper to distinguish previously seen listings from newly discovered listings.

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

Phase 1 target frequency is approximately every 30 minutes.

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

The intended Phase 1 schedule is approximately:

```text
Every 30 minutes
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
