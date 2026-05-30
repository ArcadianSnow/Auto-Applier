# Phase 6 — v3.1 (after core proves out)

Knowledge base for the **v3.1** workstreams (spec §11b Phase 6 + the §11 scope-split table). v3.0-core
(phases 0–5) shipped 2026-05-29 at `v3@3fab3f9`, 612 tests green. Phase 6 has **several independent
sub-phases with no mandated order**:

- per-job résumé-path rewire (a Phase-3 carry-over swept up first — see (1/M) below)
- configurable Pareto strategy profiles (§8a)
- salary intelligence + BLS OES market data (§8d)
- outcome feedback loop (§8e)
- interactive batch skill-reconciliation (§7b)
- story bank + company research + rich analytics / what-to-learn trends
- branded UI polish (frontend-design skill)

Each sub-phase is its own slice: build → tests green → record rationale here → update spec/memory.

---

## (1/M) — Per-job résumé-path rewire (2026-05-30)

**What & why.** Closes the oldest open carry-over: the apply worker shipped the single global
`artifacts/resume.pdf` for *every* job, ignoring the per-job tailored résumé the optimize+Strict gate
(`optimize_worker`, spec §7 #6) generates. That made the whole "generate a tailored résumé per job from the
fact bank" pipeline cosmetic at the apply step — the auto-apply path uploaded a generic résumé. This is a
real correctness defect, so it leads Phase 6 before any net-new v3.1 feature.

**The contract (already established in Phase 3, now honoured).** The optimize worker writes two artifacts
keyed by `job.id`, via helpers in `av3/resume/generate.py`:

- `generated_resume_path(settings, job_id)`   → `artifacts_dir/generated/{job_id}.pdf`
- `generated_cover_letter_path(settings, job_id)` → `artifacts_dir/generated/{job_id}_cover.txt`

**File existence IS the durable "this job was optimized" contract** — there is deliberately NO DB column for
the path (decided in Phase 3 (3/M)). So the apply worker derives the SAME paths from `job.id` and reads them;
no hand-off table, no migration.

**Implementation** (`av3/pipeline/apply_worker.py`):

- New `ApplyWorker._artifacts_for(job) -> (resume_path, cover_path)`:
  - résumé = the per-job PDF when it exists on disk, **else** the global `resume.pdf` the worker was
    constructed with (`self._resume_path`).
  - cover = the per-job `.txt` when it exists, else `""`.
- `_process_one` calls it once and threads `resume_used` into `driver.prepare(...)` (replacing the old
  blanket `self._resume_path`) and writes both `generated_resume_path` + `cover_letter_path` onto the
  `Application` row.
- `_recover_job_to_review` (the per-job exception → FAILED→REVIEW path) uses the same helper so a FAILED
  attempt records which résumé it *would* have used (dashboard triage parity with the success path).

**Why keep the global `resume.pdf` as a fallback (not remove it).** A job can reach `QUEUED_APPLY` without a
per-job PDF: a crash-swept APPLYING leftover, a manual re-queue, or a job queued before optimize ran. The
fallback guarantees the apply step always has *a* résumé to upload rather than crashing. The CLI pre-flight
(`av3 apply` / `av3 run`) still requires `resume.pdf` for exactly this reason; only the comments/fix-hints
were updated to call it the fallback (it is no longer the primary).

**Why this is safe / backward-compatible.** The constructor signature is unchanged (`resume_path=` stays the
fallback). Existing worker tests pass `resume_path="/tmp/resume.pdf"` and never write per-job files, so they
fall through to the fallback exactly as before — no existing assertion changed.

**Tests** (`tests_v3/test_apply_worker.py`, +4):
- `test_uses_per_job_generated_resume_when_present` — writes the per-job PDF, asserts the driver received it
  AND the Application row records it.
- `test_falls_back_to_global_resume_when_no_per_job_pdf` — no per-job file → driver + row get the global path;
  cover path is `""`.
- `test_records_per_job_cover_letter_path_when_present` — per-job `.txt` present → recorded on the row.
- `test_failed_recovery_records_per_job_resume_path` — driver crash → FAILED row carries the per-job path.

**Not in this sub-phase.** Whether the apply *drivers* paste the cover-letter `.txt` into a form textarea is
separate (drivers currently take only `resume_path`; cover-letter field-fill is a future driver concern). This
sub-phase makes the worker *resolve + record* the right artifacts; the résumé upload is wired end-to-end, the
cover-letter is recorded on the row for the dashboard.

**Validation:** full v3 suite green (612 → 616), 11 deselected by design.

---

## (4/M) — Outcome feedback loop §8e (2026-05-30)

**What.** The "gets smarter over time" loop, shipped as **record + read-only insights + advisory nudges** —
NOT auto-tuning. Greenfield (no outcome recording existed).

- **Domain:** `OutcomeKind` enum in `domain/state.py` (ghost/rejection/response/interview/offer), funnel-
  ranked (`.rank`, `.is_positive`); `Outcome` dataclass in `domain/models.py`.
- **Storage:** `outcomes` table (job_id FK, kind, noted_at, note) + `OutcomeRepo` (add, list_by_job, list_all,
  count_by_kind, `applied_with_outcomes` = the APPLIED-jobs ⟕ scores ⟕ outcomes join feed). Schema is
  `CREATE TABLE IF NOT EXISTS` so `init_app_db` picks it up idempotently — no migration needed.
- **`av3/analytics.py`** (pure, no I/O): `compute_conversion_report(feed)` collapses to one record per job
  (furthest-reached outcome wins), buckets conversion by source / title / score-band; a silent APPLIED job
  (no outcome) counts as applied-not-converted + implicit-ghost (honest denominator).
  `recommend_weight_nudges(report)` is **advisory only** — gated behind `MIN_SAMPLES_FOR_NUDGE=20`, fires on a
  ≥10pp high-vs-low band conversion gap, returns a `WeightNudge` suggestion (never mutates config).
- **CLI:** `av3 outcome <job_id> <kind>` records (warns if job isn't APPLIED but still records); `av3 analytics
  [--json]` renders the report + nudges.

**Decision: surface, don't auto-apply (Rule 2.6).** Recording outcomes + computing analytics is *gather*
(read-only, safe). Auto-mutating `settings.scoring.weights` from early sparse data is an *act* that compounds
(bad weights → worse applies → worse data). So the loop stops at a **recommendation**; the user applies it by
editing `user_config.json`. This matches §8e "bounded auto-tuning" honestly for v3.1 data volumes. A real
per-axis regression (which of the 7 axes predicts conversion) needs far more data — explicitly out of scope.

**Backward-compat.** New table + new commands only; no existing code path changed. All prior tests untouched.

**Tests.** `test_analytics.py` (OutcomeKind ranking, OutcomeRepo on a real tmp DB incl. silent-job feed +
non-applied exclusion, pure report aggregation: furthest-wins / silent-counts / by-source / by-band /
outcome-counts, nudge thresholds + min-samples gate). `test_cli_outcome.py` (record / unknown-job exit 2 /
non-applied warning / bad-kind reject / analytics empty + conversion + json). Full suite **692 green**.

**Anti-stuck note.** Two edits silently failed (comment dash-count + `db/__init__.py` shape differed from
the guessed `old_string`) — the `OutcomeRepo` class and its export never landed on the first pass, surfacing
as an ImportError at collection (not the stale exit-0 notification). Fix per Rule 1: re-Read exact anchors,
re-edit; then a quick `python -c "import …"` smoke + grep-for-duplicate-defs BEFORE the test run caught that
both were now singular. Verify imports structurally before running the suite when an edit batch touches
`__init__` exports.

---

## (3/M) — Salary intelligence §8d (2026-05-30)

**What.** `av3/resume/salary.py` (pure logic, no I/O): `SalaryRange`, `SalaryRecommendation`,
`recommend_ask` (priority **posted → market → user**; floor = hard lower bound; never overshoot posted
ceiling), `parse_posted_range` (tolerant of `$`/`k`/`,`/`-`/`–`/`to`; rejects sub-1000 non-annual noise),
`is_below_floor` (comp-filter gate), `format_ask`, plus a pluggable `MarketDataSource` Protocol +
`NoMarketData` default + `build_market_source(name)` factory.

**Decision: market data is local-first / default-OFF.** Project hard rule = zero net egress except opt-in
telemetry. So `market_source="none"` → `NoMarketData` (returns `None`); recommendation math is 100% local on
posted-range + user-range. The spec's "default BLS OES" is reframed as an **opt-in adapter** the user wires
in `salary.market_source` (accepting egress) — `build_market_source` falls back to `NoMarketData` for any
unknown/unimplemented name (fail-safe: never silently start network calls). Spec §8d updated to match.

**Wiring.**
- `SalaryConfig{floor, ceiling, market_source="none"}` on `Settings.salary` (validates floor ≤ ceiling);
  re-exported from `av3.config`.
- **Apply worker** builds the market source once; `_apply_salary_ask(job)` computes the per-job ask from
  `{config floor/ceiling, parse_posted_range(job.compensation), market}` and sets
  `resolver.salary_expectation` before each job's questions resolve (safe — jobs processed sequentially).
  `None` recommendation → `""` → resolver bails salary Qs to REVIEW (unchanged v3.0 behaviour when nothing
  configured). The market source is only *queried* when `market_source != none`.
- **Score worker** runs `is_below_floor(parse_posted_range(job.compensation), salary.floor)` BEFORE the LLM
  call → below-floor jobs skip to terminal SKIPPED (`comp_skipped` counter, new `_skip_below_comp` four-edge
  walk matching the other skip paths). Saves the expensive LLM scoring + downstream generation on a job the
  user wouldn't accept. No posted range or no floor → proceed (spec §8d).
- CLI: `av3 score` summary gained `comp_skipped=N`.

**Backward-compat.** All new config defaults to None/"none" → comp-filter inert, salary ask empty → exactly
v3.0 behaviour. Existing score/apply worker tests untouched.

**Tests.** `tests_v3/test_salary.py` (30: ranges, parser shapes + rejections, recommend posted/market/user
priority + floor/ceiling clamps, comp-filter gate, format, default source). `test_apply_worker.py` (+3 salary
ask: posted-anchor, ceiling fallback, empty). `test_score_worker.py` (+4 comp-filter: skips-below-floor-pre-LLM,
overlap-proceeds, no-posted-proceeds, no-floor-inert). Full suite **668 green**.

**Anti-stuck note.** Three `Edit`s to score_worker.py failed on guessed `old_string` text (docstring/summary
wording differed from memory). Fix per Rule: re-Read the exact lines, then edit — don't retry guesses. Two
edits had already landed referencing not-yet-imported names; the focused run caught it (`parse_posted_range
not defined`, `no attribute comp_skipped`) and the re-Read fixed both. Verified via background run, not the
stale foreground output.

---

## (2/M) — Configurable Pareto strategy profiles §8a (2026-05-30)

**What & why.** Retires v3.0's single fixed pacing point. The spec (§8a) frames pacing as a
**throughput ↔ detection-risk ↔ user-effort** frontier and exposes named **profiles**, each a coherent
point on it: Cautious / Balanced / Aggressive / Custom.

**Module: `av3/config/strategy.py`** (new). Pure config logic, no I/O.
- `StrategyProfile` (str enum: cautious/balanced/aggressive/custom) + `RiskBias` (str enum:
  leans_assisted/balanced/leans_auto). Both `str`-mixin so they round-trip through `user_config.json`.
- `EffectivePacing` (frozen dataclass): the concrete knobs a profile resolves to — `min_delay_s`,
  `max_delay_s`, `daily_target`, `max_per_company_per_day`, `risk_bias`, `profile`.
- `PROFILE_PRESETS`: frozen presets for the three named profiles. **Balanced == the v3.0 `PacingConfig`
  defaults** (60–180s, daily 30, 2/co/day, balanced) — THE backward-compat invariant (a test asserts it).
  Cautious = 120–300s / daily 10 / 1-per-co / leans_assisted. Aggressive = 20–60s / daily 100 / 3-per-co /
  leans_auto.
- `resolve_strategy(settings) -> EffectivePacing`: named profile → its frozen preset (ignores
  `settings.pacing`); `custom` → builds from the hand-set `settings.pacing`. **One** place owns the mapping.

**Config wiring (`av3/config/settings.py`).** New `StrategyConfig{profile: StrategyProfile=BALANCED}` on
`Settings.strategy`. `PacingConfig` gained `risk_bias: RiskBias=BALANCED` (the custom-profile carrier). Both
re-exported from `av3.config`. `settings.py` imports the two enums from `strategy.py` (one-way; `strategy.py`
only imports `Settings` under `TYPE_CHECKING` to avoid a cycle).

**Worker consumption (`av3/pipeline/apply_worker.py`).** `ApplyWorker` resolves `self._pacing =
resolve_strategy(settings)` ONCE at construction and reads it for every knob (no more
`settings.pacing.*` direct reads). Four knobs now profile-driven:
1. **Inter-apply delay** — `self._pacing.{min,max}_delay_s`.
2. **Per-company/day cap** — `self._pacing.max_per_company_per_day`.
3. **Soft daily target** — NEW: at the top of the per-job loop, in a *real* (non-dry) run, if
   `JobRepo.applied_count_on_day()` ≥ `self._pacing.daily_target`, the worker stops INITIATING new applies
   and `break`s, leaving the rest in QUEUED_APPLY (`summary.deferred_daily_target = remaining`). A **soft
   goal, never a hard wall** — no error, no state change, gather stages untouched. Dry-runs never trip it
   (they produce no APPLIED rows). `set_state(APPLIED)` stamps `updated_at=now`, so the count naturally
   includes applies from the current run.
4. **Risk-router bias** — NEW `_effective_mode()`: a `leans_assisted` profile (Cautious) starts every job in
   `BROWSER_ASSISTED` regardless of the constructor's `mode`. This is the *starting* posture ONLY — the
   driver's downgrade-to-assisted on a real detection signal (the safety floor) still fires on top and is
   untouched. `balanced`/`leans_auto` honour the requested mode (distinct enums for a future per-job §8
   router).

**New repo method** `JobRepo.applied_count_on_day(on_day=None)` — all-companies APPLIED count for a UTC day
(vs. `company_applied_count` which is per-company). Same `updated_at` date proxy + UTC default so the two
agree on "today".

**CLI.** `av3 apply` summary line gained `deferred=N` (the soft-target deferral count). No new flags this
sub-phase — profile is config-driven (a `--profile` override + the onboarding selector are a later nicety;
spec §11a notes profile selection appears in onboarding only in v3.1).

**Scope deferred (documented in `strategy.py`).** Two of the five §8a knobs are NOT wired: **concurrency**
(sources in parallel) and **session rotation** (time-box per source then rotate). The v3.0 scheduler drains
stages sequentially and rotation needs per-source session bookkeeping that doesn't exist yet. `EffectivePacing`
deliberately omits fields for them so a half-wired knob can't masquerade as live. They land in a later
sub-phase (needs scheduler-architecture work).

**Backward-compat.** Default profile = Balanced = the old PacingConfig defaults, so a fresh install behaves
byte-for-byte as v3.0. `ApplyWorker.__init__` signature unchanged (`mode=` still the requested posture). All
prior worker tests (which never set a profile) resolve to Balanced and pass untouched.

**Tests.** `tests_v3/test_strategy.py` (+10: defaults/backward-compat invariant, named-preset directions,
resolve-returns-preset, named-ignores-pacing, custom-uses-pacing, config round-trip incl. custom risk_bias).
`tests_v3/test_apply_worker.py` (+5: cautious→assisted, balanced→honours-auto, soft-target defers,
dry-run-ignores-target, aggressive widens per-company cap). Full v3 suite **631 green**, 11 deselected.

**Anti-stuck note (count reconciliation).** Naive `def test_` counting suggested a mismatch vs the suite
total; that proxy ignores parametrization/class methods. Resolution was *provenance* not arithmetic: `git
status` confirmed only the two intended test files changed, so no existing test could be lost — the green
suite is authoritative. Don't reconcile test counts by subtraction; check which files changed.
