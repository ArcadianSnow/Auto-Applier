# Batched assisted review — the "In Progress" checkpoint (design)

**Status:** ✅ **ALL 4 PHASES SHIPPED 2026-06-24** (1483 green; phases 3 + 4 live-verified in a real browser
via Playwright). Built from a friction discussion with the owner; the four shaping forks below were all
decided up front. Supersedes the single-job direction in `e2-on-demand-fill-design.md` (that doc's per-job
"prepare" became one primitive — `build_proposed_application` / `prepare`-time persistence — inside this
batched flow). See the "Phase N — SHIPPED" sections below. Remaining (non-blocking) follow-ups: durable/DB
batch state (barrier is in-memory), retention for `artifacts/proposed/*.json`, and `needs-work` re-prepare
(today it's a side-lane).

## The friction this fixes

Today the apply worker prepares jobs as fast as it can and the owner can't keep up — there's no moment to
work on missed fields or verify what was filled. Critically, **the applier never clicks submit anyway**, so
every job is already "bot prepares, human submits" — but with no surface to do that on and no pause, so the
owner just has to *assume* it's going right and has zero in-flight visibility/control.

## The shape (owner decisions, 2026-06-24)

A batched human-in-the-loop checkpoint. The apply stage prepares a **batch of N (default 5)** jobs, then
**pauses** and surfaces an **"In Progress" page** with one tab per job. The owner verifies / corrects /
submits each, marks its disposition, releases the batch, and the worker prepares the next N.

Owner's four decisions (from the AskUserQuestion round):

1. **Scope = EVERY job.** The applier doesn't hit submit, so nothing is ever auto-submitted; every prepared
   job waits on the page. This is *the* apply mode in the dashboard, not a special case for messy forms.
2. **Filling split = the browser gets the confident set; the PAGE is COMPLETE.** The bot fills the live form
   with everything it is confident in — deterministic fills *and* audited-inferred answers, exactly as
   today. The "In Progress" page goes further: **every field is filled in completely**, including full drafts
   for the fields the bot left blank in the live form (open-ended / fabrication-risk ones). The page is the
   *complete proposed application*; the live form is the *confident subset*.
3. **Disposition = per-job + release.** Each job is marked applied / skip / needs-more-work; then the batch
   is released and the next N prepare.
4. **Drafting = draft everything; owner verifies.** Even content the honesty guard blocks from AUTO-submit
   gets a full first draft ON THE PAGE — because the owner is the gate (verify/correct before it's used).

## Why this is mostly reuse (verified against the tree 2026-06-24)

- **`draft_freeform` DRAFT path is real** (`answer_resolver._draft_open_ended`, `ResolutionSource.DRAFT`,
  `any_drafted`). The copilot already writes open-ended answers, flags them unverified, and forces assisted.
  Today it TYPES the draft into the form; here we **redirect it to the page** and run it for *every* gap
  (decision 4's aggressive mode), not just when an owner flag is on.
- **The scheduler already has an `apply_gate`** (`pipeline/scheduler.py` — `apply_gate: Callable[[], bool]`,
  consulted before the apply stage; gather stages keep running when it's set). The **batch barrier is exactly
  this gate**: while a batch awaits review, `apply_gate()` returns True → apply paused, discovery/score/
  optimize keep flowing. Bonus: the worker being idle during review sidesteps the worker-vs-manual browser
  clash we have today.
- **Assisted mode + `ASSISTED_PENDING` → REVIEW** is the existing "bot fills, human finishes" lane
  (`apply_worker`, `domain/state`).
- **Human-attested APPLIED** already exists (manual-apply mode: a human marks a job applied → APPLIED, which
  keeps dedup honest). The per-job "applied" button reuses that semantics.
- **Pasteable plain text + voice** govern how drafts render: per-field copy buttons copying PLAIN text (no
  markdown — it doesn't paste into web fields, [[feedback_paste_docs_as_docx]]); drafts in the owner's voice
  with no AI tells ([[feedback_writing_voice_no_ai_tells]] — no em-dashes, no "I'm excited to apply", no
  rule-of-three). The copilot draft path already targets this voice.

**Not yet built (don't cite as existing):** `e2-on-demand-fill-design.md`'s `prepare_single` is design-only;
its "borrow the ApplyWorker to prep one job" idea is the per-job primitive this batch orchestrates, but it
has to be built.

## What's genuinely new

1. **The batch barrier** — the apply stage prepares N jobs, then holds on `apply_gate` until the owner
   releases the batch (a control flag the "In Progress" page sets/clears).
2. **The "In Progress" page** — N job tabs, each rendering the COMPLETE proposed application: every field +
   value + source/confidence, the must-verify drafts clearly marked, per-field copy buttons, a per-job
   "open the application" action, per-job disposition controls, and a release-batch button.

## State / data

- A **batch grouping** over jobs that have been prepared (in `ASSISTED_PENDING`, or a new `IN_REVIEW` lane) +
  a batch id; the worker holds at the barrier until every job in the batch is dispositioned.
- **Per-job disposition:** applied → APPLIED (human-attested); skip → SKIPPED; needs-more-work → held/looped.
- The **complete proposed-answer set per job** (confident fills + aggressive drafts) persisted so the page
  renders it and survives a refresh. Local-only; never mirrored to telemetry (same as today's answers/EEO).

## Reliability invariants (all still hold)

- **Bot never submits** → no false APPLIED from the bot; APPLIED is the owner's per-job attestation
  (manual-apply semantics).
- **Fabrication guard** still governs what is TYPED into the live form (the confident subset). The aggressive
  drafts live ONLY on the page and are human-gated, so nothing unverified ever reaches a real submission —
  the guard protects auto-submission, and there is no auto-submission here.
- **Dedup honest** — only jobs the owner marks applied become APPLIED.

## Open items (decide during build, not blockers)

- **Live ATS tabs:** keep N real form tabs open during the pause, or open-per-job on demand from the page?
  Lean **open-on-demand** (avoids N heavy tabs + browser-session pressure; the page is the source of truth,
  the form tab is opened when the owner is ready to paste + submit that one).
- **Batch size** configurable (default 5).
- **`needs-more-work`** behavior: requeue into a later batch vs hold in a side lane.
- **Headless `av3 run`** has no human → this is a **web-dashboard mode**; the headless loop keeps today's
  behavior (or its apply stage is paused while the dashboard owns the batch).

## Suggested build phases (each shippable + testable on its own)

1. **Prep-complete** — extend the apply path to compute & persist the COMPLETE proposed set per job
   (confident fills + aggressive drafts for every gap), WITHOUT changing submit behavior. Reuses
   `draft_freeform` made unconditional + the resolver. Unit-testable with no browser.
2. **Batch barrier** — apply stage prepares N then holds on `apply_gate`; a release flag advances it.
3. **"In Progress" page** — the dashboard view: tabs, complete fields, copy buttons, per-job
   open-application + disposition, release-batch.
4. **Tracking** — per-job applied/skip → state + outcomes; batch advance; surfaces in the funnel.

Phase 1 is the honest place to start (pure prep/draft logic, no UI, fully unit-testable) and de-risks the
rest.

## Phase 1 — SHIPPED (2026-06-24, 1450 green)

Pure prep/draft logic, no UI, browser-free. The apply path now computes + persists the COMPLETE proposed
application per job; **submit behavior is unchanged** (the live form still gets only the confident subset).

**What was built:**

- **`auto_applier/resume/proposed.py`** (new) — the whole Phase 1 deliverable:
  - `ProposedField` — one page row: `{key, label, value, kind, source, confidence, required, needs_verify,
    is_draft, options, note}`. Three dispositions captured by the two booleans: **confident**
    (`needs_verify=False`, the live-form subset), **draft** (`is_draft=True` ⇒ `needs_verify=True`), **needs
    input** (`needs_verify=True, is_draft=False` — sensitive/how-heard/unanswered gaps, never fabricated).
  - `ProposedApplication` — `job_id` + `fields` + `resume_path`/`cover_letter_path` + `built_at`; summary
    counts (`confident`/`drafted`/`needs_verify`); `to_dict`/`from_dict`.
  - `build_proposed_application(...)` — async, browser-free. Takes the **driver's already-computed**
    `questions` + `resolutions` (so the page's confident rows == the live form exactly, decision 2) + 5
    synthesized standard rows (name/email/phone/résumé/cover). For every **non-filling** gap it calls
    `resolver.draft_open_ended(q)` — the unconditional aggressive draft (decision 4).
  - `proposed_path` / `save_proposed` (atomic temp-then-replace) / `load_proposed` (None on missing/corrupt)
    → per-job JSON at **`artifacts/proposed/<job_id>.json`**. Local-only, never mirrored (same as answers/EEO).
- **`AnswerResolver.draft_open_ended(question)`** (new public method) — the "draft made unconditional" gate.
  Ignores `self.draft_freeform` (the owner is the submit gate on the page) but is a STRICT SUPERSET of
  `resolve()`'s open-ended gating, so it can never fabricate a value the resolver would refuse: returns
  `None` for sensitive / how-heard / non-essay questions or when the copilot can't produce text. Wraps the
  existing `_draft_open_ended` (the BUILD-6 copilot draft path) → one source of truth for drafting.
  **Also fixed a latent bug:** `_draft_open_ended`'s except branch logged through an **undefined `logger`**
  (module-level `logging`/`logger` were missing) — a `NameError` waiting on the copilot-error path, which
  Phase 1 exercises far more. Added `import logging` + `logger` to `answer_resolver.py`.
- **`apply_worker._process_one`** — wired in `_persist_proposed(job, outcome, resume_used, cover_used)`
  right after `_log_resolutions`, for **dry-run AND real** runs. Reuses the driver's `outcome.{custom_
  questions,resolutions}` (no double-resolve; `draft_freeform` ON means the driver already drafted → builder
  reuses, no re-draft) and `self._resolver` (with `current_job` still set → drafts use the right company/JD).
  Best-effort: any failure is logged + swallowed (same posture as `_log_resolutions`) — the page artifact
  must never break the apply loop.

**Tests:** `tests/test_proposed_application.py` (20) — builder structure, drafting-unconditional (drafts an
essay gap with `draft_freeform=False`), honesty (sensitive/how-heard/non-essay never drafted),
`resolver=None` skip, no-re-draft of an already-drafted resolution, summary counts, the `draft_open_ended`
gate in isolation (incl. no-LLM + copilot-raise → the logger-fix path), and JSON round-trip / missing /
corrupt / partial-payload. Plus `test_apply_worker.py::test_proposed_application_artifact_is_persisted` (the
worker wiring). 1450 green.

**Deliberately NOT in Phase 1 (later phases / follow-ups):**

- No DB state. The **batch grouping + batch id + per-job disposition** is Phase 2/4 state (a table), distinct
  from the per-job proposed-SET content (this JSON artifact). The builder is decoupled from how resolutions
  are produced, so a future on-demand `prepare_single` (E2) that has questions-but-no-driver-run just calls
  `resolver.resolve_all(questions)` first, then `build_proposed_application`.
- No retention/prune of `artifacts/proposed/*.json` yet (they accumulate per prepared job) — wire into
  `pipeline/retention.py` (or Phase 4 tracking) on a window keyed to REVIEW/disposition.
- Drafting gaps is **serial** per job inside the apply loop (one copilot best-of-two per essay gap), adding
  real LLM latency to a prepared job with several essays. Acceptable for Phase 1; parallelize if it bites.

## Phase 2 — SHIPPED (2026-06-24, 1461 green)

The batch barrier: the apply stage prepares N jobs (default 5), then HOLDS on the scheduler's existing
`apply_gate` until the owner releases — apply pauses, gather stages (discover/filter/score/optimize) keep
running, exactly like a manual takeover or quiet hours. **Default OFF** (`settings.scheduler.batched_review`),
so headless `av3 run` and every existing test keep today's continuous-drain behavior; the dashboard turns it
on. In-memory barrier (mirrors `ManualTakeover`); durable batch grouping + per-job disposition is Phase 4.

**What was built:**

- **`auto_applier/pipeline/review_batch.py`** (new) — `ReviewBatch`: thread-safe (one `Lock`, three touchers
  — worker/scheduler/web). `add(job_id)→bool` (idempotent; returns "now full"), `is_full`/`is_holding`
  (== full-and-unreleased), `release()→new_batch_id` (clears members, opens a fresh batch → lifts the hold),
  `snapshot()` (`{batch_id,size,count,members,holding}` for Phase 3), `size`/`count`/`batch_id` props. Size
  clamped to ≥1 so a bad config can't wedge apply.
- **`SchedulerConfig`** — `batched_review: bool = False` + `batch_review_size: int = 5` (validator: size ≥ 1).
- **`ApplyWorker`** — optional `review_batch` ctor arg (None = no barrier). `run_once`: a **top-of-loop hold
  check** defers the rest + breaks when the batch is already holding (handles fill-mid-run AND `av3 apply
  --once`, which bypasses the scheduler gate); after each handled job (success OR error → both land awaiting
  the owner) `_register_in_batch(job)` adds it. New summary field `deferred_batch`. A *soft* defer like
  session rotation — deferred jobs stay in QUEUED_APPLY.
- **`cli serve`** — builds ONE `ReviewBatch` when `batched_review` is on (web-dashboard mode only — the
  headless `run` command never wires it), composes `apply_gate = _takeover.is_active() or
  _review_batch.is_holding()`, passes `review_batch=` to the apply worker, and stashes it on `WebState`
  (new optional `review_batch` field) so Phase 3's page can reach `snapshot()` + `release()`.

**Tests:** `tests/test_review_batch.py` (7) — fill→hold, under-N no-hold, idempotent membership, blank-id
ignore, release→fresh-empty+new-id, size clamp, snapshot shape. `tests/test_apply_worker.py` (+4) —
holds-after-N (`deferred_batch`), resumes-after-release (re-prepares the deferred job), no-batch-drains-all,
self-defers-when-already-holding. 1461 green.

**Deliberately NOT in Phase 2 (Phase 3/4 / follow-ups):**

- **No HTTP route / UI.** `release()` exists + is wired, but the "release" button + the snapshot/release
  endpoints are Phase 3 (the "In Progress" page). Until then, enabling `batched_review` would wedge apply
  after the first batch with no way to release — hence default OFF.
- **No durable/DB batch state.** The barrier is in-memory (restart starts empty; prepared jobs sit in REVIEW
  for the owner). Durable batch grouping + per-job disposition (applied/skip/needs-work) + the funnel are
  Phase 4 (Tracking) — the natural home for persistent batch state.
- A FAILED/errored job counts toward the batch (it lands in REVIEW awaiting the owner too) so the barrier
  can't churn through many failing jobs without pausing; Phase 3 decides whether such a job renders a page tab.

## Phase 3 — SHIPPED (2026-06-24, 1470 green + live-verified)

The "In Progress" page: one tab per prepared job, each rendering the COMPLETE proposed application, with
per-field plain-text copy buttons, an open-the-application action, per-job disposition, and a release-batch
button. Live-verified end-to-end in a real browser (Playwright MCP) on a seeded 2-job batch — tabs, the
draft badge, the EEO "needs your input" gap, copy buttons, tab-switch, and release (count→0, empty state)
all render correctly; console clean.

**What was built:**

- **`web/routes.py`** — two new API routes + one page:
  - `GET /api/batch` — the page feed: `{enabled, batch: snapshot, jobs:[{job_brief, assisted_application_id,
    proposed}]}`. `proposed` = `ProposedApplication.to_dict()` + `summary()` (Phase 1's per-job artifact via
    `load_proposed`), or `null` when none saved. `enabled:false` when batching is off. Skips members whose
    job row is gone.
  - `POST /api/batch/release` — calls `review_batch.release()`; 409 when batching is off. Returns the fresh
    snapshot.
  - `GET /in-progress` (pages_router) — the page shell.
  - `GET /api/status` now also carries `review_batch` (snapshot or `null`) for a discoverability badge.
- **`web/templates/in_progress.html`** (new) — standalone Alpine page (mirrors `reconcile.html`): batch
  status + release button; a tab strip (one chip per job, with state pill + "N to check"); a detail panel
  rendering each `ProposedField` (value in a `<pre>` for drafts, source + confidence, a Copy button that
  writes PLAIN text via `navigator.clipboard`; drafts get a "draft — review before sending" badge, gaps a
  "needs your input" badge + the bail note); per-job **Open the application** (prefers `/assisted/open` when
  there's a pending attempt, else `/jobs/{id}/open`), **I applied** (`/jobs/{id}/mark-applied`), **Skip**
  (`/jobs/{id}/skip`). Gentle 8s auto-refresh that preserves the selected tab. All `selected.*` bindings use
  optional chaining (kills the Alpine teardown-tick warnings when the batch empties after release).
- **`web/state.py`** — `WebState.review_batch` was added in Phase 2; the routes read it via `getattr`.
- **`templates/base.html`** — an "In Progress" nav link. **`static/app.css`** — `.ip-*` styles (tabs, field
  rows, draft/gap/required badges, copy button), tokenized + dark-mode-safe.

**Reuse (no new disposition code):** the page's disposition buttons call the EXISTING human-attested APPLIED
(`mark_manually_applied`, allowed from DECIDED/REVIEW) + skip (REVIEW→SKIPPED) + open endpoints — so the
state-machine semantics match `av3 applied` / the assisted queue exactly. The genuinely-new wiring is the
two `/api/batch*` routes + the page.

**Tests:** `tests/test_web_in_progress.py` (9) — feed disabled/enabled, members + proposed payload + summary,
`proposed:null` for an unsaved member, missing-job skip, `assisted_application_id` passthrough, release
409-when-off, release clears + new id + empty feed, status badge present/null, page shell renders. Plus a
node `--check` of the inline JS + the live Playwright pass. 1470 green.

**Deliberately NOT in Phase 3 (Phase 4):**

- **"needs-work" disposition** — deferred with its open item (requeue into a later batch vs hold in a side
  lane). Phase 3 ships applied + skip (the two unambiguous ones) + release.
- **Disposition → batch advance.** Release is an explicit owner button; auto-advance when all N are
  dispositioned (+ removing a dispositioned job from the batch) is Phase 4.
- **Funnel surfacing + durable batch state.** Phase 4 (Tracking).
- **Operating-mode note:** the disposition endpoints need the job in REVIEW (real assisted mode). In dry-run
  the prepared jobs stay QUEUED_APPLY, so applied/skip will 409 (the page surfaces the error). Batched review
  is meant to run assisted; this is an operating-mode reality, surfaced honestly, not a Phase 3 bug.

## Phase 4 — SHIPPED (2026-06-24, 1483 green + live-verified)

Per-job disposition + batch advance + funnel surfacing. The owner marks each prepared job applied / skipped /
needs-work; once all are dispositioned the hold lifts and the apply worker **auto-advances** (releases the
spent batch, prepares the next N). Live-verified in the browser: marking one job "I applied" + another "Needs
work" showed the per-tab badges (applied / needs work), the "N dealt with" counter, and the status flipping
to "ready for the next batch"; console clean.

**What was built:**

- **`pipeline/review_batch.py`** — `ReviewBatch` now tracks per-member disposition (`_members: dict[job_id →
  disposition]`, default `pending`): `dispose(job_id, disposition)` (validates against `DISPOSITIONS =
  {applied, skipped, needs_work}`; no-op for non-members; returns "now all dispositioned"); `all_dispositioned()`;
  `pending` count; **`is_holding()` is now `is_full AND not all_dispositioned`** (a fully-dealt-with batch
  stops holding); `add` no longer resets an existing disposition; `snapshot()` gains `dispositions` / `pending`
  / `all_dispositioned`.
- **`ApplyWorker.run_once`** — auto-advance: before pulling the queue, `if review_batch.all_dispositioned():
  review_batch.release()` → the spent batch is cleared and this run prepares the next N. (The hold already
  lifted, which is why the scheduler let the apply stage run.)
- **`web/routes.py`** — the existing `mark-applied` / `skip` endpoints now also `_dispose_batch(...)` the
  member (`applied` / `skipped`; no-op for non-batch jobs — so review-queue actions stay safe); new
  **`POST /jobs/{id}/needs-work`** (404 no job · 409 batching-off · else dispose `needs_work`, **state
  unchanged** = side-lane); the `/api/batch` feed adds each job's `disposition`.
- **`web/templates/in_progress.html`** — per-tab disposition badge (`applied` / `skipped` / `needs work`),
  a **Needs work** button, a disposed indicator + disabled actions on dealt-with jobs, and the status line's
  "N dealt with" + "ready for the next batch". **`app.css`** `.ip-pill-done`.

**Funnel:** no new funnel code needed — a batch job marked applied walks to APPLIED via the existing
human-attested path, so it already flows into the outcomes funnel (`av3 analytics` / `/api/outcomes`). The new
visible surface is the batch's own progress (the In-Progress status + the `/api/status` `review_batch` badge).

**Tests:** `tests/test_review_batch.py` (+6: dispose/all_dispositioned/holding-flip, unknown-value raise,
non-member no-op, add-doesn't-reset, empty, partial-advance), `tests/test_apply_worker.py` (+1: auto-advance),
`tests/test_web_in_progress.py` (+6: mark-applied/skip dispose, needs-work happy/404/409, feed disposition).
Plus the live Playwright pass. 1483 green.

**`needs-work` decision (the open item):** SIDE-LANE, not requeue. It unblocks the batch + leaves the job in
REVIEW (visible in the normal review queue) for the owner to revisit. Re-prepare-on-needs-work is a future
enhancement (would tie into the E2 on-demand `prepare_single`).

**Remaining (non-blocking) follow-ups:** durable/DB batch state (in-memory today — a restart starts the
barrier empty; prepared jobs sit in REVIEW, no loss); retention for `artifacts/proposed/*.json`
(`pipeline/retention.py`); parallelizing per-gap drafting if latency bites; Lever/Ashby live smoke.
