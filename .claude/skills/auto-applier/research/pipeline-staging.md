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

---

## Phase 3 (5/M) — staged-worker scheduler landed

`av3/pipeline/scheduler.py` (new — `Scheduler` + `CycleSummary` +
`SchedulerRunSummary`), `av3/pipeline/quiet_hours.py` (new — `QuietHours`
+ `parse_quiet_hours`, ported from v2's `active_hours.py` with inverted
semantics), `av3/config/settings.py` (new `SchedulerConfig`), and
`av3 run` CLI command — plus 43 new tests (331 v3 tests total, +43 from
(4/M)). **Spec §7a satisfied:** the staged loop is the production entry
that drives filter → score → optimize → apply each cycle in pipeline
order, with quiet-hours gating the apply stage only, cooperative
pause, and per-worker isolation. **When this committed, v3.0 has a
fully working pipeline end-to-end — the apply path that ships v3.0.**

### Scope decisions (kept v3.0 tight)

  * **One cycle drains every stage.** Per-stage cadences ("filter
    every 10s, score every minute") are a v3.1 strategy-profile
    concern (spec §8a Pareto). v3.0 ships one `cycle_interval_s` knob
    and stops.
  * **No discovery stage in the scheduler yet.** Discovery still
    lives in the source adapters and runs separately (CLI / cron /
    Phase 4 dashboard button). Wiring it as a scheduler stage is its
    own slice — it has per-source rate limits, ATS rotation policy,
    etc. that the gather-only workers don't need.
  * **No describe worker.** Per the handoff §4 (5/M) note: for ATS
    sources the JD is populated at discovery, and the score worker
    already fail-closes on empty JD which routes browser-board rows
    (Indeed/Zip) to terminal SKIPPED. A real describe stage arrives
    alongside browser-board apply wiring (post-Phase 3).

### Order is the cycle: filter → score → optimize → apply

Stages run in pipeline order each cycle so a freshly DISCOVERED job
can flow all the way through to QUEUED_APPLY (and, with acceptable
timing, APPLIED) within ONE cycle when the queues are mostly idle.
Order matches the spec §7 pipeline; running stages in any other
order would just delay throughput by a cycle per misordering.

### Quiet hours mask the apply stage ONLY

Apply is the only stage gated by quiet hours. Gather stages
(filter / score / optimize) keep running because:

  * Being wrong in gather is cheap and doesn't compound (Rule 2.6).
  * The user might want results ready when they wake up — a JD scored
    + optimized overnight is in QUEUED_APPLY by the time the window
    closes, so the first cycle after wakeup applies immediately.
  * The user-visible posture during quiet hours is "do not drive my
    browser while I sleep," not "do absolutely nothing."

`QuietHours` ported from v2's `active_hours.py` with inverted
semantics: v2's parser describes when the bot *runs*; v3's describes
when apply does NOT run. The naming flip matches the spec ("quiet
hours") and the v3.0 "always-on by default" stance — windows are
exceptions, not permission. Both overnight ("22:00-08:00") and
same-day ("12:00-14:00") windows work; bad config falls back to
no-op rather than silent-deadlock.

### Per-worker isolation is the reliability move

A crash in the filter worker MUST NOT stop the score / optimize /
apply workers from doing their job. Each `await worker.run_once()`
runs inside a per-stage try/except. The `total_errors` counter
aggregates across all cycles so the CLI exit code still trips when
something's wrong (monitoring catches it), but the loop survives —
a transient outage in one stage doesn't kill the always-on operating
model.

### Cooperative pause — injectable predicate

The handoff §4 called for a "paused flag between iterations" for
F6 / idle-detect hookup in Phase 4. v3.0 (5/M) exposes the predicate
shape: `pause_predicate: Callable[[], bool] | None`. Default is
no-op (always False). Tests drive it directly; Phase 4 will wire it
to the F6 system hook + idle detector. Sidesteps the singleton/
globals issue while keeping the API stable for Phase 4.

The pause check fires ONCE per cycle (not between stages) because
one cycle of gather work is cheap and a mid-cycle pause complicates
the event log without adding meaningful responsiveness — the cycle
interval is the bound on responsiveness, and any reasonable cycle
interval (60s default) is fine for an F6 press.

### Sleep is honored on every cycle including paused/quiet ones

Sleep fires once per cycle regardless of whether anything actually
ran — backpressure invariant. A paused scheduler with no sleep would
spin the CPU; a quiet-hours scheduler with no sleep would burn
events.db with empty 'skip' rows. The injected sleep recorder in
tests confirms one sleep per cycle.

### Telemetry — cycle boundaries

Each cycle emits a `scheduler` start + ok/error/skip event so the
spine records cycle boundaries (useful for "what happened between
03:00 and 03:01?" queries against `events.db`). Per-stage @stage
events still fire inside each worker — the scheduler event is just
the outer boundary, not a replacement.

### CLI shape: `av3 run [--max-cycles N] [--quiet-hours HH:MM-HH:MM]`

The new `av3 run` command is the production entry. The `--once`
flags on per-worker CLIs (`av3 filter --once`, etc.) stay available
for testing and doctor checks. Pre-flight: fact bank + resume.pdf
must exist (the apply worker still reads a single global resume.pdf
as a fallback until per-job derived paths from the optimize worker
are wired into apply — that wiring is the next natural sub-phase
but not blocking for v3.0). CLI flag precedence: explicit flag wins
over `settings.scheduler.*`, both fall back to spec defaults.
Loud confirmation before `--no-dry-run` because this is the only
way to send real applications under the always-on loop.

### What's NOT in this sub-phase

  * **Apply worker reading per-job optimize-generated PDFs.** Still
    uses the single global `resume.pdf` for backward compatibility.
    Wiring `generated_resume_path(settings, job.id)` from (3/M) into
    the apply worker's per-job dispatch is a small slice that lands
    alongside the live-Lever smoketest follow-up.
  * **F6 hook + idle detector.** Phase 4 (web UI + worker service)
    wires these to the cooperative pause predicate exposed here.
  * **Per-stage cadences.** v3.1 strategy profiles.

### Edge cases covered

| Path | Outcome |
|---|---|
| One cycle, healthy workers | filter → score → optimize → apply in order |
| `max_cycles=N` | exits after N cycles |
| `cycle_interval_s=X` | sleep called with X each cycle |
| Inside quiet hours | apply skipped, gather stages run |
| Outside quiet hours | apply runs normally |
| No window configured | apply runs every cycle |
| Pause predicate True | whole cycle skipped (no workers run) |
| Pause predicate False | normal cycle |
| Filter worker raises | other stages still run, error recorded |
| Apply worker raises | next cycle still tries it |
| Multiple stage errors in one cycle | all recorded in cycle.stage_errors |
| Each cycle | scheduler event start + ok emitted |
| Quiet skip | scheduler event status=skip, reason=quiet_hours |
| Paused cycle | scheduler event status=skip, reason=paused |

### Where Phase 3 stands after (5/M)

**v3.0 has a fully working end-to-end pipeline.** The remaining
sub-phases are operational hardening, not pipeline construction:

  6. Retention + backups (spec §4).
  7. Scoring eval harness (spec §10) — pins (2/M)'s prompt versioning
     against a labeled set; also calibrates the (1/M) filter threshold.
  8. Mocked-source CI for selector drift.
  9. Live smoke tests on real ATS APIs/forms (NEVER submits).

---

## Phase 3 (6/M) — retention + backups landed

`av3/pipeline/retention.py` (new — `prune_ephemeral`, `prune_events`,
`backup_app_db`, `backup_events_db`, `run_backup_cycle`),
`av3/config/settings.py` (new `RetentionConfig`),
`av3/pipeline/scheduler.py` (maintenance hook integration — cadence-gated
async callable injected into the scheduler), `av3/doctor.py` (new
`check_backups` recency check), and `av3 prune` + `av3 backup` CLI
commands — plus 23 new tests (354 v3 tests total, +23 from (5/M)).
**Spec §4 satisfied:** ephemeral data prunes on a configurable window,
APPLIED is kept indefinitely (dedup source of truth), both DBs back up
on a separate cadence with rotation. The always-on operating model is
now sustainable for months without manual disk pruning.

### Scope: APPLIED kept forever; everything else has a window

  * **`prune_ephemeral`**: deletes jobs in `EPHEMERAL_STATES` (SKIPPED
    + FILTERED) older than `retention.ephemeral_days` (default 30d).
    APPLIED is *never* pruned — it's the dedup source of truth per
    spec §4. Active-pipeline states (DESCRIBED, QUEUED_APPLY, REVIEW,
    etc.) are also not pruned; in-flight is not ephemera.
  * **`prune_events`**: deletes events older than
    `retention.events_days` (default 14d). Shorter window because
    events.db is the highest-write table.
  * **`backup_app_db` / `backup_events_db`**: SQLite online-backup-API
    snapshots (safe while DBs are in use, WAL included). Rotation to
    keep `retention.backup_keep` newest (default 10) per DB.
  * **`run_backup_cycle`**: convenience wrapper — both DBs attempted
    even if one fails. Asymmetric resilience: a transient events.db
    issue doesn't skip app.db (the load-bearing one).

### Atomicity matters here

Each prune runs inside an explicit transaction via
`av3.db.engine.tx`. A partial prune that deletes half the matched rows
is worse than no prune — it orphans cascades or splits a state's
history. SQLite's cascade behavior removes dependent `job_scores` and
`applications` rows automatically when a `jobs` row is deleted.

### Scheduler integration — cadence-gated maintenance hook

The scheduler gained an optional `maintenance: MaintenanceHook | None`
plus `maintenance_interval_s` (default 3600s). At the end of each
cycle, the scheduler checks "has the interval elapsed since last
maintenance?" via a monotonic-clock comparison. First call after
construction is *always* due (cold-starts inside the interval
shouldn't silently skip). Exceptions from the maintenance hook are
isolated like stage worker exceptions — recorded in `cycle.stage_errors`
under the `"maintenance"` key, loop continues. The hook itself is
injected by `av3 run`'s CLI closure that wires retention.

Why at the *end* of each cycle and not the start: by end-of-cycle,
filter / score / optimize have already drained and apply has either
ran or been quiet-skipped. Doing maintenance here minimizes contention
with the heavy stages and gives the backup snapshot a clean window
where writes have just settled (SQLite's online backup API uses a
WAL checkpoint that briefly takes the write lock).

Why a monotonic clock instead of wall-clock: the wall clock can move
backward (NTP adjustments, DST in some implementations); monotonic
time is guaranteed to only increase, which is what we want for an
"every N seconds" cadence.

### Doctor backup-recency check

`check_backups` returns:

  * **PASS** when the newest `app.db.*` snapshot is younger than
    2 × `retention.maintenance_interval_s` (so a one-cycle blip doesn't
    trip monitoring).
  * **WARN** when older or missing. Not FAIL — backups are recoverable
    from the live DB until something catastrophic happens, and a
    fresh install legitimately has no backup yet. WARN flags a real
    operational concern without breaking CI.

### CLI shape

  * **`av3 prune [--ephemeral-days N] [--events-days N]`**: ad-hoc
    or cron-driven prune. CLI overrides win over settings; output
    surfaces effective values so operators can verify they didn't
    typo the flag.
  * **`av3 backup`**: ad-hoc or cron-driven snapshot. Exit code 1
    on any backup failure (the asymmetric-resilience contract still
    holds — the OTHER DB still gets attempted; we just surface the
    failure to monitoring).

Both commands are safe to run while `av3 run` is active because the
prune txns + backup API both tolerate concurrent reads + writes via
WAL.

### What's NOT in this sub-phase

  * **Auto-prune of generated PDFs / cover letters**. The optimize
    worker writes per-job artifacts under `artifacts_dir/generated/`;
    these accumulate forever. A `prune_artifacts` function paired
    with the scheduler hook would be a clean follow-up but the disk
    impact is small (~50KB/job) and not blocking v3.0.
  * **Configurable per-state retention**. Currently `EPHEMERAL_STATES`
    shares one window. A power-user might want "SKIPPED at 7d but
    FILTERED at 30d" — defer until someone asks.
  * **Backup verification**. The backup API guarantees consistency but
    we don't open + checksum each snapshot. A `doctor` step that
    opens the newest snapshot and runs `PRAGMA integrity_check` is a
    clean follow-up but rarely fails in practice.

### Edge cases covered

| Path | Outcome |
|---|---|
| SKIPPED/FILTERED older than cutoff | deleted |
| SKIPPED younger than cutoff | kept |
| APPLIED of any age | kept (never pruned) |
| Non-ephemeral states (DESCRIBED, etc.) | kept (in-flight not ephemera) |
| Cascade: prune a job with scores + apps | cascade removes them |
| Empty match-set | deleted=0, no error |
| Events older than cutoff | deleted |
| Backup creates timestamped snapshot | snapshot exists in backups_dir |
| Backup rotates to keep=N | rotated count matches surplus, dir size matches |
| run_backup_cycle, one DB fails | error recorded, other DB still backed up |
| Scheduler: first cycle | maintenance fires immediately |
| Scheduler: within interval | maintenance skipped |
| Scheduler: no hook | no-op, maintenance_ran stays False |
| Scheduler: hook raises | error in stage_errors, loop continues |
| Doctor: no backups dir | WARN |
| Doctor: empty backups dir | WARN |
| Doctor: recent snapshot | PASS |
| Doctor: stale snapshot | WARN |

---

## Phase 3 (7/M) — scoring eval harness landed

`tests_v3/eval/golden_set.jsonl` (12 hand-authored JD+band pairs:
4 high / 4 mid / 4 low), `tests_v3/eval/test_score_quality.py`
(live-LLM harness gated by `@pytest.mark.eval`), and the
`@pytest.mark.eval` marker registered in `pyproject.toml`
`addopts` (skipped by default, opt-in via `pytest -m eval`).
**Spec §10 + §11b satisfied:** the (2/M) prompt-version stamps now
have a regression detector. A prompt or model change that drifts
the score distribution will surface as a band miss with the exact
`SCORE_JD.version` in the failure message — no more "scoring looks
weird; was it the prompt or the model?" debugging.

### Two tests, two roles

  * **`test_golden_set_loads_and_validates`** (NO eval marker —
    runs in the default suite): parses the JSONL file, asserts all
    three bands are represented, asserts no duplicate ids, asserts
    field-shape contract. A broken golden set surfaces on every PR,
    not only when someone runs the eval explicitly. Cheap insurance
    against the file being malformed by an editor or merge conflict.
  * **`test_golden_set_score_bands`** (`@pytest.mark.eval` — opt-in):
    the real harness. Constructs a real `ScoreWorker` with a real
    `CompletionClient` (Ollama → Gemini fallback), seeds every JD as
    a DESCRIBED job, runs one `run_once()` pass, asserts every
    pair's `total` lands in the labeled `[band_min, band_max]` range.
    The per-pair failure message embeds `SCORE_JD.version` so a band
    miss points at the exact prompt revision that regressed.

### LLM-availability skip vs fail

If neither Ollama is reachable nor `GEMINI_API_KEY` is set, the eval
test *skips* rather than fails — a dev machine without a model
configured shouldn't break CI gates. The point of the harness is
"when the LLM IS available, here is the quality bar." Live LLM is
the contract; absence is "not measured this run."

### The canonical profile is fixed in the test file

The eval profile is a senior Python data engineer with ETL /
Airflow / dbt / Snowflake background, ~7 years experience, $150k+
salary expectation, defined inline in `test_score_quality.py` rather
than read from a real fact bank. Reproducibility — the harness gives
the same answer on any machine. The 12 JDs are labeled against
*this* profile, so swapping in a different one would invalidate
every band assertion.

### Band ranges (inclusive)

  * **low**: `[0.0, 3.5]` — completely off-target
    (frontend, embedded, marketing, junior QA)
  * **mid**: `[3.5, 6.5]` — adjacent but not core
    (data scientist, backend, data analyst, ML engineer)
  * **high**: `[6.0–6.5, 10.0]` — core fit (senior DE, staff DE,
    early-stage DE, senior analytics engineer)

The `band_min` for HIGH is slightly relaxed to 6.0 on the looser
pairs (early-stage data engineer at a startup, analytics engineer)
because comp + seniority dimensions pull the total down a little
even on a strong skills+experience match. The relaxed lower bound
catches realistic LLM noise without making the assertion meaningless.

### Per-pair version stamping

The harness reads `JobScore.model` (which the score worker stamps
with `{SCORE_JD.version}|{settings.llm.ollama_model}`) on every
score row and asserts the prompt version is present. Two
regressions get caught:

  * **Prompt drift** (band miss): the LLM scored within the prompt
    schema, but the new prompt steers it differently than before.
    Re-pin `band_min`/`band_max` in JSONL after manual verification
    the new behavior is acceptable.
  * **Stamping regression**: if a future refactor breaks the
    `model` field shape (e.g. drops the version), the harness fails
    with "model tag X missing prompt version Y" rather than
    silently no-oping.

### Calibration finding from this harness — DEFERRED

The handoff §4 (7/M) suggested using the harness output to pick
defensible production values for the `0.6` filter threshold and
`4.0`/`7.0` score thresholds. **This requires a real run with the
configured model**, which is the user's call to make on their box.
The harness is now in place to support that calibration; the
thresholds stay at their spec defaults until the user runs
`pytest -m eval` and the bands tell us what to tune.

### What's NOT in this sub-phase

  * **Per-axis assertions**. A future enhancement could pin
    individual dimensions (e.g. "skills must be ≥ 7 on a DE JD")
    rather than just the total. Band-level total is the first
    meaningful gate; per-axis comes after the total assertion
    proves stable.
  * **Auto-tuning of thresholds from the eval output**. The harness
    detects drift; it doesn't change config. Threshold tuning
    remains a deliberate user decision.
  * **Statistical pass criteria** (e.g. "11/12 pairs must pass"
    instead of "every pair"). Loosen only if calibration shows
    persistent borderline cases that aren't quality regressions.
  * **More than 12 pairs**. The spec said "a dozen"; we ship 12.
    Adding pairs is cheap; growth happens organically as users hit
    edge cases worth pinning.

---

## Phase 3 (8/M) — mocked-source CI for selector drift landed

`tests_v3/fixtures/{greenhouse,lever,ashby}/apply_form.html` (3
hand-authored representative apply-form HTML files covering every
selector each driver depends on), `tests_v3/test_selector_drift.py`
(11 parser-based tests asserting standard fields + CAPTCHA carriers
+ custom-question patterns + Ashby UUID exclusion logic resolve in
each fixture), and `scripts/refresh_fixtures.py` (manual refresh
helper that opens the real ATS form via the existing `BrowserSession`
stack and overwrites the fixture). **Spec §10 + §11b satisfied:** the
fast CI gate for selector drift is in place. Live smoke tests ((9/M))
catch outright structural drift on real sites; the mocked tests here
catch the more specific "the selectors our drivers depend on are
still in the saved fixture" regression. When an ATS visibly changes,
the user runs `refresh_fixtures.py` to re-pin.

### Why fixture-based rather than driver-replay

Driver replay (a full async Page stand-in that runs the entire
`prepare_application` flow against saved HTML) would duplicate the
per-driver flow logic already covered by
`test_lever_apply.py` / `test_greenhouse_apply.py` /
`test_ashby_apply.py`. The selector-drift surface is narrower: which
ids/names/classes our drivers depend on are still present. A
small stdlib `html.parser` collector + a handful of
`_has_id` / `_has_name` / `_has_name_starting` helpers covers it
in 200 lines and runs in 80 ms. No BeautifulSoup dep just for CI.

### What each fixture pins

  * **Greenhouse**: `#first_name`, `#last_name`, `#email`, `#phone`,
    `#resume` file input, `button[type=submit]`, `g-recaptcha-response`
    textarea, at least one `#question_*` custom question.
  * **Lever**: `input[name='name']`, `[name='email']`, `[name='phone']`,
    `[name='org']`, `#resume-upload-input`, `resumeStorageId` parse-wait
    signal, `#btn-submit`, `h-captcha-response` carrier, at least one
    `cards[*]` custom-question name, EEO `eeo[*]` discovery target.
  * **Ashby**: `#_systemfield_name`, `#_systemfield_email`,
    `#_systemfield_resume`, `button[type=submit]` (NOT wrapped in
    `<form>` — SPA), `g-recaptcha-response` carrier, at least one
    UUID-named custom question, sanity that no `_systemfield_*` id
    matches the UUID pattern (the discovery exclusion logic depends
    on this disjoint property).

### What this test does NOT catch

  * The actual API/site changed but the fixture is stale. Only the
    (9/M) live smoke test catches that. When the fixture goes stale,
    THIS test still passes; the operator runs
    `scripts/refresh_fixtures.py` to refresh and the test re-pins.
  * Behavioral changes (submit no longer redirects to `/thanks` etc).
    Confirmation detection is tested by its own unit tests in
    `test_detect.py`.

### Refresh workflow

  1. `python scripts/refresh_fixtures.py <ats> <apply-url>` (opens
     headed Chrome via `BrowserSession`, navigates, settles 3s for
     SPAs and CAPTCHA attach, dumps `page.content()` to the fixture).
  2. `pytest tests_v3/test_selector_drift.py -v` to see which
     selectors changed.
  3. If selectors drifted, update the driver code to match; if
     the fixture just got fluffier (vendor added new classes,
     reshuffled markup), re-run the test — it should still pass
     because the selector probes are tolerance-aware.
  4. Inspect the diff, commit fixture + any driver changes together.

### Edge cases covered

| Path | Outcome |
|---|---|
| Each ATS fixture file present + parseable | passes |
| GH standard fields all resolve | passes |
| GH `g-recaptcha-response` carrier present | passes |
| GH at least one `#question_*` custom Q | passes |
| Lever standard fields all resolve | passes |
| Lever `h-captcha-response` carrier present | passes |
| Lever `cards[*]` custom Q pattern present | passes |
| Lever `eeo[*]` discovery pattern present | passes |
| Lever `resumeStorageId` parse-wait signal present | passes |
| Ashby `_systemfield_*` fields all resolve | passes |
| Ashby UUID-named custom Q pattern present | passes |
| Ashby UUID/systemfield disjoint sanity | passes |

### What's NOT in this sub-phase

  * **Confirmation page fixtures**. The success-text + URL pattern
    detector is covered by unit tests; capturing the post-submit
    confirmation HTML is its own slice if desired later.
  * **Discovery-side fixtures**. ATS discovery hits API endpoints
    (JSON), not HTML — the API responses are already covered by
    integration tests with live network. Mock fixtures for those
    would be a different sub-phase.
  * **Refresh-script auto-diff**. The script prints byte-size deltas
    but doesn't run the selector-drift tests for the operator. They
    run `pytest tests_v3/test_selector_drift.py -v` after refresh
    by convention — adding it to the script muddles the script's
    single responsibility (capture HTML).
