# MVP readiness — gaps to ship to the 3–4 person friend group (assessed 2026-06-20)

The owner asked "what else do we need for an MVP today?" naming four areas: onboarding refining,
easy email setup, easy Ollama setup, company-bank refresh cadence. Four read-only Explore agents
mapped the actual code state; the two load-bearing claims (the `resume.pdf` scheduler gate + the
default model name) were spot-verified by hand. **MVP target = a non-technical friend can install,
onboard, and see discovered + scored jobs** (discovery+scoring tool; the owner applies by hand —
[[project_personal_search_goal]]).

## Verdict in one line
Discovery/scoring/onboarding *wiring* is shipped and solid; the gaps are **first-run setup
friction** (Ollama, email) + an **onboarding completion cliff** (`resume.pdf`). Company-bank
refresh is **not a code gap** — it's a documented cadence decision.

## ✅ ALL FOUR SHIPPED 2026-06-20 (commit-only, NOT pushed; full suite 1300 green, +17 tests)

The gap analysis below is the record of what was found; here's what landed to close it:

- **#1 Ollama setup** — `av3 setup-llm` (mirrors `av3 install-browser`: pulls `gemma4:e4b` +
  `nomic-embed-text`, or prints the download link + copy-paste pulls if Ollama is absent).
  `doctor.check_llm` now ALSO checks the embed model (was a silent green→runtime-fail gap). README
  Install names the models; stale "Gemini fallback" string in `installer/post_install.ps1` fixed.
  Tests: `test_update.py` (setup-llm ×3) + `test_doctor_llm.py` (×5).
- **#2 Onboarding cliff** — the scheduler gate in `run_cmd` + `serve_cmd` is now **fact-bank-only**;
  a missing `resume.pdf` is a non-blocking note (it's only an apply-stage upload fallback —
  optimize generates a per-job résumé; the apply driver fails closed if neither exists). Plus
  inline required-field validation (contact name+email, ≥1 role, ≥1 skill) and a rewritten "done"
  screen (worker starts automatically / edit goals on the dashboard / no JSON-editing dead-end).
  Test: `test_cli_run.py::test_preflight_missing_resume_warns_but_runs`; validation/done browser-verified.
- **#3 Email setup** — guided **wizard step 7 "Connect email (optional)"** + `POST
  /api/onboarding/inbox`: verifies the IMAP creds with a LIVE login first, then splits secret
  (`AV3_IMAP_PASSWORD` → `<data_dir>/.env`) from non-secret config (enabled/user/host/port →
  `user_config.json`). `load_settings` now also loads `<data_dir>/.env`. `.env.example` gained the
  key; `doctor.check_inbox` added. Tests: `test_web_onboarding.py::TestInboxConnect` (×5) +
  `test_doctor_inbox.py` (×4). Browser-verified (step renders, Skip works, 0 console errors).
- **#4 Company bank** — no core change (confirmed). Shipped `scripts/register-discovery-task.ps1`
  (daily `av3 discover` Windows task, gather-only) + README "Keeping the job list fresh" cadence
  guidance. **Also fixed a real MVP blocker found en route:** `av3-launcher.{cmd,sh}` invoked the
  stale `av3.cli.main` module path (renamed to `auto_applier.cli.main` at the v2 cutover) → the
  double-click launcher would `ModuleNotFoundError`. Fixed both.

---

## 1. Onboarding — NOT MVP-ready (one real blocker + polish)

Wizard is 7 steps (contact → work-history → skills → work-auth → targeting → telemetry →
web-prefs), each saves immediately, re-entrant. Résumé-upload→extract, the goal-elicitation chat,
and the find-companies seed-boards probe are all **properly wired into the UI** (genuine strengths).

**BLOCKER — the `resume.pdf` completion cliff.** `cli/main.py:2493-2495`:
`scheduler_ready = fact_bank_path.exists() and resume_path.exists()` where
`resume_path = settings.artifacts_dir / "resume.pdf"`. But:
- `OnboardingStatus.is_complete` (onboarding.py) does NOT check `resume.pdf` — only the CLI gate does.
- The wizard never asks for a résumé PDF or writes `artifacts/resume.pdf` (the upload step runs LLM
  extraction into the fact bank; it does NOT save the file to that path).
- Net: a user finishes all 7 steps → the dashboard onboarding banner CLEARS (is_complete=true) →
  but the scheduler stays "Stopped" forever. The CLI prints the reason to **stderr**
  (`cli/main.py:2508-2523`), invisible to a dashboard user.
- **Fix options:** (a) RELAX the gate — discovery+scoring only need the fact bank (master.json);
  scoring is JD-vs-fact-bank and optimize GENERATES a per-job résumé. Require `resume.pdf` only
  when the apply stage runs (auto/assisted). VERIFY what consumes `artifacts/resume.pdf` first (it
  looks like an apply-path upload fallback). This is the cleanest discovery-only-MVP fix. AND/OR
  (b) surface the "scheduler not started: missing X" reason ON the dashboard (not just stderr).
  Recommended: (a)+(b).

**Polish gaps (non-blocking but friend-facing):**
- No inline required-field validation: saving an empty name/email "succeeds" at the API, the gate
  stays false, no error shown, the ✓ never appears → user is confused why the banner persists
  (onboarding.js saveContact, onboarding.py merge_contact accept empties).
- The "done" screen tells users to hand-edit `data/user_config.json` + `master.json` for later
  changes (onboarding.html ~285) — a dead end for non-technical users; doesn't link back to the
  wizard or say how to start the scheduler.
- No education step in the wizard despite `/api/onboarding/education` + status support existing →
  résumé-extracted education is silently dropped.
- Minor: no `x-cloak` on the wizard root (FOUC flash); stale onboarding.py docstring says extraction
  is "v3.1 / not built" though it's shipped.

## 2. Easy email reader setup — NOT MVP-ready (no guided path at all)

The inbox loop (fetch/classify/match/worker, `av3 inbox`) is built + live-verified, but turning it
on is **100% manual**:
- Hand-edit `user_config.json`: `inbox.enabled=true`, `inbox.user=you@gmail.com`.
- Hand-edit `.env`: `AV3_IMAP_PASSWORD=<16-char app password>` (read at `settings.py:438`
  `load_dotenv`). **`.env.example` has NO `AV3_IMAP_PASSWORD` entry** → a fresh user has no clue it
  exists.
- The only guidance is the `_inbox_setup_nudge` CLI text (`cli/main.py:~1151-1173`) — visible ONLY
  if the user already discovered + ran `av3 inbox`. No wizard step, no dashboard panel.
- `av3 doctor` has NO inbox check (doctor.py runs config/app_db/events_db/llm/backups/relay).

**Smallest fix:** an optional wizard step (or post-onboarding card) + `POST /api/onboarding/inbox`
that merges `{inbox:{enabled,user}}` via the existing `save_user_config` AND writes
`AV3_IMAP_PASSWORD` to `.env` (python-dotenv `set_key`). Add the `.env.example` line + a doctor
inbox check. No new deps, secret stays out of JSON (the `.env`-only rule holds). `creds_from_settings`
already gates on enabled+user+env-var gracefully.

## 3. Easy Ollama setup — NOT MVP-ready (zero automated path)

The pipeline assumes Ollama is installed + models pulled; nothing helps the user get there.
- Manual steps: install Ollama (~600MB), `ollama serve`, `ollama pull gemma4:e4b` (~9.6GB),
  `ollama pull nomic-embed-text`. None are prompted/sequenced/copy-paste presented.
- Default models (verified `settings.py:84-85`): completion `gemma4:e4b`, embed `nomic-embed-text`.
  (NB the installer pins `gemma4:e4b` too — `installer/build_installer.py:20`. Memory's "qwen3:8b"
  is the owner's *personal* override, not the shipped default.)
- `av3 doctor` checks the COMPLETION model presence (good, gives `ollama pull <model>` fix) but
  **never checks the embed model** → missing `nomic-embed-text` reports green, then fails at runtime
  with a semi-cryptic `EmbeddingError` during the resolver. Ollama-down is a WARN (doctor exits 0).
- No `av3 setup-llm`; README:73 names no model tags. Installer `post_install.ps1:139` has a stale
  "Cloud fallback (Gemini) will be used" string (Gemini was removed).
- Contrast: `av3 install-browser` (`cli/main.py:3291-3332`) is the exact pattern to copy.

**Smallest fix:** `av3 setup-llm` (install-browser analog: detect ollama, `ollama pull` both models
with live echo, or print the download URL + copy-paste commands if absent) + extend `doctor.check_llm`
to also check `embed_model` + README model commands + fix the stale installer string.

## 4. Company-bank refresh — NOT a code gap (cadence decision only)

Two distinct things, both fine for MVP:
- **Bundled dataset** `auto_applier/data/ats_companies.csv` (~9,935 cos, MIT ats-scrapers, static,
  loaded read-only via importlib.resources). No refresh mechanism exists — and **doesn't need one**:
  the confirm-probe drops dead/changed slugs, so staleness is harmless. `av3 seed-boards` is
  re-runnable by design (shuffles candidates before the `--limit` cap, additive dedup-safe merge,
  only dead slugs cached) → re-running grows coverage.
- **Discovery** (`DiscoverWorker`): runs every `cycle_interval_s` (default **60s**) under `av3 run`
  (wired at `cli/main.py:2289`), idempotent upsert, ~1 req/s per board, never quiet-gated. Or one-shot
  `av3 discover`.
- **Retention** (`retention.py`): SKIPPED/FILTERED prune at 30d, events 14d, maintenance tick 1h.
  **APPLIED is NEVER pruned** (dedup source of truth) — correct.

**Cadence answer:** dataset → re-`seed-boards` at onboarding + quarterly / when targeting changes;
rebuild the CSV from upstream ~every 6mo (maintenance release). Discovery → 60s under `av3 run`;
if not always-on, a Windows Task Scheduler task running `av3 discover` 1–2×/day. **The only gap is
operational:** nothing in the repo schedules discovery when `av3 run` isn't alive, and there's no
shipped Task Scheduler helper / startup script (doc + optional script, NOT code).

---

## Recommended "today" sequence (by what blocks a friend from value)

1. **Ollama setup** (`av3 setup-llm` + doctor embed-model check + stale-string fix) — foundational;
   without the LLM, scoring/extraction/optimize all fail. Small.
2. **Onboarding `resume.pdf` blocker** (relax the gate to fact-bank-only for the gather pipeline +
   surface the stopped-reason on the dashboard) + inline required-field validation + done-screen
   copy. Small–medium; removes the "I finished but nothing runs" cliff.
3. **Email setup wizard step** (`POST /api/onboarding/inbox` + .env write + .env.example + doctor
   check) — high value, least day-one-critical (jobs still discover+score without it). Small.
4. **Company bank** — no build: document the cadence (above) + optionally ship a `schtasks` helper
   for users not running `av3 run` continuously.

Items 1+2 are the true blockers to "onboard → see scored jobs." 3 is the next-best small build.
4 is a decision + a doc.

Related: [[project_future_directions]] (Direction 1 onboarding + Direction 4 email both "shipped"
at the CLI/wizard-wiring level — this doc is the FIRST-RUN-FRICTION layer on top), [[user_profile]]
(RTX 3080/16GB → the 8B-class local-LLM target), [[feedback_no_cost]] (local-first, the setup
helpers must stay zero-egress).
