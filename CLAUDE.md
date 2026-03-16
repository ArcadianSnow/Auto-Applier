# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Auto Applier** is a Python desktop app that automates job searching and applying on LinkedIn. It features a GUI setup wizard (tkinter) that walks users through configuration, then uses Playwright browser automation to search for jobs and auto-apply via Easy Apply. It also tracks skills gaps (questions/fields jobs asked that the resume didn't cover) and helps users improve their resume over time.

This is a personal/small-group project (shared repo among friends). **Everything must run locally and cost nothing.** No paid APIs, no cloud services, no external databases.

## Tech Stack

- **Python 3.11+** — core language
- **tkinter/ttk** — GUI wizard (built into Python, zero dependencies)
- **click** — CLI interface (alternative to the GUI)
- **Playwright** with `playwright-stealth` — browser automation (not Selenium — less detectable)
- **CSV files** — all data storage (openable in Excel, no database setup)
- **JSON** — user config (`data/user_config.json`)
- **pdfplumber** / **python-docx** — resume parsing
- **python-dotenv** — credential management
- **PyInstaller** — builds standalone `.exe` (dev dependency)

## Build & Run Commands

```bash
# Install dependencies
pip install -e .

# Install Playwright browsers (required first time)
playwright install chromium

# Launch the GUI wizard (default)
python run.py
python -m auto_applier

# CLI mode (alternative)
python -m auto_applier --cli configure
python -m auto_applier --cli run
python -m auto_applier --cli run --dry-run
python -m auto_applier --cli status
python -m auto_applier --cli gaps

# Build standalone .exe
pip install pyinstaller
python build.py
# Output: dist/AutoApplier.exe
```

## Architecture

### Module Boundaries

```
auto_applier/
  __main__.py          # Entry point — launches GUI by default, CLI with --cli flag
  main.py              # CLI commands (click), orchestrates the application workflow
  config.py            # Loads .env, app settings, path constants

  gui/                 # Desktop wizard UI (tkinter)
    wizard.py          # Main window, step controller, shared state
    styles.py          # ttk theme and style definitions
    steps/             # One frame class per wizard step
      welcome.py       # Step 1: intro + "Create Dummy Data" button
      credentials.py   # Step 2: LinkedIn email/password
      resume.py        # Step 3: file picker for PDF/DOCX
      personal.py      # Step 4: name, phone, city, LinkedIn URL
      preferences.py   # Step 5: job keywords + location
      ready.py         # Step 6: config summary + Run / Dry Run / Exit

  browser/             # LinkedIn-specific — knows about Playwright and DOM
    session.py         # Browser lifecycle, persistent context (reuses browser_profile/)
    linkedin_auth.py   # Login flow, session validation, re-auth
    job_search.py      # Search LinkedIn, parse job listings
    easy_apply.py      # Walk through Easy Apply modal steps
    anti_detect.py     # Stealth config, human-like delays, jitter

  resume/              # Platform-agnostic document parsing
    parser.py          # Extract text from PDF/DOCX
    skills.py          # Normalize and match skills keywords

  storage/             # Platform-agnostic data layer (CSV-backed)
    models.py          # Dataclasses: Job, Application, SkillGap
    repository.py      # CSV read/write operations

  analysis/            # Platform-agnostic gap analysis
    gap_tracker.py     # Compare job requirements vs resume, record gaps
    report.py          # Generate skills gap and status reports for CLI
```

**Key separations:**
- `gui/` is the desktop wizard — knows about tkinter, nothing else
- `browser/` is LinkedIn-specific — knows about Playwright and DOM selectors
- `storage/`, `analysis/`, and `resume/` are platform-agnostic
- Adding Indeed/Glassdoor later means adding new browser modules without touching the rest

### GUI Wizard Architecture

The wizard (`gui/wizard.py`) manages:
- **Shared state** via `dict[str, tk.Variable]` — all steps read/write the same StringVars
- **Step frames** — all 6 built at startup, swapped via `tkraise()` (no destroy/recreate)
- **Navigation** — Back/Next in footer (hidden on Welcome and Ready steps)
- **Dummy data** — fills all fields with realistic fake values and jumps to the summary step
- **Config save** — `ready.py` writes `.env` + `user_config.json` + copies resume to `data/resumes/`
- **Launch** — spawns `python -m auto_applier --cli run` in a new console window

### Data Storage (CSV files in `data/`)

All data is stored as CSV files that can be opened directly in Excel or any spreadsheet tool:

- **`data/jobs.csv`** — job_id, title, company, url, description, search_keyword, found_at
- **`data/applications.csv`** — job_id, status (`applied`/`failed`/`skipped`/`dry_run`), failure_reason, applied_at
- **`data/skill_gaps.csv`** — job_id, field_label (the question asked), category, first_seen
- **`data/user_config.json`** — personal info (name, phone, city, search keywords, resume path)

### Core Workflow (`run` command)

1. Parse resume and extract known skills
2. Check daily application limit (default 10/day)
3. Launch Playwright with persistent browser context (preserves cookies/session)
4. Ensure logged into LinkedIn (re-auth if session expired; pause for CAPTCHA if needed)
5. For each search keyword: search jobs → filter to Easy Apply → for each unseen job:
   - Get full job description, save to `jobs.csv`
   - Compare description to resume skills, log gaps to `skill_gaps.csv`
   - Walk through Easy Apply modal, filling fields from user config
   - Record any form field the system can't fill as a skill gap
   - Submit (or dry-run), save result to `applications.csv`
   - Random delay between jobs (60-180s)
6. Close browser

### Anti-Detection Strategy (Critical)

This is the primary technical risk. Key rules enforced in `browser/anti_detect.py`:

- **Rate limit hard:** 10-15 applications per day max. LinkedIn flags bulk apply behavior.
- **Human-like delays:** `random.uniform(min, max)` for every action. Per-character typing jitter (50-150ms). Scroll job descriptions before clicking apply.
- **Persistent sessions:** `data/browser_profile/` stores cookies/localStorage across runs. LinkedIn sees a returning user, not a fresh bot.
- **Headed mode always:** Never run headless — it's too easy to detect.
- **Hard stops on detection:** CAPTCHA → stop and alert user. "Suspicious activity" warning → stop for 24 hours. Never retry through these.
- **Realistic browser fingerprint:** Custom viewport, user-agent. Don't use Playwright defaults.

### Skills Gap Tracking (Differentiating Feature)

Two sources of gap data:
1. **Job descriptions** — `skills.py` compares job text against resume skills, logs missing ones
2. **Easy Apply form fields** — any field the system can't auto-fill gets recorded

The `gaps` command aggregates these by frequency: "15 jobs asked about AWS certification, 12 asked about Kubernetes, 8 asked for portfolio URL."

## Important Files (gitignored)

- `.env` — LinkedIn credentials (see `.env.example` for template)
- `data/*.csv` — Application data (contains personal info)
- `data/user_config.json` — Personal info and preferences
- `data/browser_profile/` — Persistent Playwright browser context
- `data/resumes/` — Uploaded resume files
- `dist/`, `build/`, `*.spec` — PyInstaller build artifacts

## Design Principles

- **Zero cost:** No paid APIs, no cloud, no external services. Everything local.
- **Transparency:** CSV storage means users can always inspect/edit their data in Excel.
- **Safety first:** `--dry-run` by default when testing. Hard daily limits. Human-like pacing.
- **LinkedIn DOM changes frequently:** Selectors in `browser/` modules will break. Use multiple fallback selectors and fail gracefully.

## LinkedIn ToS Risk

LinkedIn prohibits automated access. The `--dry-run` flag exists for testing without submitting applications. Users accept the risk of account restriction.

## Evolution Roadmap

The MVP is GUI wizard + LinkedIn + keyword-based gap matching. Planned phases:
- Better error recovery and `--dry-run` polish
- Local LLM-powered gap analysis (e.g., Ollama — replacing keyword matching, still free)
- Indeed/Glassdoor support (new `browser_*` modules)
- Resume auto-rewriting based on accumulated gap data
