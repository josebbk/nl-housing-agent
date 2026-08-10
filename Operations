# operations.md — Amsterdam Funda Home-Search Agent

## Overview

This is the operational runbook: how to run the scraper manually, how it's
scheduled, where to find logs, and what to do when something goes wrong.
For *what* the system does, see `product.md`. For *how it's built*, see
`architecture.md`. This file is for keeping it *running*.

## Running the scraper manually

Always test a manual run before trusting a cron schedule — this is also
your primary debugging tool when something looks wrong.

```bash
cd ~/projects/nl-housing-agent        # project root, wherever it lives on the server
source .venv/bin/activate  # if not already active in your shell
python src/main.py
```

Watch the terminal output directly — this is the fastest way to see errors
as they happen, before worrying about log files.

## Logging

Two separate logs exist, capturing different things:

| File | What it captures | Written by |
|---|---|---|
| `logs/scraper.log` | Application-level events: run start/end, listings scraped, new listings found, notifications sent, parsing errors on individual listings | Python's `logging` module, inside the scraper code itself |
| `logs/cron.log` | Raw stdout/stderr of the entire process, including crashes the application-level logger never got to log (e.g. Python crashing before logging initializes, or the process getting killed) | cron, via output redirection |

Both are git-ignored (runtime data, not source).

**To check recent activity:**
```bash
tail -n 50 logs/scraper.log
```

**To watch a run live** (e.g. while testing):
```bash
tail -f logs/scraper.log
```

**If a scheduled run seems to have not happened at all** (no new log
entries after 30+ minutes), check `cron.log` first — that's where you'd see
a crash that happened before the application logger even started:
```bash
tail -n 50 logs/cron.log
```

**Log rotation:** not configured yet at Phase 1 — logs will grow
unbounded. This is fine at current scale (small text files, checked
periodically), but revisit if `logs/scraper.log` starts growing
noticeably large (e.g. add Python's `RotatingFileHandler`, or a simple
`logrotate` config).

## Scheduling — cron setup (step by step)

The scraper runs every 30 minutes via cron, calling the Python interpreter
inside the project's `.venv` directly (not by activating the venv inside
the cron job — cron's environment is minimal and activation scripts can
behave unreliably there; calling the venv's Python binary directly sidesteps
this entirely).

**1. Find your project's absolute path** (cron needs absolute paths, not
relative ones):
```bash
cd ~/projects/nl-housing-agent
pwd
```
Copy this output — you'll need it below. Example used in these
instructions: `~/projects/nl-housing-agent`.

**2. Create the logs directory if it doesn't exist yet:**
```bash
mkdir -p logs
```

**3. Test the exact command cron will run**, before scheduling it — this
catches path/environment issues immediately instead of debugging a silent
cron failure later:
```bash
~/projects/nl-housing-agent/.venv/bin/python ~/projects/nl-housing-agent/src/main.py
```
(Replace the path with your actual project path from step 1.) This should
run and complete the same way your manual `python src/main.py` run did. If
it doesn't, fix that before touching cron.

**4. Open your crontab for editing:**
```bash
crontab -e
```
(First time running this, it may ask you to choose an editor — `nano` is
the simplest choice if unsure.)

**5. Add this line at the end of the file**, replacing the path with your
actual project path:
```
*/30 * * * * cd ~/projects/nl-housing-agent && .venv/bin/python src/main.py >> logs/cron.log 2>&1
```
Breaking this down:
- `*/30 * * * *` — run every 30 minutes
- `cd ~/projects/nl-housing-agent &&` — move into the project directory first, so relative paths inside the script (e.g. `data/funda.db`) resolve correctly
- `.venv/bin/python src/main.py` — run the script using the venv's Python directly
- `>> logs/cron.log 2>&1` — append both normal output and errors to `cron.log`, so nothing gets silently lost

**6. Save and exit.** Verify it registered:
```bash
crontab -l
```
You should see the line you just added.

**7. Wait for the next 30-minute mark, then check both logs** to confirm
it actually ran:
```bash
tail -n 20 logs/cron.log
tail -n 20 logs/scraper.log
```

**To pause the scraper temporarily** (e.g. while debugging), comment out
the line in `crontab -e` by putting a `#` at the start, rather than
deleting it — easier to re-enable later.

## Monitoring — what "healthy" looks like

Check periodically (no automated alerting on failures yet — that's a
possible Phase 4 addition, not in scope now):
- `logs/scraper.log` shows a new run roughly every 30 minutes, with a
  clear start/end for each
- Telegram is receiving messages when genuinely matching new listings
  exist (absence of messages isn't itself a problem — it just means no
  new matches, per the filter criteria in `product.md`)
- No repeated errors for the same cause across multiple runs

## Troubleshooting

### Scraper runs but finds 0 listings every time (even though Funda clearly has some)
Likely cause: Funda changed page structure and the scraper's selectors no
longer match anything. **This is a Learning Loop event** — diagnose it,
fix it, and record the fix in `docs/site-notes/funda.md` per `AGENTS.md`.
Do not just patch the selector silently; the whole point of the Learning
Loop is that this gets documented.

### Scraper fails/crashes entirely, or gets blocked (e.g. CAPTCHA, access denied)
Check `logs/cron.log` and `logs/scraper.log` for the actual error. If it
looks like anti-bot detection (unexpected redirect, CAPTCHA page, blank
page where content should be), this is also a Learning Loop event —
Funda's anti-scraping behavior changing counts as "site breakage" just as
much as a selector change does. Do not respond by increasing scrape
frequency or adding a paid proxy/captcha-solving service without first
flagging it to the developer, per the no-paid-services rule in
`AGENTS.md`.

### No Telegram notifications, but scraper log shows matching listings found
Check the Telegram bot token and chat ID in `.env` are still correct and
the bot hasn't been blocked/removed. Test the bot independently of the
scraper with a minimal manual message send to isolate whether the problem
is the bot/token or the scraper's notification logic.

### `database is locked` errors
Should not happen at Phase 1 (single process, sequential runs 30 minutes
apart), but if a manual run overlaps with a cron run, this can occur.
Avoid running the scraper manually at the same time a scheduled run might
be executing; if this becomes a recurring problem, it's a sign Phase 1's
concurrency assumptions need revisiting in `architecture.md`.

### Cron job doesn't seem to run at all
- Confirm with `crontab -l` that the line is actually present and
  uncommented
- Confirm the paths in the cron line are absolute and correct
- Check `logs/cron.log` — if it's completely empty (not even error output),
  the cron job likely isn't triggering at all; check the system's cron
  service is running (`systemctl status cron`)

## Backups

`data/funda.db` is the only stateful data in this system. Not automated at
Phase 1 — periodically copy it somewhere safe if you'd be upset to lose
scrape history:
```bash
cp data/funda.db data/funda.db.backup-$(date +%Y%m%d)
```

## Updating dependencies

```bash
source .venv/bin/activate
pip install -r requirements.txt --upgrade
playwright install chromium   # keep the browser binary in sync with the playwright package version
```
Test with a manual run afterward before trusting the next cron cycle.
