# AGENTS.md — Amsterdam Housing Scraper Agent Rules

## Role

You are an autonomous coding agent working across many sessions on a shared
personal project: a real estate scraping agent for the Amsterdam housing
market, scoped to **Funda only** (Funda.nl is the largest Dutch real estate
listing platform; other sites are explicitly out of scope for this project).

The project has **two developers/agents working on the same Git repository**.
One developer may work through Gemini CLI and another may work through
OpenCode CLI. Both agents must follow the same project documentation,
architecture, Git workflow, and technical constraints.

Treat each session as a continuation of prior work: read relevant notes before
acting on a task, and leave clear notes behind for the next session.

The project is currently running on a free-tier AI CLI environment with a
hard daily request cap. Work efficiently, not just correctly.

---

## Shared Repository / Multi-Agent Workflow

This project is developed by two developers working on the same repository.

* Developer 1 may use Gemini CLI.
* Developer 2 may use OpenCode CLI.
* Both agents operate on the same Git repository.
* Project documentation in this repository is the shared source of truth.
* Do not assume that another agent's work is absent simply because it was not
  created in the current CLI session.
* Before modifying architecture, product scope, or shared configuration,
  inspect the current repository state and relevant documentation.
* Do not overwrite another developer's work without understanding the change.
* Prefer additive changes over destructive rewrites.
* When changing an existing rule or decision, explain why the change is
  necessary and preserve the previous decision in documentation when useful
  for historical context.
* Keep commits focused and understandable so the other developer can review
  and continue the work.

### Git collaboration rules

Before beginning a task that changes files:

1. Check the current Git status.
2. Check the current branch.
3. Inspect recent commits when the task depends on previous work.
4. Read relevant project documentation before making architectural changes.

Do not assume that the working tree is clean.

Never discard, reset, force-overwrite, or revert another developer's changes
without explicit confirmation.

`git add` and local `git commit` are allowed.

**Always ask first before:**

* `git push` of any kind
* destructive Git operations
* resetting or discarding another developer's uncommitted work

---

## Project Environment

The project environment is already provisioned. Do not unnecessarily
reconfigure the server.

* Ubuntu VPS, 4GB RAM + 2GB swap
* Non-root user
* SSH-key-only authentication
* UFW firewall active
* Outbound-only traffic
* No inbound application services are planned
* Separate Linux users are used by the developers so their environments do
  not interfere with each other
* tmux is used for persistent terminal sessions
* Node.js v24 (nvm)
* Python 3.12
* `.venv` is used for Python dependencies
* Git repository is already authenticated to GitHub via SSH

### tmux sessions

The intended tmux sessions are:

* `agent-work` — used for the AI coding agent and development work
* `scraper` — reserved for future scraper execution, manual testing, and
  debugging

Do not run the production scraper continuously inside `agent-work`.

The `scraper` tmux session is not a replacement for the planned production
scheduler.

---

## AI CLI Environment

Different developers may use different coding agents.

Current examples:

* Gemini CLI
* OpenCode CLI

The CLI itself is **not the source of truth**.

The repository documentation and source code are the source of truth.

Do not create agent-specific project behavior that conflicts with
`AGENTS.md`, `product.md`, or `architecture.md`.

---

## Resource Ceiling — 4GB RAM

Assume RAM is tight.

If a headless browser is introduced:

* Never run more than one browser instance concurrently unless a task
  explicitly calls for it and the memory implication has been flagged first.
* Prefer streaming/incremental processing over loading full page sets or large
  result sets into memory at once.
* If a planned approach would predictably spike memory, say so before
  implementing it.
* Do not create unnecessary parallel browser contexts.

---

## Request Budget

AI CLI usage is limited and should be treated as a finite resource.

* Do not re-read a file already read during the current session unless there
  is reason to believe it changed.
* Batch related reads and edits when reasonably possible.
* Plan multi-step tasks before executing them.
* Prefer targeted diffs/patches over unnecessarily rewriting entire files.
* Do not repeatedly ask the AI agent to rediscover project context that is
  already documented.

---

## No Paid Tiers / No Billing — Hard Rule

Never enable, silently assume, or write code that depends on:

* paid API tiers
* paid proxy services
* paid CAPTCHA-solving services
* commercial scraping infrastructure
* other billed resources

without first asking the developer and receiving explicit confirmation.

If a task genuinely appears to require a paid service, stop and clearly flag
the requirement instead of silently selecting a paid workaround.

---

## Network — Outbound Only

This project does not expose, and is not intended to expose, an inbound
service.

Never:

* open an inbound port
* bind a server to a non-localhost interface
* add an inbound service
* introduce infrastructure requiring inbound network access

without first flagging it to the developer and receiving explicit
confirmation.

---

## Scope Discipline — Funda Only

This project scrapes **Funda only**.

Do not add support for:

* Pararius
* Huurwoningen
* other real-estate sites

unless explicitly requested.

Do not create generic multi-site abstractions in anticipation of hypothetical
future sites.

Keep the implementation as simple as a single-site scraper requires.

If an unrelated bug or refactoring opportunity is noticed, record it under
`Observations` or `Follow-ups` rather than fixing it inline, unless it is
actively blocking the assigned task.

---

## Permission Model

### Always ask first

Do not proceed without confirmation for:

* destructive file operations
* deleting files/directories
* force-overwriting existing content
* `git push`
* destructive Git operations
* installing new system packages (`apt`, etc.)
* adding new major dependencies
* installing Playwright or browser dependencies before the architecture/task
  explicitly allows it
* anything that enables or assumes paid billing
* anything requiring inbound network exposure
* changes outside the project directory

### Free to do without asking each time

* Reading files
* Running tests
* Running the scraper in dry-run / limited mode
* Checking Git status/history
* `git add`
* Local `git commit`
* Editing files inside the project directory belonging to the current task
* Installing pip packages into the existing `.venv` if they are already listed
  as project dependencies

---

## Documentation Is Part of the Implementation

The following documents are part of the project's source of truth:

* `AGENTS.md` — agent behavior and project rules
* `product.md` — product scope and requirements
* `architecture.md` — technical architecture and implementation decisions
* `operations.md` — operational procedures
* `docs/site-notes/funda.md` — Funda-specific scraper knowledge

Documentation must remain synchronized with the implementation.

### Product changes

If a task changes:

* product scope
* filtering criteria
* notification behavior
* roadmap
* user-visible functionality

update `product.md` in the same task.

### Architecture changes

If a task changes:

* scraper technology
* browser automation
* database
* data model
* scheduling
* dependencies
* notification architecture
* execution model

update `architecture.md` in the same task.

### Operational changes

If a task changes:

* deployment
* cron
* environment setup
* logs
* monitoring
* recovery procedures

update `operations.md`.

---

## Requirement Contradictions

If project documentation contains conflicting requirements, do not silently
choose one based on personal assumptions.

Examples include:

* different price ranges
* different bedroom requirements
* different area requirements
* conflicting scheduling intervals
* conflicting notification behavior

When a contradiction is discovered:

1. Identify the exact conflict.
2. Preserve the information needed to understand the previous decision.
3. Record the contradiction in the relevant documentation.
4. Ask the developer/owner for clarification when implementation depends on it.
5. Do not implement a disputed requirement as though it were final.

The product requirement must ultimately be confirmed before it is treated as
final behavior.

---

## Documentation Change Philosophy

Prefer **additive documentation changes** over deleting historical context.

When changing an existing document:

* preserve useful existing information
* add clarification where possible
* do not remove a rule merely because it can be phrased more cleanly
* if a rule becomes obsolete, explicitly document that it was superseded and
  why
* do not silently erase decisions that another developer may still be relying
  on

This is especially important because Gemini CLI and OpenCode CLI may work on
the repository in different sessions.

---

## Learning Loop — Mandatory

Scraper breakage caused by Funda changing CSS selectors, page structure, or
anti-bot behavior is expected to recur throughout the life of the project.

Knowledge from diagnosing and fixing it must persist across sessions instead
of being rediscovered.

### Where

`docs/site-notes/funda.md`

Create this file with the following header the first time a Funda-related
scraper issue is diagnosed and fixed, if it does not already exist:

```markdown
# Funda — Site Notes

Running log of scraper breakage on Funda: what changed, how it was diagnosed,
and how it was fixed. Read this file before debugging any Funda scraper issue
— the fix may already be documented, or the underlying pattern may already
be known.

## Entries

(newest entries at the top)
```

### Required format

Each entry must follow:

```markdown
### YYYY-MM-DD — <short description of what broke>

- **Symptom:** What failed, and how it was noticed.
- **Diagnosis:** How the root cause was found.
- **Fix:** What was changed to resolve it.
- **Pattern/Warning (optional):** Any observed pattern worth remembering.
```

### When required

Immediately after fixing a Funda scraper breakage, before considering the task
complete.

This is mandatory and must not be deferred to a cleanup task.

---

## Scraping Safety and Anti-Bot Behavior

The project does not use paid proxy or CAPTCHA services.

The scraper should behave like an infrequent legitimate visitor:

* realistic pacing
* randomized delays where appropriate
* low-frequency scheduled runs
* one browser instance at a time
* persistent browser context where technically appropriate
* no aggressive retries
* no high-volume parallel scraping

If Funda blocks or challenges the scraper:

1. Do not aggressively retry.
2. Diagnose the behavior.
3. Record the issue in `docs/site-notes/funda.md`.
4. Do not introduce paid workarounds without explicit approval.

---

## Current Browser Dependency Rule

Playwright is **not assumed to be installed** merely because it appears in
the architecture proposal.

Before installing Playwright or any headless-browser dependency:

1. Confirm the architecture decision.
2. Confirm that the current task explicitly requires installation.
3. Confirm the dependency is permitted under the no-paid-service constraint.
4. Consider the 4GB RAM limit.

Do not install browser dependencies simply as preparation for a future task.

---

## Project Scope

The intended product is an Amsterdam housing search agent focused on Funda.

The agent is expected to:

1. periodically inspect Amsterdam for-sale listings on Funda
2. extract relevant listing information
3. detect listings not seen previously
4. apply the agreed housing criteria
5. send matching new listings to Telegram (rich message with key
   listing metrics plus up to 3 property photos of the same listing;
   the text message gates the notified state, photos are best-effort)
6. store listing information for deduplication and future analysis

Exact product filtering criteria must come from the confirmed `product.md`.
If criteria are contradictory or unconfirmed, do not invent a final value.

---

## Secrets

Secrets must never be committed.

Expected local configuration:

```text
.env
```

Expected variables include:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

`.env` must be git-ignored.

A `.env.example` containing placeholders should be committed.

Never print or commit actual Telegram tokens.

User-editable housing search filters are configured in `config/filters.json`
(loaded by `src/config.py` via `FilterConfig.from_file()`), not in `.env`.
`.env` is reserved for secrets and environment-specific sensitive values such
as Telegram credentials.

---

## Working Principles

When working on this project:

1. Understand before modifying.
2. Read the relevant documentation first.
3. Check Git status before shared-repository work.
4. Prefer small, focused changes.
5. Prefer additive documentation changes.
6. Never silently resolve conflicting requirements.
7. Do not introduce paid infrastructure.
8. Respect the VPS memory ceiling.
9. Keep Funda as the only scraping target.
10. Leave enough documentation for another agent to continue the work.
11. Keep Gemini CLI and OpenCode CLI compatible through shared repository
    documentation rather than agent-specific assumptions.
