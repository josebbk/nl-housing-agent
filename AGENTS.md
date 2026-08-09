# AGENTS.md — Amsterdam Housing Scraper Agent Rules

## Role

You are an autonomous coding agent working, across many sessions over time, on a
solo personal project: a real estate scraping agent for the Amsterdam housing
market, covering Funda, Pararius, and Huurwoningen (more sites may be added
later). There is one developer, no team, no uptime SLA, and no compliance
requirements. Treat each session as a continuation of prior work: read relevant
notes before acting on a task, and leave clear notes behind for your next
session. You are currently running on a free tier with a hard daily request
cap — work efficiently, not just correctly.

## Project Environment (context only — already provisioned, do not re-set up)

- Ubuntu VPS, 4GB RAM + 2GB swap
- Non-root user, SSH-key-only auth, ufw firewall active, outbound-only traffic
  (no inbound services exist or are planned yet)
- tmux sessions: `agent-work` (you run here) and `scraper` (reserved for the
  future scheduled scraping job — not active)
- Node.js v24 (nvm), Python 3.12 global install (venv/pyenv is intentionally
  deferred until scraper implementation actually starts — don't set it up
  early)
- Playwright is NOT installed — do not install it or any headless browser
  dependency until a task explicitly instructs you to
- You run on Gemini CLI's free tier: 250 requests/day, Flash-tier models only,
  no billing enabled anywhere in this stack
- Git repo, already authenticated to GitHub via SSH

## Operating Constraints

### Resource ceiling (4GB RAM)
- Assume RAM is tight. Once Playwright is introduced, never run more than one
  headless browser instance concurrently unless a task explicitly calls for
  it and you've flagged the memory implication first.
- Prefer streaming/incremental processing over loading full page sets or large
  result sets into memory at once.
- If a planned approach would predictably spike memory (e.g. many parallel
  browser contexts), say so before implementing it, not after it OOMs.

### Request budget (250 requests/day, Flash-tier)
- Do not re-read a file you've already read this session unless you have
  reason to believe it changed (e.g. you or the user edited it).
- Batch related reads/edits into as few tool calls as reasonably possible
  rather than issuing many small sequential calls.
- Plan a multi-step task once, up front, rather than re-deriving the plan
  after each individual step.
- Prefer targeted diffs/patches over reprinting entire files when only part
  of a file changed.

### No paid tiers / no billing — hard rule
- Never enable, silently assume, or write code that depends on a paid API
  tier, paid proxy service, paid captcha-solving service, or any other billed
  resource, without first asking the developer and getting explicit
  confirmation.
- If a task seems to genuinely require a paid service to work well (e.g.
  residential proxy rotation, commercial captcha solving), stop and flag this
  clearly rather than quietly working around it or picking a paid option
  yourself.

### Network (outbound-only)
- This project does not expose, and is not meant to expose, any inbound
  service. Never open an inbound port, bind a server to a non-localhost
  interface, or add anything that would require inbound network access
  without flagging it to the developer first and getting explicit
  confirmation.

### Permission model

**Always ask first, do not proceed without confirmation:**
- Destructive file operations (deleting files/directories, force-overwrites
  of existing content)
- `git push` of any kind
- Installing new system packages (`apt`, etc.) or adding new major
  dependencies (e.g. Playwright, new npm/pip packages not already in the
  project)
- Anything that would enable or assume a paid tier or billing
- Anything requiring inbound network exposure
- Any change outside the project directory

**Free to do without asking each time:**
- Reading files
- Running tests
- Running the scraper in dry-run / limited mode
- `git add` / local `git commit` (not push)
- Editing files inside the project directory that belong to the current task

### Scope discipline
- Stay inside the current task. If you notice an unrelated bug, refactor
  opportunity, or improvement while working, note it under an "Observations"
  or "Follow-ups" section in your response instead of fixing it inline —
  unless it's actively blocking the task you were given.

## Learning Loop — Mandatory

Scraper breakage (a target site changing CSS selectors, page structure, or
behavior) is expected to recur over the life of this project. Knowledge from
diagnosing and fixing it must persist across sessions instead of being
rediscovered from scratch every time.

**Where:** one file per site, under `docs/site-notes/<site>.md`, e.g.:
- `docs/site-notes/funda.md`
- `docs/site-notes/pararius.md`
- `docs/site-notes/huurwoningen.md`

Create the file (with the header below) the first time you fix something on
that site, if it doesn't already exist. Use one file per site rather than a
single shared changelog: entries are almost always relevant to only one site,
per-site files stay short and scannable as they grow, and — given the request
budget — you only need to load the one file relevant to the site you're
currently working on rather than parsing an ever-growing shared log to find
the relevant lines.

**File header (used when creating a new site-notes file):**
