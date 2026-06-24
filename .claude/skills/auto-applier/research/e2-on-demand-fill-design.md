# E2 — "Fill what it can on demand"

**Status:** **Phase A SHIPPED 2026-06-24** (`prepare_single` on `ApplyWorker` + 9 unit tests, 1506
green). Phase B (worker holder + route + dashboard button) next; Phase C is the owner-watched live
validation. The 3 open owner questions were resolved with the doc's own recommendations when the
owner said "start work": eligible = REVIEW + QUEUED_APPLY only; résumé-less = fill the rest, human
attaches; outcome surfacing = one-line "filled N / M left" (per-field already in events.db). E2 is
the owner's headline want from the live test pass: a dashboard button that opens a review job's
listing **and pre-fills what the bot can**, leaving the rest for the human.

## Phase A — SHIPPED (2026-06-24, 1506 green)

`prepare_single` is built on `ApplyWorker`, with the fill path refactored so there is exactly ONE
instrumented fill+record code path (no drift — the doc's core concern):

- **Refactor:** the body of `_process_one` (driver dispatch → resolve/fill → mirror/log →
  persist-proposed → Application row → state walk) moved into a new `@stage("apply")`-decorated
  **`_drive_and_record(*, job, run_id, page, mode, dry_run, remember_dry_run=True) -> ApplyOutcome`**.
  `_process_one` is now a thin shim that supplies the worker's page/mode/dry-run and maps the result
  back to its old contract (None in dry-run, else status). Behaviour unchanged — all prior apply-worker
  tests green.
- **`prepare_single(job_id, *, page) -> ApplyOutcome`:** validates eligibility, walks REVIEW→QUEUED_APPLY
  when needed, then calls `_drive_and_record` with `page=<injected launcher tab>`,
  `mode=BROWSER_ASSISTED`, `dry_run=False`. Forces assisted + real regardless of the worker's posture,
  so it leaves a genuine `ASSISTED_PENDING` attempt the "Ready to finish" lane keys off.
- **Failure contract:** any failure ends the job in REVIEW — a mid-fill crash routes
  APPLYING→FAILED→REVIEW (FAILED row) via `_recover_job_to_review`; a crash before the APPLYING
  transition (we'd promoted REVIEW→QUEUED_APPLY) is put back to REVIEW, so an on-demand fill never
  silently queues a job for an auto-apply.
- **Single-flight:** `self._prepare_lock` (asyncio.Lock) + a `locked()` pre-check → a second concurrent
  call raises `PrepareSingleError(code=409)` rather than racing the one shared BrowserSession.
- **`PrepareSingleError(message, *, code)`** carries an HTTP-ish code (404 no job / 422 no url / 409
  bad-state·unknown-source·in-flight) so Phase B's route maps refusals without re-deriving the reason.
- Tests: `test_apply_worker.py` +9 (happy REVIEW→ASSISTED_PENDING + proposed artifact persisted; forces
  assisted+real+injected-page even when worker is dry/AUTO; QUEUED_APPLY eligible; driver-error→REVIEW
  no stuck state; 404/409-state/422/409-source matrix; single-flight 409). Honesty invariants inherited
  verbatim from the shared path (how-heard from source, fabrication guard, never auto-submits).

---

## Original design (below) — for reference

## What exists today (and why E2 is still wanted)

Three open-in-browser paths already exist:

| Path | Endpoint | Pre-fills? | Eligible jobs |
|---|---|---|---|
| E1 "Open in browser" | `POST /api/jobs/{id}/open` | **No** — just navigates to `job.url` | ANY review job (decide / login groups) |
| Assisted "Open the application" | `POST /api/jobs/{id}/assisted/open` | Yes — re-opens an **already** pre-filled `ASSISTED_PENDING` attempt | only jobs the apply worker already drove to `ASSISTED_PENDING` |
| Login | `POST /api/sources/{src}/login` | n/a | `AUTH_REQUIRED` sources |

The gap the owner hit: a job in the **"Needs your decision"** group (REVIEW, no prior attempt)
has only E1 — open-only. The owner clicked it and "it didn't start filling anything out." E2 is
the missing middle: run the driver's **fill** on the freshly opened page for a REVIEW job that
has no `ASSISTED_PENDING` attempt yet, then leave it pending for the human to submit.

## The honest caveat (must be in the UI copy)

"Needs your decision" jobs are in REVIEW **because the bot could not complete them** — the
fabrication guard blocked the résumé, a required question bailed to assisted, or a prior fill
attempt failed mid-form. So an on-demand fill is **inherently partial**: it fills the
deterministic fields it's confident about (name/email/LinkedIn/city/country/salary/nationality/
notice/years) and leaves everything it bailed on (essays, how-heard, sensitive EEO, unresolved
screeners) for the human. The button copy must say "fills what it can — you finish the rest,"
not imply a complete application.

## Architecture — recommended: borrow the scheduler's apply worker

The scheduler's `ApplyWorker` (built in `cli/main.py serve._factory`) already has everything
wired: `fact_bank`, the two-tier `resolver`, `drivers`, `new_page=session.new_page`, `llm`,
`embed`. **Reusing it is the only way E2's fill matches the production fill path byte-for-byte**
(a fresh route-built worker would drift from scheduler config — two code paths to keep in sync,
exactly the v2 mistake).

Plan:
1. **Expose the worker to the web layer.** `serve` already stashes the `BrowserSession` in
   `_session_holder` and passes `_takeover` to the launcher. Add the constructed `ApplyWorker`
   to a holder (or attach to `web_state`) so a route can reach it. Stays `None` in
   `--no-scheduler` / pre-onboarding → the endpoint 409s with a clear "start the worker" message.
2. **New single-job entry on `ApplyWorker`:** `async def prepare_single(job_id, *, page) -> ApplyOutcome`.
   It runs the SAME body as `_process_one` (salary ask → `resolver.current_job` → `driver.prepare`
   with `mode=BROWSER_ASSISTED`) but: (a) takes an injected `page` (the launcher's tab, so it's the
   tab the human is looking at — no second tab), (b) forces assisted mode regardless of the
   worker's configured mode, (c) is **never dry-run** (it must leave a real `ASSISTED_PENDING`
   attempt), (d) processes exactly one job, no `run_once` loop / `@stage` cycle semantics.
3. **The route** opens the page via the launcher (engaging the #2 manual-takeover gate, so the
   scheduler's apply stage is masked — no clash), then calls `prepare_single` on that page.

### State transitions (verified against `domain/state.py`)

`ASSISTED_PENDING` is an **Application status, not a job state**. The job state walk is:

```
REVIEW ──(prepare)──▶ QUEUED_APPLY ──▶ APPLYING ──(driver fills, assisted halt)──▶ REVIEW
                                              │                     + an ASSISTED_PENDING
                                              │                       Application row
              (driver can't fill / errors)   ▼
                                            FAILED ──▶ REVIEW   (no pending row)
```

Confirmed allowed edges: `REVIEW→QUEUED_APPLY` (state.py:73), `QUEUED_APPLY→APPLYING` (62),
`APPLYING→{REVIEW,FAILED}` (68), `FAILED→REVIEW` (70). **No state-table change needed.**

On success the job is back in `REVIEW` carrying a pending `ASSISTED_PENDING` Application row —
which is exactly the shape the existing **"Ready to finish"** group keys off
(`_latest_assisted_pending` + `assisted_confirm` requires `job.state == REVIEW`). So E2 *promotes*
a "Needs your decision" job into the "Ready to finish" lane: it only has to produce the pending
attempt; `/assisted/open` | `/assisted/confirm` | `/assisted/cancel` already exist.

### Endpoint

`POST /api/jobs/{job_id}/assisted/prepare`
- **404** no job · **422** no `job.url` · **409** worker unavailable (`--no-scheduler`/pre-onboard)
  or job not in a preparable state (only REVIEW/QUEUED_APPLY).
- Success: `{job_id, launch: {...}, outcome: {status, filled, bailed, ...}}` so the dashboard can
  show "filled 7 fields, 3 left for you" and then surface the Ready-to-finish actions.
- Dashboard: a **"Fill what it can"** button next to E1's "Open in browser" in the "Needs your
  decision" group, with the honest one-liner.

## Interaction with #2 (manual-takeover gate) — already handled

E2 opens the page through the launcher, which now `engage()`s the `ManualTakeover` (shipped this
session). So the moment E2 opens the tab, the scheduler's apply stage is masked — the route-driven
`prepare_single` and the scheduler's apply worker can't both drive the session. Releasing on tab
close (or the safety timeout) resumes the scheduler. **E2 depends on #2 being in place** (it is).

One concurrency caveat to enforce: two rapid E2 clicks on different jobs must not run
`prepare_single` concurrently on the same `BrowserSession`. Guard with a simple asyncio lock on
the worker (single-flight) → second click 409s "a fill is already in progress."

## Honesty invariants (unchanged, must hold)

- **How-heard is derived from `job.source`** (owner directive 2026-06-21) — `prepare_single` uses
  the same resolver, which auto-fills the honest per-job channel (ATS ⇒ "Company Website", boards ⇒
  their name). E2 inherits this for free; it never seeds or invents a source.
- **Fabrication guard** still gates the résumé. If optimize never produced a guarded résumé for
  this job, the fill uses the global `resume.pdf` fallback (now legacy-tolerant via the #1 fix) or,
  if none, the driver proceeds without a résumé upload — the human attaches one. E2 must NOT
  fabricate a résumé to fill the field.
- **Never auto-submit.** `prepare_single` forces assisted → halts at `ASSISTED_PENDING`. The human
  always clicks submit. This is the whole point.

## Risks

1. **Real browser apply trigger from a web request.** A bug here drives a live form. Mitigation:
   assisted-only (never submits), single-flight lock, dry-run-able for tests (inject a stub driver).
2. **Partial fills confuse the user** if the copy oversells. Mitigation: honest button + an
   outcome summary ("filled N, left M for you").
3. **State leakage** if `prepare_single` throws mid-fill (job stuck in APPLYING). Mitigation: reuse
   `_recover_job_to_review` (already exists) in a try/finally so a crash routes APPLYING→FAILED→REVIEW.
4. **Worker reachability** — `web_state` doesn't hold the worker today. Mitigation: the holder
   pattern (same as `_session_holder`); endpoint 409s cleanly when absent.

## Open questions for the owner (resolve before building)

1. **Eligible jobs:** only "Needs your decision" (REVIEW, no attempt)? Or also let E2 re-prepare a
   job whose earlier fill failed? (Recommend: REVIEW + QUEUED_APPLY only, exclude APPLIED/terminal.)
2. **Résumé-less fill:** if no guarded résumé exists, fill the other fields and let the human attach
   the résumé, or refuse and tell them to generate one first? (Recommend: fill others, human attaches.)
3. **Outcome surfacing:** is a one-line "filled N / M left" enough, or do you want the per-field
   breakdown (we already log resolutions to events.db, so both are cheap)?

## Test plan (when built)

- `prepare_single` happy path → `ASSISTED_PENDING` + pending Application row (stub driver).
- `prepare_single` driver error → APPLYING→FAILED→REVIEW (no stuck state).
- forces assisted even when worker mode=auto; never dry-run.
- route: 404/422/409 matrix; success shape; single-flight lock 409 on concurrent click.
- how-heard still bails (honesty regression guard).
- takeover engaged for the duration (reuse the #2 launcher test pattern).

## Phasing

Phase A: `prepare_single` on `ApplyWorker` + unit tests (no web). Phase B: worker holder + the
route + dashboard button + route tests. Phase C: live dry-context validation on a real REVIEW job
(owner-watched), then a real ASSISTED_PENDING the owner submits by hand.
