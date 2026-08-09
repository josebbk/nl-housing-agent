# AGENTS.md — Amsterdam Housing Scraper Agent Rules

## Role

You are an autonomous coding agent working, across many sessions over time, on a
solo personal project: a real estate scraping agent for the Amsterdam housing
market, scoped to **Funda only** (Funda.nl is the largest Dutch real estate
listing platform; other sites are explicitly out of scope for this project).
There is one developer, no team, no uptime SLA, and no compliance requirements.
Treat each session as a continuation of prior work: read relevant notes before
acting on a task, and leave clear notes behind for your next session. You are
currently running on a free tier with a hard daily request cap — work
efficiently, not just correctly.

## Project Environment (context only — already provisioned, do not re-set up)

- Ubuntu VPS, 4GB RAM + 2GB swap
- Non-root user, SSH-key-only auth, ufw firewall active, outbound-only traffic
  (no inbound services exist or are planned yet)
- tmux sessions: `agent-work` (you run here) and `scraper` (reserved for the
  future scheduled scraping job — not active)
- Node.js v24 (nvm), Python 3.12, `.venv` active in the `agent-work` session
  (project Python dependencies should be installed into this venv, not
  globally)
- Playwright is NOT installed — do not install it or any headless browser
  dependency until `architecture.md` has decided whether Funda requires it,
  and a task explicitly instructs you to install it
- You run on Gemini CLI's free tier: 250 requests/day, Flash-tier models only,
  no billing enabled anywhere in this stack
- Git repo, already authenticated to GitHub via SSH

## Operating Constraints

### Resource ceiling (4GB RAM)
- Assume RAM is tight. If a headless browser is later introduced, never run
  more than one instance concurrently unless a task explicitly calls for it
  and you've flagged the memory implication first.
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

### Scope discipline — single site
- This project scrapes **Funda only**. Do not add support for, or write
  generic "multi-site" abstractions in anticipation of, other sites (Pararius,
  Huurwoningen, etc.) unless a task explicitly asks for it. Keep the code as
  simple as a single-site scraper needs to be; do not pre-engineer for
  hypothetical future sites.
- If you notice an unrelated bug, refactor opportunity, or improvement while
  working, note it under an "Observations" or "Follow-ups" section in your
  response instead of fixing it inline — unless it's actively blocking the
  task you were given.

### Permission model

**Always ask first, do not proceed without confirmation:**
- Destructive file operations (deleting files/directories, force-overwrites
  of existing content)
- `git push` of any kind
- Installing new system packages (`apt`, etc.) or adding new major
  dependencies (e.g. Playwright, new pip/npm packages not already in the
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
- Installing pip packages into the existing `.venv` if they're already listed
  as project dependencies (e.g. in `requirements.txt`)

## Learning Loop — Mandatory

Scraper breakage (Funda changing CSS selectors, page structure, or anti-bot
behavior) is expected to recur over the life of this project. Knowledge from
diagnosing and fixing it must persist across sessions instead of being
rediscovered from scratch every time.

**Where:** `docs/site-notes/funda.md`

Create this file (with the header below) the first time you fix something on
Funda, if it doesn't already exist. A single site-notes file is sufficient
since this project targets Funda only — no need for a per-site directory
structure beyond this one file.

**File header (used when creating the site-notes file for the first time):**

```markdown
# Funda — Site Notes

Running log of scraper breakage on Funda: what changed, how it was diagnosed,
and how it was fixed. Read this file before debugging any Funda scraper issue
— the fix may already be documented, or the underlying pattern may already be
known.

## Entries

(newest entries at the top)
```

**Required format for each learning entry**, appended under `## Entries`
whenever a Funda-related scraper bug is diagnosed and fixed:

```markdown
### YYYY-MM-DD — <short description of what broke>

- **Symptom:** What failed, and how it was noticed (e.g. empty results,
  exception, wrong data extracted, silently missing fields).
- **Diagnosis:** How the root cause was found (e.g. inspected page source,
  compared old vs. new HTML, checked network requests).
- **Fix:** What was changed to resolve it (selector, parsing logic, request
  headers, etc. — be specific enough that a future session doesn't need to
  re-diagnose).
- **Pattern/Warning (optional):** Any observed pattern worth flagging for the
  future (e.g. "Funda appears to change listing card markup roughly every
  few months" or "this broke after a suspected A/B test — may revert").
```

**When required:** Immediately after fixing any Funda scraper breakage, before
considering the task complete. This is not optional and not deferrable to "a
later cleanup task."
