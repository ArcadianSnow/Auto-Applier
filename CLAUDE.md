# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ Working discipline — ALWAYS document research (read first)

**The most important rule for this repo: always write investigations, research, and findings into a file
structure you can easily read and reference — never leave them only in the conversation.** A finding that
lives only in chat is one the next session wastes time re-deriving. Writing it down is part of "definition of
done," like passing tests.

- **Invoke the `auto-applier` skill at the start of any session** — it's the single-source-of-truth router +
  working discipline for v3. For long iterative/debug work, also invoke the `unstuck` skill.
- **Research / investigation findings →** `.claude/skills/auto-applier/research/<topic>.md`.
- **Architecture & design decisions →** `docs/v3-architecture.md` (the v3 spec), and mirror the rationale in
  the project memory ([[project_v3_rewrite]]).
- When a session learns something durable, record it in the right place **before** calling the task done —
  see the `auto-applier` skill's "How to extend this skill" section.

> **The active codebase is v3, in the `auto_applier/` package** (built as `av3/`, renamed at the
> v3→master cutover on 2026-05-30 when v3 superseded and replaced v2). The CLI command verb stays
> `av3`; only the package/import namespace is `auto_applier`. The legacy v2 app has been deleted —
> its lessons live in git history and the spec's §1 root-cause table. For any design question,
> `docs/v3-architecture.md` and the `auto-applier` skill are authoritative.

## Project Overview

**Auto Applier v3** is a local-first desktop service that automates job applications. It discovers jobs (ATS
APIs + browser boards), scores each JD against a single **master fact bank**, generates a tailored résumé +
cover letter per job under a **fabrication guard**, and applies — fully automatic on clean ATS forms,
**assisted** (bot pre-fills, human submits) on hostile ones. It runs as an always-on background **worker**
controlled from a **local web dashboard**.

Personal/small-group project (3–4 people). **The core pipeline runs fully locally and costs nothing.** The
*one* scoped exception is opt-in, scrubbed error telemetry (§9, default OFF). Requires **Python 3.11+**.
Install the v3 extras: `pip install -e ".[v3]"`.

### Why v3 exists (what v2 fought against)

CSV-as-database (schema drift, no atomic writes), browser-apply as the spine (anti-detect arms race),
inferred-not-stored state (dedup hacks), a synchronous per-platform pipeline, and no observability. v3 fixes
the **root causes**: SQLite system-of-record, API-for-discovery + browser-for-apply, an explicit job state
machine, staged async workers over a status queue, and an automatic event spine. See spec §1.

## Build & Run Commands

```bash
pip install -e ".[v3]"            # core v3 deps (FastAPI, pydantic, httpx, packaging, …)
av3 install-browser               # fetch Chromium (first run; real Chrome via channel is the primary path)

# One-click launcher (non-technical entry): starts the worker+server, opens the dashboard tab
av3 launch

# Web UI + background worker service (power-user entry)
av3 serve [--host H] [--port P] [--no-scheduler] [--dry-run/--no-dry-run] [--mode auto|assisted]

# Always-on staged loop, headless (no web UI) — THE production loop
av3 run [--max-cycles N] [--quiet-hours HH:MM-HH:MM] [--dry-run/--no-dry-run]

# Per-stage workers (testing / doctor) — each drains one state, --once
av3 filter | score | optimize | apply   [--once] [--limit N] [--no-llm]

# Setup / health
av3 init-db                       # create data dir + app.db + events.db
av3 doctor                        # preflight (config, DBs, LLM, backups, relay); exits non-zero on FAIL
av3 status                        # job counts by state

# Observability (read straight from events.db — no log files; spec §9)
av3 errors [--stage X] [--platform X] [--since 30m|2h|7d] [--run-id ID] [--limit N] [--json]
av3 stats  [--platform X] [--since ...] [--run-id ID] [--json]

# Telemetry — opt-in remote error mirror (default OFF; only network egress in the product)
av3 telemetry on [--handle NAME] [--relay-url URL]   # shows the §9 disclosure, opts in
av3 telemetry off
av3 telemetry status
av3 mirror drain [--limit N]      # out-of-band drainer: POST queued scrubbed rows to the relay
av3 export-diagnostics [--raw]    # support tarball (scrubbed by default; --raw is PII-bearing)

# Data lifecycle + distribution
av3 backup                        # snapshot app.db + events.db, rotate
av3 prune [--ephemeral-days N] [--events-days N]
av3 update [--exit-code]          # check the GitHub release feed; prompt if newer (no auto-replace)
av3 survey                        # multi-ATS CAPTCHA-presence survey (dry-run, never submits)

# Tests
pip install -e ".[v3,dev]"
pytest tests_v3/                  # 612 green / 11 deselected (live smoke/eval/integration markers)
pytest tests_v3/test_apply_worker.py -k name

# Build the standalone executable (PyInstaller; build-host tool, not a runtime dep)
pip install pyinstaller && python build_v3.py     # → dist/AutoApplierV3
```

`--dry-run` is the dev default everywhere an apply could fire; `--no-dry-run` is the gated path that submits
real applications. Live ATS smoke tests are marked `smoke` and excluded by default (run via cron to catch
selector drift — the #1 v2 bug source).

## Architecture

> Package root is **`auto_applier/`** (built as `av3/`, renamed at the v2 cutover 2026-05-30). The CLI
> command verb stays `av3`; only the import namespace is `auto_applier`. Full spec: `docs/v3-architecture.md`.

### Module layout

- **`config/`** — Pydantic Settings (`load_settings()` reads `user_config.json` + `.env`, validates on
  construction so `doctor` fails fast). Derived paths (`app_db_path`, `events_db_path`, `artifacts_dir`, …).
- **`db/`** — SQLite engine + `schema.sql` + repositories. `app.db` holds jobs / job_scores / applications /
  skill_gaps / answers. **SQLite is the system of record; CSV is an export format.**
- **`domain/`** — pure dataclasses + the **job state machine** (`state.py`, one allowed-transitions table).
- **`llm/`** — router (Ollama→Gemini→rule, ported from v2), `complete`, `embed` (nomic-embed-text), prompts.
- **`scoring`** lives across `llm/` + `pipeline/score_worker.py` — 7 weighted axes, JD vs the master profile.
- **`resume/`** — `factbank` (the single master fact bank), `generate` (per-job résumé), `guard` (fabrication
  guard, fail-closed to REVIEW), `render` (ATS-safe single-column PDF), `answer_resolver` (two-tier: semantic
  match → bail-to-REVIEW → confidence-gated backup), `seed_answers`.
- **`sources/`** — capability-based adapters. `greenhouse` / `lever` / `ashby` / `jobspy` discover via API or
  browser; `browser/` has the apply drivers (`greenhouse_apply`, `lever_apply`, `ashby_apply`), the stealthy
  `session` (patchright + real Chrome via channel, persistent profile), CAPTCHA/confirmation `detect`, and the
  CAPTCHA-presence `survey`. **LinkedIn is CUT from v3.**
- **`pipeline/`** — the staged workers (`filter` → `score` → `optimize` → `apply`), the `scheduler`
  (always-on loop), `quiet_hours`, `retention` (prune + backup), and the **`@stage` wrapper** (`stage.py`)
  that auto-emits start/ok/error/skip events — the entire observability story, no scattered logging.
- **`telemetry/`** — the always-local `EventSink` (`events.db`), the PII `scrub`ber, the opt-in `mirror`
  queue + `MirrorPolicy`, the relay `client` (drainer), and the `diagnostics` tarball builder.
- **`web/`** — FastAPI app + Alpine.js dashboard (live pipeline, review queue, login-needed badges, history),
  SSE event feed, `ControlState` (manual / F6 hotkey / idle-detect), headed-browser launcher for
  login-on-demand + assisted submit, the onboarding wizard, and the one-click `launch`.
- **`doctor.py`** — preflight checks (config, app.db, events.db, LLM reachable, backups recent, relay
  reachable). Each returns a `CheckResult(PASS|WARN|FAIL)` with a `fix` hint; exits non-zero on any FAIL.
- **`update.py`** — GitHub Releases version check (check + prompt, never auto-replace).

### Job state machine (`domain/state.py`)

```
DISCOVERED → (dedup/ghost) → SKIPPED
           → (embedding pre-filter) → FILTERED | DESCRIBED
DESCRIBED → SCORED → DECIDED → { QUEUED_APPLY | REVIEW | SKIPPED }
QUEUED_APPLY → APPLYING → APPLIED (terminal, requires positive confirmation)
                       → FAILED → REVIEW (mid-form break: fail fast, no retry)
```

The queue **is** the `state` column; workers drain by status. Only `APPLIED` counts for dedup, so an
unconfirmed apply is safely retryable and never inflates success. Transitions live in one allowed-transitions
table.

### Staged pipeline (`pipeline/`, spec §7)

`filter` (embedding cosine pre-filter, fail-open) → `score` (LLM 7-axis vs master profile, fail-closed) →
`optimize` (**Strict gate**: generate résumé + cover letter + fabrication guard; ALL must pass or → REVIEW) →
`apply` (detection-risk router picks auto vs assisted; two-tier answer resolver fills the form; positive
confirmation → APPLIED). The `scheduler` runs these in pipeline order each cycle, 24/7, with optional quiet
hours that pause **only** the apply stage (gather stages keep running — being wrong in gather doesn't
compound).

### Reliability invariants (NEVER compromise for throughput)

- **Manual login only; headed browser only; never retry through CAPTCHA** → downgrade to assisted.
- **Mid-form break → fail fast to REVIEW, no retry** (retries risk duplicate/garbled submissions).
- **`APPLIED` only on a positive submit confirmation** (Greenhouse `/confirmation`, Lever `/thanks`, Ashby
  in-place panel + `success:true` XHR) — never off a confirmation email alone.
- **Fabrication guard:** a generated résumé may use ONLY facts in the bank; any unsupported company / title /
  date / credential / skill drops the job to REVIEW. The bank is the single source of truth.

### Telemetry (the one scoped cloud exception, spec §9)

Every `@stage` writes to local `events.db` always. The **opt-in** remote mirror (default OFF) sends a
**scrubbed subset** of two event classes — errors and inferred-answer metadata — keyed by
`user_id = sha256(handle)[:10]`. **The answer value and EEO data NEVER leave the machine** (enforced
structurally: the scrubber schema has no field for them). Flow: worker enqueues a category-scrubbed row into
`mirror_queue` (in `events.db`) → `av3 mirror drain` POSTs it to an **owner-hosted relay** (Cloudflare Worker
in `relay/`) → the relay re-scrubs, rate-limits, and inserts into a shared Turso DB. **The Turso write token
lives only in the relay, never in the app.** Toggle with `av3 telemetry on|off|status`.

### Distribution (spec §11a)

`build_v3.py` produces a lean PyInstaller executable (`run_v3.py` entry: no-arg → `av3 launch`, args → full
CLI). **Chromium is fetched on first run** (`av3 install-browser`), not bundled — Playwright resolves browsers
via its own cache, and most applies use the user's real Chrome via channel anyway. `av3 update` checks the
GitHub release feed and prompts (no in-place auto-replace in v3.0).

## Data storage (spec §4)

`data/v3/` (relocatable via `AV3_DATA_DIR`):
- `app.db` — jobs, job_scores, applications, skill_gaps, answers (SQLite, the system of record).
- `events.db` — the observability spine + the `mirror_queue` table (higher write rate, pruned on a shorter
  window).
- `profile/master.json` — the master fact bank (contact, work history, skills, work-auth, optional EEO).
- `user_config.json` — typed settings (targeting, telemetry, scheduler, web, scoring weights). `.env` holds
  secrets (`GEMINI_API_KEY`) — never in the JSON.
- `artifacts/` — generated résumés / cover letters (files; the DB stores paths).
- `browser_profile/` — one persistent shared Chrome profile across all sites.
- `.backups/` — rotated SQLite snapshots. `diagnostics-<ts>.tar.gz` — support bundles.

Generated artifacts stay as files; the DB stores their paths. `APPLIED` history is kept indefinitely (dedup
source of truth); ephemera (SKIPPED/FILTERED) and events prune on configurable windows.

## Important constraints

- **Zero cost for the core.** No paid APIs / cloud / external DBs for the pipeline. The *only* egress is the
  opt-in, scrubbed, default-OFF telemetry mirror.
- **SQLite is the source of truth.** Inspect via DB Browser for SQLite or `av3 export`/CSV.
- **`--dry-run` for development.** Never submit real applications while building.
- **Selectors drift.** Use multi-fallback selectors; live `smoke` tests catch drift between releases.
- **Add scrollbars / good contrast** to any web UI panel that can overflow; the dashboard is keyboard-navigable.
- **Original résumé files are never modified.** Generation writes to `artifacts/`; evolution proposes
  fact-bank additions the user approves.
- **Tests live in `tests_v3/`.** Keep them green; document durable findings in the `auto-applier` skill's
  `research/` before calling a task done.

## Legacy v2 — deleted (history only)

v2 was a Tkinter + CSV desktop app (multi-résumé score-all-pick-best, synchronous per-platform pipeline,
browser-apply spine, LinkedIn discovery). It was **deleted at the v3→master cutover (2026-05-30)** once v3
superseded it; the `auto_applier/` name now belongs to the v3 package. v2's design and the reasons it was
replaced live in the git history (before the "Retire v2" commit) and the spec's "v2 root cause" table (§1) —
consult those for lessons; there is no v2 code in the tree to extend.
