# Auto Applier

AI-assisted job-application automation for Indeed, Dice, and ZipRecruiter. Runs locally on your machine. Scores each posting against your resume, fills the form, and submits — with you watching the browser the whole time.

> **Status**: dry-run validated across all three platforms. Real runs work but are bound by anti-bot rate limits — see *How fast it goes* below.

---

## What it does

- Reads your resume(s) and personal info from local files.
- Searches each enabled job board for your keywords + location.
- Scores every job posting against every resume on seven weighted axes (skills, experience, seniority, location, culture, growth, compensation).
- For each high-scoring job: opens the apply form, fills it out (using your personal info, your `answers.json` Q&A, or an LLM as fallback), and submits.
- Tracks unanswered questions as "skill gaps" so you can teach it new answers as it learns the questions employers actually ask.
- Saves every job and application to CSVs you can open in Excel.

It will **never** automate your login. Whenever a platform asks you to sign in, the browser pauses for you to do it manually.

---

## First-time setup

Everything is handled by `setup.bat`. You don't need to install Python yourself if you're on Windows 10 (1909+) or Windows 11 — `setup.bat` will install it silently in the background using the built-in `winget` package manager.

### The easy way (Windows)

1. **Download or clone this repo.**
2. **Double-click `setup.bat`.**
3. Follow the on-screen prompts. The script:
   - Detects Python; if missing, installs Python 3.12 silently via `winget` (~2 minutes).
   - Installs project dependencies (~30 seconds).
   - Installs the browser engine (~1 minute).
   - Opens the graphical setup wizard.
4. The wizard walks you visually through:
   - Installing Ollama (free local AI)
   - Pulling the AI model (~10 GB, one time)
   - Adding a free Gemini API key (recommended backup AI — one-click "Open AI Studio" button)
   - Personal info (name, email, address, work auth, salary expectations)
   - Resumes
   - Preferences (which job boards, search keywords, location)
   - Pre-baked Q&A for common screener questions
5. **Daily use after that: double-click `run.bat`.**

If your Windows is too old for `winget`, the script opens python.org for you with a clear "install Python with the PATH box checked, then re-run setup.bat" message.

### The manual way (any OS, or if you prefer the terminal)

```bash
# 1. Install the app and its dependencies (one-time)
pip install -e .

# 2. Install the browser engine (one-time)
playwright install chromium

# 3. Run the setup wizard (handles Ollama, Gemini key, personal info, resumes, etc.)
python -m auto_applier

# 4. Verify everything is ready
python -m auto_applier --cli doctor
```

`doctor` runs 13 preflight checks. If they're all green, you're good to go. If anything is yellow or red, the message tells you how to fix it.

> **Why a Gemini key?** Ollama is free and local but sometimes returns empty responses on long prompts. Gemini's free tier (1500 requests/day, no credit card) catches the gaps so the form filler doesn't dead-lock on a question Ollama choked on. The wizard has a one-click "Open Google AI Studio" button — you create the key, paste it in, click Test.

---

## Getting updates

Auto Applier is still being improved. To pull the latest version:

**Easiest (Windows): double-click `update.bat`.**

It will:
1. Check GitHub for the latest version (silently, ~1 second).
2. If there's nothing new, say "Already up to date" and close.
3. If there's an update, show you the new version number and ask `Update? [Y/n]`.
4. Download the new code, replace your source files, and re-install dependencies.

Your `data/` folder, your `.env` file, and your saved resumes are **never touched** — only the program code itself gets refreshed. Your applications history, gap data, profile, and Q&A library all survive an update.

No git installation required. No PowerShell knowledge required. Same one-click experience as `setup.bat`.

If something goes wrong mid-update, just re-run `setup.bat` — it will repair anything that was half-installed.

---

## Daily use

**Easiest (Windows): double-click `run.bat`** — opens the dashboard.

Or from the terminal:

```bash
# Open the GUI dashboard
python run.py

# Or run from the CLI:
python -m auto_applier --cli run --dry-run               # safe — never submits
python -m auto_applier --cli run --platform indeed       # one platform
python -m auto_applier --cli run --limit 5               # cap applies per run
python -m auto_applier --cli run                         # real run, all enabled platforms
```

When the run starts, a Chrome window opens. **Log in to each platform manually** when it asks you to. The run resumes automatically once login is detected.

> **`--dry-run` is your friend.** It walks every form to the very last step and shows what *would* be submitted, without actually clicking Submit. Use it when you change your resume, edit `answers.json`, or after pulling new code.

---

## When a platform asks for verification

Job boards aggressively detect automation. When that happens:

- The CLI prints a clear box: **`ACTION NEEDED`** or **`CAPTCHA DETECTED`**, with the platform name and instructions.
- The browser window stays open with the verification challenge visible. **Solve it manually.**
- After 3 consecutive solves on a single platform, it's probably worth taking that platform's day off — the bot-detection fingerprint needs time to cool.

If a platform times out waiting for you to verify, it goes into a **4-hour cooldown** automatically. This protects your account from being flagged and stops `--continuous` mode from hammering the same wall.

```bash
# Check what's in cooldown
python -m auto_applier --cli pauses

# Clear a cooldown manually (only do this if you're sure the heat is off)
python -m auto_applier --cli unpause indeed
python -m auto_applier --cli unpause all
```

---

## Editing your answers

Two files matter:

- **`data/user_config.json`** — your name, email, phone, address, work authorization, salary expectations. Edit through the GUI wizard, or by hand. The form filler reads these for "personal info" fields.
- **`data/answers.json`** — pre-baked answers to common screener questions. Format: `{"Question text": "Answer text"}`. The filler matches the question (exact, then substring, then fuzzy at 60% with a length guard) and uses the answer.

If a question isn't matched anywhere, the LLM tries to answer it from your resume + the JD. If even the LLM is empty, the run logs a "skill gap" so you can add it to `answers.json` later.

---

## How fast it goes

Slow on purpose. Defaults:

- **10 applications/day max** — every job board flags accounts that apply faster than humans.
- **60–180 seconds between applications** (random within range) — not a typical-bot interval.
- **Sometimes 0/5 fields filled** — that just means the form was already pre-populated by your saved profile on the platform. Not a bug.

You can tune these in `.env` (`MAX_APPLICATIONS_PER_DAY`, `MIN_DELAY_BETWEEN_APPLICATIONS`, etc.) but be careful — going faster doesn't make you applications, it makes you bans.

---

## Helpful commands

```bash
# Status & data
python -m auto_applier --cli status                 # today's run summary
python -m auto_applier --cli show <job_id>          # everything known about one job
python -m auto_applier --cli gaps                   # unanswered screener questions
python -m auto_applier --cli almost                 # high-score jobs that needed manual apply
python -m auto_applier --cli patterns               # success rates by platform / score / resume

# Interview prep (uses local LLM)
python -m auto_applier --cli research <company>     # company briefing
python -m auto_applier --cli tailor <job_id> --resume <label>   # tailored resume PDF
python -m auto_applier --cli outreach <job_id> --resume <label> # LinkedIn message draft
python -m auto_applier --cli story list             # STAR interview stories
python -m auto_applier --cli followup list --due    # pending follow-ups

# Maintenance
python -m auto_applier --cli doctor                 # preflight checks
python -m auto_applier --cli fsck                   # data integrity check
python -m auto_applier --cli normalize              # repair data inconsistencies
python -m auto_applier --cli pauses                 # active platform cooldowns
```

---

## When things go wrong

**"All LLM backends failed for prompt"** — Ollama returned empty text and Gemini failed (or no key). Check `--cli doctor` output. The next prompt usually recovers; if it persists for 3+ in a row, the router temporarily disables the bad backend and falls back to the rule-based answer file.

**"form stuck on questions/N: validation: Select an option"** — A required field on the apply form wasn't filled correctly. Check `data/logs/run-*.log` for the question text, then add an entry to `answers.json` for it.

**"Indeed: 0 jobs found" + Cloudflare in the log** — Indeed is challenging the browser. Open the Chrome window, solve the challenge, then retry. If it persists, give Indeed a 12-24 hour rest.

**"Apply login gate timed out"** — The platform required re-auth mid-apply. Either click sign-in fast enough next time, or accept that the platform is bot-walling hard right now and let the cooldown ride.

---

## File map (the parts you'll edit)

```
.env                       — passwords + API keys (gitignored, never committed)
data/user_config.json      — your personal info
data/answers.json          — your pre-baked Q&A
data/resumes/              — your resume files (.docx / .pdf)
data/jobs.csv              — every job we've seen
data/applications.csv      — every application we've made
data/skill_gaps.csv        — questions we couldn't answer (yet)
data/logs/run-*.log        — debug trails for individual runs
```

Everything in `data/*` is gitignored. Don't share these — they have your personal info.

---

## Project ground rules

1. **Free, local, zero-cost.** No paid APIs, no cloud, no external databases. Ollama runs on your machine; Gemini's free tier is enough.
2. **Manual login only.** The app waits for you to type your password — it never has it.
3. **Headed browser, real Chrome.** No headless mode, no Chromium-stealth tricks beyond what patchright already does.
4. **Stop on CAPTCHA.** If a platform shows you a verification challenge, the platform goes into cooldown. Don't fight it.
5. **CSV over databases.** All app data is plain CSV — open it in Excel, edit it by hand, sort it however you want.

If you want to change any of these, take a beat first — they exist because of the lessons learned during the v2 rebuild.
