# Batched assisted review — the "In Progress" checkpoint (design)

**Status:** DESIGN (2026-06-24, from a friction discussion with the owner). **No code yet.** Supersedes the
single-job direction in `e2-on-demand-fill-design.md` (that doc's per-job "prepare" becomes one primitive
inside this batched flow). Owner answered the four shaping forks below; the rest are build-time details.

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
