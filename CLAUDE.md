# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Auto Applier v2** is a Python desktop app that automates job applications across multiple platforms (LinkedIn, Indeed, Dice, ZipRecruiter, and more). It uses AI (local Ollama or free Gemini API) to score jobs against multiple resumes, pick the best resume per job, fill application forms intelligently, generate tailored cover letters, and evolve resumes over time based on skill gap patterns.

This is a personal/small-group project (3-4 people). **Everything must run locally and cost nothing.** No paid APIs, no cloud services, no external databases. Requires **Python 3.11+**.

## Build & Run Commands

```bash
# Install dependencies
pip install -e .

# Install Playwright browsers (required first time)
playwright install chromium

# Launch the GUI wizard (default)
python run.py
python -m auto_applier

# CLI mode — core
python -m auto_applier --cli run
python -m auto_applier --cli run --dry-run
python -m auto_applier --cli run --platform linkedin
python -m auto_applier --cli run --limit 5
python -m auto_applier --cli doctor     # preflight checks
python -m auto_applier --cli status
python -m auto_applier --cli gaps
python -m auto_applier --cli resumes
python -m auto_applier --cli migrations # CSV schema migration history

# CLI mode — data tools
python -m auto_applier --cli show <job_id>       # full job detail view
python -m auto_applier --cli export -o jobs.csv   # export data
python -m auto_applier --cli fsck                 # data integrity check
python -m auto_applier --cli normalize            # fix data issues
python -m auto_applier --cli patterns             # conversion analytics
python -m auto_applier --cli reset-history --yes  # clear application history

# CLI mode — interview prep
python -m auto_applier --cli research <company>   # company briefing
python -m auto_applier --cli tailor <job_id> --resume <label>  # tailored resume PDF
python -m auto_applier --cli outreach <job_id> --resume <label>  # LinkedIn message
python -m auto_applier --cli story list           # STAR interview stories
python -m auto_applier --cli followup list --due  # pending follow-ups
python -m auto_applier --cli followup draft <job_id>  # draft follow-up email
python -m auto_applier --cli archetype list       # archetype routing config

# Tests (asyncio_mode = "auto" in pyproject.toml)
pip install -e ".[dev]"
pytest
pytest tests/test_scoring.py
pytest -k test_name

# Build standalone Windows .exe (PyInstaller, outputs to dist/)
python build.py
```

Test suite covers `llm/` (cache, prompts, router), `scoring/` (models, scorer thresholds), `storage/` (models, repository, migrations, integrity), `resume/` (manager, skills, evolution, cover letter, parser, tailor, outreach, story bank), `browser/` (anti-detect, captcha detection, form filler, budget counting), and `orchestrator/` (events, stop flag, ghost check, discover). Browser interactions and GUI require manual testing with `--dry-run`.

## Architecture

### Module Boundaries

- **`llm/`** — LLM abstraction layer. Fallback chain: Ollama (local) -> Gemini (free API) -> Rule-based (answers.json). Knows nothing about browsers or GUI.
- **`resume/`** — Multi-resume management, parsing, skill extraction, cover letter generation, evolution. Uses LLM for intelligence.
- **`scoring/`** — Job scoring pipeline. Scores every resume against each JD, picks the best match, decides auto-apply vs review vs skip.
- **`browser/`** — Browser automation with platform adapters. Uses FormFiller for AI-powered form filling.
- **`orchestrator/`** — Pipeline engine with event system. Ties everything together. Decoupled from GUI via EventEmitter.
- **`storage/`** — CSV-backed persistence (Excel-compatible). Dataclass models.
- **`analysis/`** — Gap tracking and reporting.
- **`gui/`** — Tkinter wizard + dashboard + panels. Knows about tkinter, nothing else.
- **`doctor.py`** — Preflight runner (`cli doctor`). Each check is a small function returning a `CheckResult(PASS|WARN|FAIL)`. Runs read-only, fast (<5 s), fails closed. Every FAIL/WARN carries a `fix` hint. Exits non-zero on any FAIL so CI / scripts can gate on it.

### LLM Fallback Chain (`llm/router.py`)

1. **Ollama** (localhost:11434) — local inference, zero cost. Default model `gemma4:e4b` (~4.5B effective, ~9.6 GB download, multimodal text+image+audio, 128k context). Requires **Ollama ≥ 0.8.0** for Gemma 4 architecture support. Alternative presets in `config.OLLAMA_MODEL_PRESETS`: `gemma4:e2b` (smaller/CPU), `gemma4:31b` (dev machines), plus legacy `gemma3:4b` and `llama3.1:8b` fallbacks.
2. **Gemini** (free API) — 1,000 req/day free tier, no credit card needed
3. **Rule-based** — fuzzy match against `data/answers.json`, always available

All LLM calls go through `LLMRouter.complete()` or `complete_json()`. Cache layer (72h TTL, SHA-256 keyed) sits in front. Seven prompt templates in `llm/prompts.py` — all JSON prompts declare their schema inline and demand JSON-only output with no thinking preamble or code fences, to keep Gemma 4's instruction-following reliable across backends.

### Multi-Resume System (`resume/manager.py`)

Users load multiple resumes (e.g., "Data Analyst", "Data Engineer"). Each gets a profile in `data/profiles/<label>.json` with LLM-extracted skills. For each job, `ResumeManager.score_all()` scores EVERY resume against the JD and picks the best match — based on skills/experience, not job title. The best resume is used for form filling, file upload, and cover letter generation.

### Job Scoring Pipeline (`scoring/scorer.py`)

- Score >= `auto_apply_min` (default 7) -> AUTO_APPLY
- Score >= `review_min` (default 4) -> USER_REVIEW (GUI shows review panel; CLI uses `cli_auto_apply_min`)
- Score < `review_min` -> SKIP

### Multi-Platform Adapter Pattern (`browser/`)

- **`base_platform.py`** — `JobPlatform` ABC: `ensure_logged_in()`, `search_jobs()`, `get_job_description()`, `apply_to_job()`. Shared helpers: `safe_query()`, `safe_click()`, `detect_captcha()`, `wait_for_manual_login()`.
- **`platforms/`** — One module per site, each subclassing `JobPlatform`.
- **`platforms/__init__.py`** — `PLATFORM_REGISTRY` dict.
- **`form_filler.py`** — Shared AI form filling: personal info -> answers.json -> LLM -> record gap. Handles cover letters and resume uploads.

Adding a new job site: create `browser/platforms/newsite.py` subclassing `JobPlatform`, register in `PLATFORM_REGISTRY`.

### Orchestrator (`orchestrator/engine.py`)

`ApplicationEngine` runs the full pipeline with **per-platform error isolation** (one platform crashing doesn't stop others). Event-driven via `EventEmitter` pub/sub — GUI subscribes to events, CLI uses print handlers.

Pipeline: discover -> fetch description -> score (all resumes) -> decide -> fill (AI) -> apply -> track gaps -> evolve resume.

**Event names** (defined in `orchestrator/events.py`): `run_started`, `resume_parsed`, `platform_started`, `platform_login_needed`, `platform_login_failed`, `search_started`, `jobs_found`, `job_scored`, `user_review_needed`, `application_started`, `application_complete`, `platform_error`, `platform_finished`, `evolution_triggers`, `run_finished`, `captcha_detected`. Most are fire-and-forget via `emit()`. User-blocking events (e.g. `user_review_needed`) use `emit_and_wait()` — the GUI handler must call `resolve_event(name, result)` to unblock the pipeline (5-minute default timeout).

**Registered platforms** (`browser/platforms/__init__.py`): `linkedin`, `indeed`, `dice`, `ziprecruiter`.

### Cross-Source Deduplication (`storage/dedup.py`)

Canonical hashing: `normalize_title(title) + normalize_company(company)` -> SHA-256. Before scoring, the engine checks `job_seen_canonically(hash)` to skip cross-posted duplicates (same job on LinkedIn and Indeed). Per-source dedup via `job_already_applied(job_id, source)` catches re-runs.

### Ghost Job Detection (`browser/ghost_check.py`)

LLM-powered analysis of job descriptions to detect likely ghost/stale postings. Jobs scoring >= `GHOST_SKIP_THRESHOLD` (default 8, configurable via env var) are skipped before the apply step to avoid wasting time. Uses the `GHOST_JOB_CHECK` prompt template. Dry runs skip this check.

### Follow-Up Cadence (`storage/repository.py`, `main.py followup group`)

After each successful application, `schedule_followups()` creates pending follow-up records at configurable intervals (default 7/14/21 days). `cli followup list --due` shows actionable items. `cli followup draft` generates personalized follow-up emails via the `FOLLOWUP_EMAIL` prompt, with tone progression across attempts.

### Multi-Dimensional Scoring (`scoring/models.py`)

Seven weighted axes: skills (0.35), experience (0.20), seniority (0.15), location (0.10), culture (0.08), growth (0.07), compensation (0.05). LLM scores each axis 0-10 via `SCORE_DIMENSIONS` prompt. Python computes the weighted total — users can re-tune weights in `user_config.json:scoring_weights` without re-running the LLM. Falls back to legacy single-score prompt on parse failure.

### Archetype Classification (`resume/archetypes.py`)

Opt-in feature. When `data/archetypes.json` exists and multiple resumes are loaded, classifies each JD into an archetype (e.g., "data_analyst", "data_engineer") and routes scoring to matching resumes only. Falls back to scoring all resumes if no archetype matches confidently.

### Interview Prep Extensions

- **Story Bank** (`resume/story_bank.py`, `cli story`) — STAR+Reflection interview stories generated from resume + JD via `STAR_STORIES` prompt. Persisted in `data/stories/`. Subcommands: `list`, `show`, `export`, `prune`.
- **Outreach** (`main.py`, `cli outreach`) — LinkedIn connection-request messages (<280 chars) via `OUTREACH_MESSAGE` prompt.
- **Tailor** (`resume/tailor.py`, `cli tailor`) — Per-JD resume rewrite via `TAILOR_RESUME` prompt, rendered to PDF via Playwright.
- **Research** (`main.py`, `cli research`) — Company briefing via `COMPANY_RESEARCH` prompt. Cached in `data/research/`.

### Data Integrity (`storage/integrity.py`)

- `cli fsck` — Validates referential integrity (orphan applications, missing jobs, score range), reports issues.
- `cli normalize` — Fixes common data issues (empty fields, duplicate rows, date format normalization).
- `cli patterns` — Conversion analytics: success rates by platform, score distribution, resume performance.

### Resume Evolution (`resume/evolution.py`)

Tracks skill gap frequency. When a skill appears >= 3 times, triggers a user prompt. User confirms skill level -> LLM generates resume bullet points -> user approves -> saved to that resume's profile. Original resume files are never modified.

### Data Storage

CSV files in `data/` (openable in Excel):
- `jobs.csv`, `applications.csv`, `skill_gaps.csv`

**Schema migrations** (`storage/migrations.py`): CSV schemas drift as dataclass models gain/lose fields between versions. The migration layer runs transparently before every `load_all()` / `save()`. On header drift it backs up the old file to `data/.backups/<name>.<timestamp>.csv`, rewrites the live file with the current canonical header, preserves overlapping columns, backfills new columns with dataclass defaults, and drops removed columns (archived only in the backup). All migrations are recorded in `data/.schema_version.json`. Inspect history with `cli migrations`. Forward-only — no down-migrations. When adding a field to any model in `storage/models.py`, you don't need to write a migration by hand.

JSON files in `data/`:
- `user_config.json` — personal info and preferences
- `answers.json` — pre-configured Q&A (starts blank, populated in wizard, grows from encounters)
- `unanswered.json` — new questions from runs, queued for next wizard open
- `profiles/<label>.json` — per-resume enhanced profiles
- `prompted_skills.json` — skills already prompted for evolution

Path constants in `config.py`.

### Browser & Anti-Detection (Critical)

- **patchright** first, standard Playwright fallback
- **Real Chrome** via `channel` param
- **Manual login ONLY** — never automate credentials for any platform
- **Headed mode always**
- **Bezier mouse paths** + typing jitter + organic noise + distraction pauses
- **Rate limiting:** configurable, default 10 apps/day, 60-180s between apps
- **Hard stop on CAPTCHA** — never retry through detection

### Credential Flow

- Passwords in `.env` only (pattern: `{PLATFORM}_EMAIL`, `{PLATFORM}_PASSWORD`)
- `GEMINI_API_KEY` also in `.env`
- `orchestrator/engine.py:_load_credentials()` merges `.env` into config at runtime

## Entry Points

- `python -m auto_applier` -> `__main__.py` -> GUI by default, CLI with `--cli`
- `python run.py` -> convenience wrapper
- `auto-applier` CLI (via pip install) -> `main.py:cli()` (Click group)

## Important Constraints

- **Zero cost:** No paid APIs, no cloud, no external services. Everything local.
- **CSV over databases.** Users can inspect/edit their data in Excel.
- **`--dry-run` for testing.** Never submit real applications during development.
- **Selectors break frequently.** Use multiple fallback selectors via `safe_query()` and fail gracefully.
- **Add scrollbars** to any GUI panel that might overflow.
- **Original resume files are never modified.** Evolution writes to `data/profiles/`.

## Important Files (gitignored)

- `.env` — credentials and API keys (see `.env.example`)
- `data/*.csv`, `data/*.json` — personal data
- `data/browser_profile/` — persistent browser context
- `data/resumes/` — uploaded resume files
- `data/profiles/` — enhanced resume profiles
- `data/cache/` — LLM response cache
