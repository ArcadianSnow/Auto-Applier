# Onboarding / setup restructure — Phase 1 (SHIPPED 2026-06-21)

**Status:** SHIPPED. Terminal-free first-run setup is live for BOTH the exe path (future) and
today's pip users. Full suite **1327 green** (19 new tests). This is Phase 1 of the exe plan in
[exe-distribution-viability.md](exe-distribution-viability.md) — the highest-value slice
(packaging is Phase 2, still deferred/unbuilt).

## The problem it fixed

There was **no first-run setup orchestration anywhere in the app.** `av3 setup-llm` (pull the
multi-GB Ollama models) and `av3 install-browser` were CLI-only/manual; the onboarding wizard
never ran them. A non-technical friend reaching the dashboard hit **silent scoring/discovery
failure** — no models pulled, no browser fetched. The exe is the easy 15%; this guided in-app
flow is the 85% that mattered.

## What shipped (file by file)

- **`auto_applier/setup_ops.py` (NEW)** — the shared module behind CLI + web:
  - `pull_models(settings, progress_cb) -> PullResult` — streams Ollama's HTTP
    **`POST {host}/api/pull` NDJSON** (per-layer `total`/`completed` → percent), the same HTTP
    surface the app already uses for `/api/tags` + `/api/embeddings`. On a connection failure
    returns `error="ollama_not_running"` (can't distinguish "not installed" from "not running"
    over HTTP — both mean "show the Get/Start-Ollama link"). Blocking → callers run it in
    `asyncio.to_thread`.
  - `install_browser(progress_cb, backend="auto") -> InstallResult` — lifts the old inline CLI
    loop (`subprocess.run([sys.executable, "-m", pkg, "install", "chromium"])`, patchright then
    playwright). A running/done/error **spinner** (capture_output → no byte-progress).
  - `readiness(settings) -> list[CheckResult]` = `[check_llm, check_browser]` (focused setup
    checklist, not the full `run_doctor()` dump).
  - `ensure_data_dirs(settings)` — creates data/artifacts/**backups** (serve previously skipped
    backups_dir → spurious `check_backups` WARN).
- **`auto_applier/doctor.py`** — NEW `check_browser(settings)` (WARN-only; real Chrome via channel
  may cover the apply path). Detection is read-only: `importlib.util.find_spec` driver probe +
  glob the playwright/patchright cache (`_browser_registry_dirs` / `_bundled_chromium_present`,
  honoring `PLAYWRIGHT_BROWSERS_PATH`) + the filesystem Chrome check. Appended to `run_doctor()`
  after `check_llm` → `av3 doctor` now reports browser readiness (8 checks).
- **`auto_applier/sources/browser/session.py`** — extracted `BrowserSession._detect_chrome_channel`
  to a module-level `detect_chrome_channel() -> str|None` (the method delegates); `check_browser`
  imports the free function.
- **`auto_applier/web/routes.py`** — mirrors the seed-boards background-job pattern verbatim:
  module-level `_SETUP` registry keyed by action + `_SETUP_TASKS`; `GET /api/setup/readiness`;
  `POST/GET /api/setup/{action}/start|status` (action ∈ {pull-models, install-browser}, unknown →
  404, idempotent while running); `_run_pull_models_job`/`_run_install_browser_job` run the
  blocking helper via `asyncio.to_thread` with an in-thread `progress_cb` doing GIL-safe in-place
  `_SETUP[action].update(frag)`.
- **Frontend** — `onboarding.js`+`onboarding.html`: a new FIRST wizard step **"Set up the AI
  engine"** (Ollama detect → Pull AI models w/ progress bar → Install the browser spinner →
  readiness checklist → non-gated Continue). Generalized `_pollSeed` into `_pollSetup(action,key)`;
  added `loadReadiness/startPull/startBrowserInstall/setupLlmUnreachable`. `app.js`+`dashboard.html`:
  a **"Setup needed"** card (after the onboarding banner) that renders non-PASS readiness items with
  Pull-models / Install-browser buttons hitting the same endpoints (so an already-onboarded user can
  fix a missing model without re-walking the wizard). `app.css`: `.progress`/`.progress-bar`/
  `.readiness-*`/`.setup-card`.
- **CLI refactors** — `install-browser` now calls `setup_ops.install_browser`; `serve` + `init-db`
  call `setup_ops.ensure_data_dirs`. **`setup-llm` keeps its native `ollama pull` subprocess**
  (native progress bars + auto-starts a stopped server — the HTTP path can't) — the one deliberate
  non-DRY: the surfaces have genuinely different needs.

## Load-bearing design decisions (do not regress)

- **Surfaced, never gated.** The scheduler-ready gate stays fact-bank-only
  (`scheduler_ready = fact_bank_path.exists()`, cli/main.py); `OnboardingStatus.is_complete` is
  unchanged. Models/browser readiness is shown, never a hard block. The AI-engine wizard step is
  no-gate (`flagMap['ai-engine'] = null`); a **fresh** profile (no contact saved) lands on it first
  (`_isFreshProfile()`), returning users reach it via the dashboard card.
- **HTTP `/api/pull`, not subprocess-scraping**, for the web — structured per-layer progress, no
  console parsing, consistent with the app's existing Ollama HTTP usage.
- **`check_browser` is WARN-only and never launches a browser** (doctor's read-only contract).
- Background jobs use the **proven seed-boards pattern** (module dict + `asyncio.to_thread` +
  in-thread GIL-safe `dict.update`); single writer per action, readers copy via `dict(...)`.

## Tests (19 new)

`tests/test_doctor_browser.py` (4 — WARN/PASS matrix via patched find_spec/cache/channel),
`tests/test_setup_ops.py` (8 — pull happy/server-down/error-line, install success/fallback/both-fail,
readiness, ensure_data_dirs), `tests/test_web_setup.py` (7 — readiness endpoint, both worker jobs,
start→status to done, idempotent-while-running, unknown-action 404).

## Verified

`av3 doctor` reports the browser check (8 checks). Live TestClient: `/api/setup/readiness` returns
the 2 checks, `/onboarding` + `/` render with the new UI present. On the dev box (models + Chrome
present) everything is PASS, so the card stays hidden — the WARN/running/done paths are covered by
the unit/integration tests. **Not yet exercised:** a true cold machine (no Ollama/models) walking
the in-app pull end to end — that's the next real-world validation when a friend installs.

## Real test-pass fixes (2026-06-21) — surfaced by a cold-ish walkthrough, all SHIPPED

A manual install→onboard walkthrough surfaced five issues in the *older* onboarding/goal-chat/dashboard
code (not the Phase-1 work). All fixed; full suite **1342 green** (+15 tests). Browser-verified.

1. **Email step orphaned (pre-existing bug).** `onboarding.js saveTelemetry()` set `step='web-prefs'`,
   skipping the `email` step entirely (the MVP pass added email to STEPS but never wired the
   telemetry→email hop). Fixed → `step='email'`.
2. **Goal-chat asked salary twice.** The scripted `comp` step always fired even if the user stated pay
   in the roles answer. Added `onboarding_chat.scan_salary()` (conservative: needs a `$`/`k`/`m` cue or
   a pay keyword — won't misread "8 years"); `parse_answer` now opportunistically captures it; the route
   **skips the comp step** when `salary_floor` is already known and says so ("Using $82,000 … from earlier").
3. **Goal-chat over-narrowed roles + dropped relocation intent (the "suggest, you confirm" widening).**
   New `suggest_adjacent_roles()` (deterministic curated family map — the 8B model is unreliable at
   expansion, so it's NOT LLM-driven; LLM stays a bounded parser) returns adjacent titles when the roles
   answer is vague/narrow (≤2 titles); the route returns them as `suggestions.roles` and the wizard renders
   tappable **chips** (`addSuggestedRole` → adds to draft titles, never auto-added). `detect_relocation()`
   keeps `onsite_ok=True` and records "open to relocation / visa sponsorship" as a preference (was being
   collapsed to a narrow "country, remote"). `apply_updates` now **accumulates** `preferences` (union) so a
   note added at the location step survives the later priorities step.
4. **Dashboard didn't explain an idle worker.** When the scheduler is Stopped but onboarding IS complete
   (e.g. `--no-scheduler`, or models not ready), the status bar now shows "worker idle — start it with
   `av3 serve` (no `--no-scheduler`)" instead of a bare "Stopped" with no guidance.
5. **`about:blank` Chrome window confusion (durable gotcha).** `serve` with the scheduler on **eagerly**
   starts the bot's headed apply browser (`cli/main.py` `_factory` → `BrowserSession.start()`), so a blank
   real-Chrome window pops at `about:blank` at startup — repeatedly mistaken for "the dashboard is broken."
   It is NOT the dashboard (`serve` never opens the dashboard; only `av3 launch` does). Fixed the *messaging*:
   `serve` now prints the dashboard URL prominently + "a separate Chrome window (the apply browser) will open
   and sit blank — that's normal." **Deferred option:** lazy-start the apply browser (only when an apply runs)
   so plain discovery/scoring testing never pops a blank window — architectural (apply worker would own the
   session lifecycle), not done yet.

**Gotcha for future testers:** to exercise *this session's* code, `av3` must point at the working tree
(`pip install -e ".[v3]"` from the repo) — the GitHub-zip pip install is the old release. And use
`av3 launch` (opens the dashboard), not `av3 serve` (power-user; dashboard not auto-opened, blank apply
browser appears).

## Backlog — deferred from the 2026-06-21 test pass (agreed, not yet built)

1. **Lazy-start the apply browser.** Today `serve` with the scheduler on eagerly starts the headed
   apply browser (`cli/main.py` `_factory`) → a blank `about:blank` Chrome at startup. Make it start
   only when an apply actually runs, so plain discovery/scoring testing never pops a blank window.
   Architectural (the apply worker would own the `BrowserSession` lifecycle; re-verify the §4/M headed
   login-on-demand launcher reach + teardown). Messaging band-aid already shipped.
2. **Commit the 2026-06-21 batch** (Phase 1 setup restructure + the 5 test-pass fixes). Commit-only,
   not yet done (owner's commit-only cadence; owner chose to keep testing first).
3. **Assisted queue needs inline instructions.** The "Assisted queue" card (dashboard.html ~128) shows
   three groups (Ready to finish / Sign-in needed / Needs your decision) with action buttons but NO
   explanation of the workflow — a non-technical user can't tell that "Ready to finish" = Open → submit
   in the browser → "I submitted it", vs. "Mark applied" = I applied myself. Add a one-line helper under
   each group head (and clarify that "in flight" jobs are the bot's automatic work, not user actions).
   Same class of gap as the dashboard-clarity fix (#5) and the AI-engine readiness panel.

## Apply-quality "clear wins" batch (SHIPPED 2026-06-21) — from the same test pass

The owner watched a real `--dry-run` apply and reported: UUID artifact filenames, almost-empty form
fills, "Country of Residence = Texas", the same jobs reprocessed every cycle, and an assisted queue
that's a dead-end tag. Three Explore probes traced each root cause; the owner approved a clear-wins batch
+ adding the fail-closed fields to onboarding. All shipped (plan: `~/.claude/plans/nifty-churning-pillow.md`).

- **A — "Country = Texas" (bug).** `answer_resolver._split_location` read 2-part "Dallas, TX" as
  `(city, country=TX)`. Fixed: US-state set (`_US_STATE_ABBR`/`_NAMES` + `_is_us_state`); "City, ST" →
  `(city, ST, "United States")`. Genuine "City, Country" untouched.
- **B — salary blank (bug).** `apply_worker._apply_salary_ask` read `settings.salary.floor`, but onboarding
  writes `targeting.salary_floor`. Fixed: fall back to `targeting.salary_floor`.
- **C — UUID filenames.** `generate.generated_resume_path`/`_cover_letter_path` now build a readable
  `{Name}_{Resume|Cover}_{Company}_{Title}_{id8}` stem, derived INTERNALLY from the job (app.db) + applicant
  (master.json) with a bare-`job_id` fallback — so the signature is unchanged and all ~20 callers/tests
  stay green + deterministic. Cover keeps its legacy `_cover` fallback name.
- **D — dry-run re-apply loop.** Dry-run never transitions a job (stays QUEUED_APPLY) → re-picked every
  cycle. Fixed: `ApplyWorker._dry_run_tested_job_ids` (in-memory, the worker persists across cycles); skip
  in `run_once` BEFORE `_process_one` so the `@stage` wrapper doesn't even fire. Per-session (resets on
  restart). Existing `test_dry_run_leaves_job_in_queued_apply` still holds.
- **F — fail-closed fields → onboarding.** New `FactBank.primary_nationality` / `notice_period` (+ gender via
  the free-form `eeo` dict) + the dropped `relocation` serialization fixed; `merge_extras` + `POST
  /api/onboarding/extras` + a new optional **"More details (optional)"** wizard step. Resolver: new
  `ProfileField.NATIONALITY`/`NOTICE_PERIOD`/`YEARS_EXPERIENCE` (`_compute_years_experience` from work
  history) — but in `_OPTIONAL_PROFILE_EXTRAS` they **fall THROUGH** to the bank/LLM tiers when the bank
  can't answer (so they don't hijack seeded/LLM answers). **Gender stays fail-closed** ("prefer not to
  answer") unless the user provides it — honesty invariant intact.
- **E1 — assisted human-takeover.** New `POST /api/jobs/{id}/open` opens ANY review job's listing in the
  bot's headed Chrome (no ASSISTED_PENDING needed); "Open in browser" added to the decide + login groups,
  plus a one-line **helper under each assisted-group head** (closes the "no instructions" gap the owner hit).

**Durable gotcha:** there are TWO salary configs — `settings.salary.floor` (the apply salary-answer) vs
`settings.targeting.salary_floor` (the discovery filter). Onboarding only writes the latter.

### Deferred (agreed) — E2: on-demand "fill what it can"
The owner wanted a button that opens the listing AND fills what it can. `assisted/open` requires a
pre-filled `ASSISTED_PENDING` attempt; "decide" jobs have none. E2 = a new `POST /api/jobs/{id}/assisted/
prepare` that runs a single-job assisted prepare (a one-off `ApplyWorker(mode=assisted)` from web_state +
the headed launcher's `new_page`, transitioning REVIEW→QUEUED_APPLY, halting as ASSISTED_PENDING). Complex
+ risky (real browser apply trigger, state + artifact handling), so deferred from this batch — E1 + the
existing "Open the application" (submit group) cover open-in-browser meanwhile.

## Live-test issue fixes — SESSION 2 (SHIPPED 2026-06-21, after the handoff `%TEMP%\handoff-...T1822Z.md`)

A `/clear` + handoff followed live dashboard testing. This session fixed the 2 confirmed regressions,
acted on an owner honesty directive, and produced the E2 design. Full suite **1382 green** (+18 tests).
**Commit-only, not pushed** (owner cadence; `av3` editable = working tree live). Files: `resume/generate.py`,
`pipeline/apply_worker.py`, `resume/answer_resolver.py`, `pipeline/scheduler.py`, `web/control.py`,
`web/headed.py`, `cli/main.py` + tests.

### #1 — artifact "missing résumé/cover" regression (the readable-filename orphan) — FIXED
Root cause as the handoff predicted: fix-C's readable names (`{Name}_Resume_{Company}_{Title}_{id8}.pdf`)
meant `generated_resume_path(...).exists()` returned False for any artifact written under the OLD bare
`{job_id}.pdf`, so `apply_worker._artifacts_for` fell through to the (absent) global `resume.pdf`.
**Fix:** new `resolve_generated_resume` / `resolve_generated_cover_letter` in `generate.py` (exported),
used by `_artifacts_for`. Resolution order: **readable (preferred) → legacy bare `{job_id}.pdf` /
`{job_id}_cover.txt` → a glob on the deterministic `_{job_id[:8]}` suffix** (adopted only when
unambiguous). The id8 glob also covers the latent determinism risk (master.json `contact.name` changing
between optimize-write and apply-read) — the legacy-bare check alone does NOT (it's a different readable
name, not the bare id). Tests: `test_picks_up_legacy_bare_named_artifacts`,
`test_resolve_generated_prefers_readable_then_legacy_then_id8`.

### #3 — "How did you hear about this job" — DERIVE HONESTLY FROM THE DISCOVERY SOURCE (owner directive)
Two-round directive. Round 1: *"I'll never know how I heard since you do all the searching, so a seeded
answer is impossible"* (→ don't seed a static "LinkedIn"). Round 2 (the key correction): *"that's not a
better design — how am I supposed to answer it either?"* — i.e. **routing it to the human is ALSO wrong; the
human can't answer it any better than the bot.** The resolution: there IS a truthful per-job answer — the
**discovery source is stored on the job row** (`job.source`). ATS adapters (greenhouse/lever/ashby) query
the company's OWN application portal; jobspy stores the actual board (`indeed`/`linkedin`/`zip_recruiter`/…)
as the source. So how-heard is **derived honestly, per-job, auto-filled — no seed, no human guessing.**

`_resolve_how_heard` now (answer_resolver.py): (1) still honours a value the user EXPLICITLY banked
(override); (2) `_how_heard_source_label(job.source)` → ATS ⇒ **"Company Website"** (owner-chosen label,
2026-06-21), known boards ⇒ their name, unknown board ⇒ title-cased site; (3) for a **dropdown**,
`_match_how_heard_option` matches the label to an offered option (company/employer/career-site synonyms for
"Company Website"), falls back to **"Other"**, else bails; for **free text**, fills the label; (4) bails only
when there's no `job.source` (resolver used outside the apply loop) or the form lacks our channel + no
"Other". New `ResolutionSource.DISCOVERY` for honest provenance. Still intercepted BEFORE the bank/LLM/essay
tiers so the LLM can never fabricate one. Tests (7): derive-ATS / derive-board / dropdown-match /
other-fallback / bail-no-option / bail-no-source / never-invokes-LLM, + explicit-bank-override.
**Takeaways for future sessions: (a) do NOT seed how-heard; (b) do NOT route it to the human — derive it
from `job.source`; (c) "Company Website" is the owner-approved label for ATS-portal discoveries.**

### #3 — config-gap diagnosis (the other test-dir blanks) = CONFIG, not code
The handoff's `@what-actually-works` already proved nationality/gender/notice/years fill correctly from a
COMPLETE bank (they fall THROUGH to bank/LLM tiers). The test-dir blanks were the fresh dir lacking the
optional "More details" wizard step + a real fact bank — **config, not a regression.** No code change beyond
how-heard. (Owner's production dir `C:\Users\jar85\JobSearch` is fully configured → "worked for just me.")

### #2 — scheduler apply-worker vs manual "Open in browser" CLASH — FIXED (apply-only auto-pause)
Root cause: `_launcher_new_page` opens a tab in the SAME persistent Chrome context the scheduler's apply
worker drives, so the apply worker keeps opening/navigating tabs and steals focus while the human works.
**Owner chose:** pause ONLY the apply stage during a manual takeover, auto-engage on open, auto-release on
tab close + safety timeout (gather stages keep running). **Built:**
- `web/control.py` `ManualTakeover` — token-counted active-takeover tracker; `engage()`/`release(token)`/
  `is_active()`; a `_TAKEOVER_TIMEOUT_S = 900s` safety window auto-prunes a takeover whose tab-close was
  never seen (apply can never wedge). Injectable `now` for tests.
- `pipeline/scheduler.py` — new `apply_gate: Callable[[],bool]` param (apply analog of quiet hours) +
  `CycleSummary.apply_skipped_takeover`. Apply stage now skipped if `quiet_hours OR apply_gate()`
  (quiet-first, so the badge is unambiguous). Gather stages always run.
- `web/headed.py` — `HeadedBrowserLauncher(takeover=...)`; on a bot-browser open it `engage()`s and binds
  `page.on("close", release)`. Best-effort: a page with no `.on` still engages (timeout releases it).
- `cli/main.py serve` — one `ManualTakeover` at function scope, passed to the launcher AND
  `apply_gate=_takeover.is_active` into the scheduler factory (both gated on `service is not None`).
Tests (8): `TestManualTakeover` (5 — engage/release/idempotent/multi/timeout), scheduler
`test_manual_takeover_skips_apply_only` + `test_no_takeover_apply_runs` +
`test_quiet_hours_takes_precedence_over_takeover_flag`, launcher
`test_manual_takeover_engaged_on_open_released_on_close` + `test_takeover_engaged_even_when_page_has_no_close_hook`.
**Deferred (small):** a dashboard "apply paused — you're in the browser" badge. `apply_skipped_takeover` is
in `CycleSummary` but not plumbed to the status API; owner chose auto + auto-release so it's silent for now.

### #4 — "no gray note" on Open-in-browser — wiring intact, LIVE-VERIFY pending
The per-row `reviewNote[j.id]` (dashboard.html) + the launcher's `LaunchResult.note` are untouched by the
#2 change (the return shape is unchanged; #2 only adds takeover registration on the success path). The
handoff judged the missing note "likely disrupted by the clash (#2)" — so it should reappear now the clash
is fixed. **This is a live step the owner verifies** (no automated repro for a browser-focus race).

### E2 — DESIGN DOC produced (owner chose design-first), NOT built
Full spec: [e2-on-demand-fill-design.md](e2-on-demand-fill-design.md). Recommends borrowing the scheduler's
ApplyWorker via a new `prepare_single(job_id, page)` (assisted-only, never dry, single job) reached through
a worker holder, behind `POST /api/jobs/{id}/assisted/prepare`; promotes a "Needs your decision" job into
the existing "Ready to finish" lane (REVIEW→QUEUED_APPLY→APPLYING→REVIEW + ASSISTED_PENDING row). Depends on
#2's takeover gate (already shipped) for clash-free operation. 3 open questions for the owner in the doc.

## What's next (Phase 2, still deferred)

Embedded-Python + Inno installer (needs Inno Setup 6 on the build host) — see
[exe-distribution-viability.md](exe-distribution-viability.md) §"Recommended phasing". Owner
decisions still open there: embedded-Python vs PyInstaller-onedir; install Inno Setup 6; unsigned
SmartScreen "Run anyway" posture.
