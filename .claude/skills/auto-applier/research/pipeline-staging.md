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
