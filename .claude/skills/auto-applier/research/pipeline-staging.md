# Pipeline staging — Phase 3 (spec §7 + §11b)

Knowledge base for the Phase 3 hardening work that turns the v3 pipeline from
"apply-only worker can drain a hand-seeded queue" into a fully staged loop:

```
discover → dedup/ghost → FILTER (this doc, 1/M) → describe → score →
optimize+Strict gate → apply → post-apply
```

This doc lives alongside `ats-form-automation.md`. Phase 2 sat under that one
because the work was per-ATS apply-driver shape; Phase 3 is about the *queue
between drivers*, so it gets its own slug. New sub-phases append a section here
in the same `## Phase 3 (X/M) — <slug>` style.

---

## Phase 3 (1/M) — embedding pre-filter landed

`av3/pipeline/filter_worker.py`, `av3 filter` CLI command, 25 new tests. **Spec
§7 #3 satisfied**: cosine-rank `(title + company + snippet)` vs the master
fact-bank summary; `>= threshold` → `DISCOVERED → DESCRIBED`, `< threshold`
→ `DISCOVERED → FILTERED` (terminal). This is the cheap pre-pass that keeps the
expensive describe + LLM-score stages off obvious non-matches.

### Why the worker is "fail-open," not "fail-closed"

The state machine only allows `DISCOVERED → {SKIPPED, FILTERED, DESCRIBED}`;
REVIEW isn't reachable from DISCOVERED. So if the embedding layer is
unavailable mid-run, the only options are FILTER (terminal — drops jobs the
user would never see again) or DESCRIBE (move them forward). **Routing to
DESCRIBED is the correct choice** because:

1. The downstream score worker will still reject obvious non-matches on the
   *full JD* — the pre-filter was a cheap shortcut, not a safety gate. Losing
   the shortcut means more cost, not more incorrect applies.
2. A silent inference outage that drops the whole DB into FILTERED is exactly
   the operational footgun this project must avoid — there's no "undo
   FILTERED" path in the state machine.

The worker bookkeeps the fail-open count separately from the cosine-pass count
(`failed_open` vs `passed` on `FilterRunSummary`) so the dashboard / CLI can
distinguish "the filter ran and let this job through on merit" from "the
filter couldn't run and let everything through". The CLI also exits 1 when
`errors > 0` so a misconfigured Ollama still trips a monitoring alert.

### What the worker embeds

- **Anchor side (the user):** one concatenated string built by
  `build_bank_summary(fact_bank)` — skills + work titles + work bullets +
  degrees + certifications. Dates and quantities are dropped (they're noise
  for semantic similarity). **Embedded ONCE per run** and cached on the worker;
  per-job loops reuse the cached vector.
- **Query side (the job):** `(title + company + description)` — whatever
  listing-page text exists at DISCOVERED. Full JD doesn't arrive until
  DESCRIBED, which is precisely the point: pay the JD-scrape cost only for
  jobs that survived the cosine pre-filter.
- **Empty texts are not embedded.** Empty bank summary → fail-open every job
  (zero-norm cosine would FILTER everything). Empty per-job query (a thin
  discovery row with no title/snippet) → that job alone fail-opens to
  DESCRIBED so the describe stage can scrape the JD properly.

### Threshold: 0.6 default, do not tune blind

The default lives on the constructor (and the `--threshold` CLI flag), not in
`Settings`, because tuning it before the **scoring eval harness (Phase 3
(7/M))** exists would just lock in noise from a too-small sample. The
conservative default favors recall: 0.6 lets a borderline match through to the
next stage rather than terminally filtering it. The harness will provide the
labeled set we need to pick a defensible production value.

Note the spec hints at a "top-N rather than threshold" variant. Threshold is
what v3.0 ships because the pre-filter runs per cycle, not per batch — there's
no obvious "N" to pick across cycle boundaries. The cycle/batch knob is a v3.1
strategy-profile concern (spec §8a).

### Edge cases covered (see `tests_v3/test_filter_worker.py`)

| Path | Transition | Counter |
|---|---|---|
| cosine ≥ threshold | `DISCOVERED → DESCRIBED` | `passed` |
| cosine < threshold | `DISCOVERED → FILTERED` (terminal, StageSkip event) | `filtered` |
| no embed client (`--no-llm`) | `DISCOVERED → DESCRIBED` | `failed_open` |
| empty bank summary | `DISCOVERED → DESCRIBED` (no embed calls fire) | `failed_open` |
| empty per-job listing text | `DISCOVERED → DESCRIBED` (bank embed only) | `failed_open` |
| bank-embed exception | `DISCOVERED → DESCRIBED` × all jobs in run | `failed_open` |
| per-job embed exception | `DISCOVERED → DESCRIBED` (that job only) | `failed_open` + `errors` |
| empty queue | no-op (bank not even embedded) | all zero |

The below-threshold path raises `StageSkip` *after* writing FILTERED, so the
event spine records `status='skip'` with the cosine value as `reason` — useful
for ad-hoc "why was this filtered?" queries against `events.db`.

### What's NOT in this sub-phase

- Per-source threshold overrides — single global threshold for now, on the
  worker. Adding source-specific knobs without the eval harness would lock in
  unjustifiable guesses.
- Top-N gating (see threshold note above).
- Automatic threshold tuning from outcome data — that's the §8e outcome
  feedback loop, v3.1.

### Where it slots into Phase 3

This is sub-phase **(1/M)** of the Phase 3 chain. The recommended order is
documented in the prior session's handoff (the embedding pre-filter unblocks
nothing else by itself, but it's the smallest reachable Phase 3 commit and
proves the staged-worker pattern that (5/M) generalizes). Next:

2. **Score + generate LLM wiring** (Phase 1 leftover that's a Phase 3
   prerequisite): replace the score stubs with a live LLM dimension-score
   against the master fact bank.
3. **Optimize+Strict gate**: tailored résumé + cover letter + fabrication
   guard. Pass → QUEUED_APPLY; fail → REVIEW.
4. **Session-expiry graceful degradation** (spec §8b).
5. **Staged-worker scheduler** (`av3 run`): loops all workers on independent
   cadences. Replaces `av3 apply --once` and `av3 filter --once` as the
   production entry.
6. Retention + backups, scoring eval harness, mocked-source CI, live smoke
   tests — hardening for ship.

When (1)–(5) are committed, v3.0 has a fully working pipeline end-to-end.

---

## Phase 3 (2/M) — score worker + LLM dimension scoring landed

`av3/pipeline/score_worker.py`, `av3/llm/prompts.py` (new — versioned templates,
spec §10), `av3 score` CLI command, 27 new tests. **Spec §7 #5 + §10 satisfied:**
LLM dimension-scores the full JD against the master fact-bank profile across the
seven weighted axes (`skills 0.35`, `experience 0.20`, `seniority 0.15`,
`location 0.10`, `culture 0.08`, `growth 0.07`, `compensation 0.05`), Python
computes the weighted total per `Settings.scoring.weights`, writes a `JobScore`
row, and walks `DESCRIBED → SCORED → DECIDED`. Below `review_min` (default 4.0)
keeps walking `DECIDED → SKIPPED` so the optimize worker can trust every
DECIDED job it picks up is worth optimizing.

### Why the score worker is "fail-CLOSED," not "fail-open"

This is the **inversion** of the filter worker's posture and the inversion has to
be deliberate, not accidental:

| Failure mode | Filter worker | Score worker |
|---|---|---|
| No LLM/embed client | route to DESCRIBED (`failed_open`) | walk to SKIPPED with `total=0` (`failed_closed`) |
| Per-job exception | route to DESCRIBED, isolate | walk to SKIPPED with `total=0`, isolate |
| What's at stake | losing user jobs in terminal FILTERED | auto-applying unscored garbage downstream |

The filter worker's failure mode (silently FILTER everything) would lose user
jobs forever — DESCRIBED is the right fail-open target. The score worker's
failure mode (silently let everything through to optimize with score 0.0) would
auto-apply unscored garbage — SKIPPED is the right fail-closed target. **Both
CLIs exit 1 on `errors > 0`** so a misconfigured Ollama trips monitoring even
when the per-job behavior is "graceful."

### The fail-closed walk goes through every transition

`_write_fail_closed` walks `DESCRIBED → SCORED → DECIDED → SKIPPED` rather than
shortcutting. Reasons:

1. The state machine (spec §5) has no `DESCRIBED → SKIPPED` edge — the
   allowed-transitions table is the single chokepoint and we don't punch new
   edges for fail paths.
2. The dashboard renders fail-closed and real-but-below-bar jobs identically:
   "this job was attempted, scored X, didn't meet the bar." Fail-closed just
   has `X=0`. Consistent UI from consistent state machine paths.
3. A `JobScore` row gets written for every attempt, so the audit trail records
   "we tried this job with `total=0.0` and model tag `score-jd-v1|no-llm`"
   rather than leaving a silent gap.

### `StageSkip` raise discipline

The fail-closed helper does **durable writes only** — it does NOT raise. Callers
decide whether to raise `StageSkip`:

- Inside `_process_one` (the `@stage("score")` frame): raise `StageSkip` after
  the write so the event spine records `status='skip'` with the reason, not
  `'ok'`.
- From `run_once`'s exception handler: just write and return. The handler is
  already past the `@stage` frame; raising would propagate the `StageSkip` out
  of `run_once` itself and break the run. (This was the first defect during
  authoring — a generic helper that always raised broke the per-job isolation
  contract.)

### Prompt versioning

`av3/llm/prompts.py` is new — spec §10 mandates *"prompts live in versioned
template files (not inline)."* Every `PromptTemplate` carries a `version`
string; the score worker stamps `{prompt_version}|{llm_model}` into
`JobScore.model` so a score row is self-describing. This is what the **Phase 3
(7/M) eval harness** will pin against: a prompt change that drifts scores will
be detectable from the version stamp alone.

Initial template: `SCORE_JD` (version `score-jd-v1`). Demands JSON-only output
with the exact 7-key axis shape; defensive parser (`parse_dimensions`) defaults
missing axes to 5.0 (neutral), clamps to `[0, 10]`, coerces numeric strings,
and rejects only non-dict payloads. Strict at the wire, lenient at the merge —
a partial reply doesn't crater the run; a malformed reply isolates one job.

### Defensive parser specifics (see `tests_v3/test_score_worker.py::test_parse_dimensions_*`)

| Input | Output |
|---|---|
| missing axis | 5.0 (neutral, mirrors the prompt's "unstated → 5.0" rule) |
| value > 10 | clamped to 10.0 |
| value < 0 | clamped to 0.0 |
| `None` | 5.0 |
| non-numeric string | 5.0 |
| numeric string ("7.5") | 7.5 (Python `float()`) |
| `True` / `False` | 1.0 / 0.0 (Python coercion) |
| `NaN` / `±inf` | 5.0 |
| non-dict payload (list, string) | raises `ValueError` → per-job fail-closed |

### What's NOT in this sub-phase

- **Résumé / cover-letter generation** — that's the (3/M) optimize worker.
  "Score + generate LLM wiring" in the handoff was about wiring the LLM for the
  *score* path; the *generate* path is the next sub-phase.
- **A describe worker** — the spec lists describe as step #4 between filter and
  score. For ATS sources (Greenhouse/Lever/Ashby) the description is already
  populated at discovery time (the public APIs return JD text), so the score
  worker reads `job.description` directly. For browser boards (JobSpy/Indeed/
  Zip) a real describe step will land alongside (5/M)'s scheduler — until then
  those rows fail-closed with "empty job description" which is the conservative
  right answer.
- **Eval harness** — that's (7/M). The version-stamping landed here so when the
  harness arrives, it has version-tagged rows to pin against.
- **Auto-tuning thresholds** — `auto_apply_min` and `review_min` stay at their
  spec defaults (7.0 / 4.0). Outcome-driven tuning is the §8e feedback loop,
  v3.1.

### Test pattern reused across (1/M) and (2/M)

Both workers' tests follow the same shape:
- Fake the LLM / embedding client via a small inline stub that records calls.
- Seed the right state via the canonical state-machine walk (no ad-hoc INSERT).
- Assert: state transition + summary counter + Application-or-JobScore row +
  telemetry event row (when relevant).
- CLI tests stub the *worker* entirely (recording its kwargs) so they cover
  argument parsing / pre-flight / exit codes without re-testing worker logic.

Carry this shape forward into (3/M) and beyond — `tests_v3/test_*_worker.py` +
`tests_v3/test_cli_*.py` becomes a stable two-file template per sub-phase.

---

## Phase 3 (3/M) — optimize+Strict gate landed

`av3/pipeline/optimize_worker.py`, `av3/llm/prompts.py` additions
(`GENERATE_RESUME` v `gen-resume-v1` + `GENERATE_COVER_LETTER` v `gen-cover-v1`),
`av3/resume/generate.py` (orchestration + canonical paths),
`av3/resume/render.py` (Playwright HTML → PDF + injectable seam), `av3 optimize`
CLI command, **29 new tests** (272 v3 tests total, +29 from (2/M)).
**Spec §7 #6 + §6b satisfied:** drains DECIDED, generates a per-job tailored
résumé + cover letter from the fact bank, runs the L1 fabrication guard,
renders the résumé to ATS-safe single-column PDF. ALL FOUR gates (LLM available
+ résumé generation + cover-letter generation + guard PASS + PDF render) must
clear or the job walks `DECIDED → REVIEW`. Pass → `DECIDED → QUEUED_APPLY`,
where the apply worker reads it blindly. **The Strict gate is THE safety
mechanism that justifies `BROWSER_AUTO`** — without it the apply worker would
auto-submit fabricated résumés on real applications.

### Schema decision — derived paths, no new columns

The handoff called out the "where do `generated_resume_path` /
`cover_letter_path` live?" open decision. **Resolution: derived paths,
no schema change.**

  * Résumé PDF      → `settings.artifacts_dir / "generated" / "{job.id}.pdf"`
  * Cover letter txt → `settings.artifacts_dir / "generated" / "{job.id}_cover.txt"`

Both workers (optimize writes, apply reads) derive the same paths from
`job.id` via `av3.resume.generate.generated_resume_path` /
`generated_cover_letter_path`. **File existence is the durable "this job has
been optimized" contract.** The apply worker writes those paths into the
`Application` row it creates at submit time (where the schema already has
`cover_letter_path` + `generated_resume_path` columns per spec §4) — no
`Job` column added, no `db/migrations.py` shim needed. The handoff's
recommended "add columns to `Job`" approach would have been fine too;
derived paths just dodge the migration entirely.

### Why the worker is "fail-CLOSED," not "fail-open"

Same posture as the score worker, and same reason: the next stage downstream
is the **apply worker**, which reads `QUEUED_APPLY` blind. A fail-open here
would route un-vetted jobs straight to auto-submit — exactly the catastrophic
outcome the Strict gate exists to prevent.

| Failure mode | Optimize-worker handling | Counter |
|---|---|---|
| No LLM client at construction | walk DECIDED → REVIEW for every job | `failed_closed` (+ `routed_to_review`) |
| Per-job résumé/cover LLM exception | walk that one job to REVIEW | `failed_closed` + `errors` |
| Malformed LLM payload (non-dict) | parse raises → caught upstream → REVIEW | `failed_closed` + `errors` |
| Empty cover-letter body | parse raises → REVIEW | `failed_closed` + `errors` |
| Empty JD on a DECIDED row | walk to REVIEW (defensive; score worker normally catches this earlier) | `failed_closed` |
| Fabrication guard verdict != PASS | walk to REVIEW (the gate doing its job) | `guard_rejected` (+ `routed_to_review`) |
| PDF renderer returns False | walk to REVIEW (after guard passed) | `render_failed` (+ `routed_to_review`) |

**Asymmetric cost rationale:** the cost of a false REVIEW is a manual user
click; the cost of a false QUEUED_APPLY is a fabricated submission on a real
application. Both `failed_closed` and `guard_rejected`/`render_failed` get
the same REVIEW destination but **stay in separate counters** so the
dashboard can distinguish "fix your Ollama" (operational failure) from
"review this fabrication" (content-driven rejection).

### Exit-code policy (matches score worker)

The CLI exits 1 when `errors > 0` so a misconfigured Ollama still trips a
monitoring alert even though per-job behavior was graceful. **Guard rejections
and render failures do NOT trip the exit code** — those are intended-pathway
outcomes (the gate is supposed to reject bad output sometimes, and a missing
Playwright is a setup gap the user fixes at install).

### Ordering inside `_process_one` is load-bearing

The five steps run in a deliberate order; reordering any pair changes the
failure semantics:

  1. **Generate résumé** (LLM call #1) — fail-CLOSED on exception/malformed.
  2. **Generate cover letter** (LLM call #2) — fail-CLOSED on exception/empty.
  3. **Run fabrication guard** — fail-CLOSED on non-PASS verdict.
  4. **Render PDF** — fail-CLOSED on render-returns-False.
  5. **Write cover letter `.txt`** — exception caught as per-job error.

Why guard runs BEFORE render: guard works on the structured `GeneratedResume`
shape, not the PDF. Running render first would burn ~1s of Playwright per
job that's about to be rejected anyway. And critically: a guard rejection
must leave no PDF on disk (otherwise the apply worker, which keys off file
existence, could pick up a fabricated résumé from a stale rejection).

Why cover-letter `.txt` write runs AFTER PDF render: if the render fails,
we want to leave no `_cover.txt` orphan on disk. The apply worker's
contract is "both files exist or this job hasn't been optimized" — a
lone cover letter from a render-failed run would lie about completeness.

### `StageSkip` raise discipline (carried forward from score worker)

`_route_to_review` does **durable writes only — does NOT raise.** Callers
decide whether to raise `StageSkip`:

  * Inside `_process_one` (the `@stage("optimize")` frame): raise `StageSkip`
    after the write so the event spine records `status='skip'` with the
    reason, not `'ok'`.
  * From `run_once`'s exception handler: just write and return. The handler
    is already past the `@stage` frame, so raising would propagate
    `StageSkip` out of `run_once` itself and break the run — the same
    defect the (2/M) authoring caught (see "StageSkip raise discipline"
    section above for the score worker's version).

### Prompt versioning + tagging

Two new templates with version strings stamp the run's notes alongside the
LLM model name:

  * `GENERATE_RESUME` → version `gen-resume-v1`
  * `GENERATE_COVER_LETTER` → version `gen-cover-v1`

Notes include lines like `"queued job <id>: resume=gen-resume-v1|gemma4:e4b
cover=gen-cover-v1|gemma4:e4b"` so a future audit can trace any generated
résumé back to a specific prompt revision. Future work (a sidecar
`<job_id>_meta.json` mirroring the row's stamp) is out of scope for v3.0 —
the file existence already says "this job was optimized"; the version
stamp lives in the notes for now.

### What's NOT in this sub-phase

  * **Layers 2–4 of the fabrication guard** (embedding retrieval, NLI,
    LLM self-check) — Phase 1 ships L1 only per `fabrication-guard.md`;
    later layers are deferred. The Strict gate is fail-CLOSED on L1
    alone, so a more aggressive guard would just narrow what passes
    (no safety hole opened by the deferral).
  * **An apply worker that reads the per-job paths** — the apply worker's
    Phase 2 (3/N) version still uses a single `self._resume_path` global.
    Wiring it to read the per-job derived paths is a Phase 3 (5/M)
    scheduler concern (the scheduler's wiring is the natural place to
    swap the resume_path from "single global" to "per-job derived").
    The optimize gate's contract (the files exist at the derived paths)
    is already satisfied.
  * **Cover-letter outcome learning** — spec §8e's data-revisable length
    knob is v3.1. The fixed 200-word default ships now.
  * **Sidecar guard findings JSON** — the worker captures the verdict in
    its notes; persisting the full `Finding` list as a per-job JSON for
    the dashboard's "why was this rejected?" view can land alongside the
    Phase 4 web UI without changing the optimize contract.

### Edge cases covered (see `tests_v3/test_optimize_worker.py`)

| Path | Transition | Counter |
|---|---|---|
| all gates clean | `DECIDED → QUEUED_APPLY` | `queued` |
| no LLM client (`--no-llm`) | `DECIDED → REVIEW` × all jobs | `failed_closed` (+ `routed_to_review`) |
| per-job LLM exception (résumé side) | `DECIDED → REVIEW` (one job) | `failed_closed` + `errors` |
| per-job LLM exception (cover side) | `DECIDED → REVIEW` (one job, no PDF written) | `failed_closed` + `errors` |
| malformed résumé payload (non-dict) | `DECIDED → REVIEW` | `failed_closed` + `errors` |
| empty cover-letter body | `DECIDED → REVIEW` | `failed_closed` + `errors` |
| empty JD on a DECIDED row | `DECIDED → REVIEW` (LLM never called) | `failed_closed` |
| guard verdict = HARD_FAIL | `DECIDED → REVIEW` (no PDF written) | `guard_rejected` |
| guard verdict = REVIEW (no PASS) | `DECIDED → REVIEW` | `guard_rejected` |
| PDF render returns False | `DECIDED → REVIEW` (no orphan cover) | `render_failed` |
| limit honored, oldest-first | first N processed, rest stay DECIDED | n/a |
| empty queue | no-op summary, no LLM call | all zero |

The routed-to-REVIEW path raises `StageSkip` *after* writing REVIEW, so
the event spine records `status='skip'` with the reason — useful for ad-hoc
"why was this routed?" queries against `events.db`. Successful queue path
records `status='ok'`.

### Where it slots into Phase 3

This is sub-phase **(3/M)** of the Phase 3 chain. With (3/M) committed,
**v3.0 has a near-complete end-to-end pipeline**: discover → filter →
describe (implicit for ATS sources at discovery) → score → optimize+Strict
gate → apply. The remaining sub-phases are operational hardening rather
than pipeline construction:

  4. **Session-expiry graceful degradation** (spec §8b).
  5. **Staged-worker scheduler** (`av3 run`): production entry that
     drives all the `--once` workers on independent cadences.
  6. Retention + backups (spec §4).
  7. Scoring eval harness (spec §10) — pins (2/M)'s prompt versioning
     against a labeled set; also calibrates the (1/M) filter threshold.
  8. Mocked-source CI for selector drift.
  9. Live smoke tests on real ATS APIs/forms (NEVER submits).

---

## Phase 3 (4/M) — session-expiry graceful degradation landed

Four-layer wiring across `av3/sources/browser/detect.py`,
`av3/sources/health.py` (NEW), `av3/sources/browser/apply_base.py`,
all three apply drivers (lever/greenhouse/ashby), and the apply worker —
plus 16 new tests (288 v3 tests total, +16 from (3/M)). **Spec §8b
satisfied:** when manual login dies mid-run, the affected source pauses
in a process-level health registry, telemetry emits a `session_expiry`
event for the dashboard's future "login needed" badge, and the apply
worker skips that source's jobs silently while other sources keep
running. **One dead session never stalls the whole bot.**

### The four layers

1. **Pure detector** (`detect.py`). `detect_login_wall(url, html) →
   AuthWallResult` matches a tuple of well-known login URL substrings
   (winners outright) OR a *both*-condition HTML check (password input
   AND a sign-in label). The HTML rule requires both signals because
   a password input alone (e.g. a passwordless-candidate widget
   embedded on a normal apply form) would false-positive and pause a
   healthy source. False-positives cost the user a manual re-login;
   false-negatives cost a wasted apply submit to a login page. The
   asymmetry favors the both-required HTML rule.

2. **Health registry** (`av3/sources/health.py`, NEW). Process-level
   in-memory dict keyed by source name, lock-guarded so a future
   multi-worker scheduler ((5/M)) doesn't race on mutations. API:
   `mark_auth_required(source, reason)`, `mark_healthy(source)`,
   `is_paused(source)`, `paused_sources()`, `snapshot()`,
   `reset_health()` (test-only). Why in-memory and not DB-persisted:
   the manual-login state lives in the browser profile dir which IS
   persistent, so a restart should re-probe via the next live request
   rather than carry a possibly-stale "needs login" flag. Persisting
   would risk a stale dashboard badge after the user logs back in.

3. **Driver hook** (`apply_base.check_auth_wall(page, source) → str`).
   The thin wrapper drivers call after `page.goto(apply_url)` — runs
   the detector, marks the source on detect, returns the signal so
   the driver can stamp a clear note on the outcome and early-exit
   with `status=FAILED`. The apply worker's existing FAILED→REVIEW
   translation handles the rest (no new state-machine edges). All
   three drivers (lever/greenhouse/ashby) gained the check directly
   after their existing post-navigation wait.

4. **Apply worker integration**. New `summary.paused` counter; the
   per-job loop checks `is_paused(job.source)` BEFORE dispatch and
   silently skips with no state change (the job stays in
   QUEUED_APPLY for next cycle). Sits between "unknown source"
   and "per-company rate limit" in the skip chain — paused sources
   shouldn't burn the rate-limit slot or be miscounted as
   driver errors. CLI summary surfaces it: `... skipped=N paused=N
   ... ` so the operator sees "5 skipped of which 5 are paused"
   vs "5 skipped, 0 paused" distinctly.

### Telemetry — emit on transition, not per check

`mark_auth_required` / `mark_healthy` emit a `session_expiry` event
ONLY when the state actually changed. Repeated `mark_auth_required`
on an already-paused source is a no-op on telemetry (so a polling
check doesn't flood the spine). Same for `mark_healthy` on an
already-healthy source. This keeps the event log honest about
"when did the source actually die / come back" rather than spamming
heartbeats. The reason string updates regardless (the latest cause
wins) but only the first transition writes an event row.

### What's NOT in this sub-phase

  * **Auto-recovery from a successful login.** The user has to either
    use the dashboard "I'm back in" button (Phase 4) or wait for an
    `av3 health clear <source>` CLI command (deferred — easier added
    alongside Phase 4 when the dashboard polls `snapshot()`). For
    now, the worker can re-process a paused source after a process
    restart (the registry is in-memory) — which is the documented
    Phase-3 workflow.
  * **Discovery-side hooks.** The spec mentions both discovery + apply
    paths. Discovery via ATS APIs is auth-free (spec §6a), so no hook
    is needed there in v3.0. JobSpy browser discovery (Indeed/Zip) is
    the realistic candidate but its discovery path is HTML-scraping
    not Playwright; wiring is the (5/M) scheduler's job.
  * **A CLI to inspect/clear health.** `av3 health` is straightforward
    but bundling it with the (5/M) scheduler keeps the CLI surface
    coherent (the dashboard will be the primary surface anyway).

### Edge cases covered (see `tests_v3/test_session_expiry.py`)

| Path | Outcome |
|---|---|
| URL contains `/login`, `/signin`, `/sign_in`, `/auth/login`, ... | wall present |
| HTML has password input + sign-in label | wall present (both required) |
| HTML has password input only | wall NOT present (passwordless widget) |
| HTML has sign-in label only | wall NOT present |
| Normal Lever apply form (name/email/file/submit) | wall NOT present |
| `mark_auth_required` then `is_paused` | True |
| `mark_auth_required` then `mark_healthy` | False, telemetry emits twice |
| `mark_auth_required` × 2 | telemetry emits once (idempotent) |
| `mark_healthy` on already-healthy | no telemetry emit |
| Empty source name on either mark | silent no-op |
| `snapshot()` returns a copy | mutating it does not affect registry |
| Apply worker, paused source | skip, state unchanged, no driver call |
| Apply worker, mixed paused + healthy sources | paused skipped, healthy runs |
| Paused source recovers after `mark_healthy` | next run processes it |
