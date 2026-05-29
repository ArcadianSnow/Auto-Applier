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
