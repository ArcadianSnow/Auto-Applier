# Application copilot (`av3 ask` + `/copilot`) — design & rationale

> Status: **v1 LIVE (2026-06-11).** Spec §8f. Module: `auto_applier/copilot.py`. Surfaces:
> `av3 ask` (CLI) + `/copilot` (web) + `POST /api/copilot/ask`.

## Where this came from

During the live personal search (2026-06-10), a human + frontier model answered real screener
questions by hand — Debezium/CDC, "are you in US Central time," Monzo "why us," a
silent-data-incident STAR. The pattern that emerged: **ad-hoc, honest, per-job question answering
is its own product surface**, distinct from the form-filling `answer_resolver` (§8b). The resolver
fills *known* fields from a bank of stored answers; the copilot reasons over
{fact bank + the specific job + the question} to answer *arbitrary* questions — and its hard part
is not generation, it's **honesty**.

## The design problem: local models overclaim

The nuanced honesty in the live session came from a frontier model. **qwen3:8b is exactly the
model that will check "Yes" on "Have you led a Debezium implementation?" to be helpful.** The
deterministic fabrication guard (§6b) catches invented *facts* (companies, dates, metrics) but NOT
strategic overclaim — a wrong "Yes" is not a fabricated noun, it's a judgment failure.

So the centerpiece of the copilot is the **evidence audit**: a deterministic post-LLM gate that is
to judgment calls what the fabrication guard is to facts.

### The honesty contract (enforced, not requested)

1. **The prompt demands structured evidence.** The model must return `bank_evidence`: the list of
   fact-bank facts its verdict rests on. Schema:
   `{verdict: yes|no|partial, short_answer, long_answer, reasoning, bank_evidence: [str],
   overclaim_risk: none|low|high, risk_note, framing, gaps: [str]}`.
2. **The audit verifies the evidence deterministically.** Each `bank_evidence` item is
   token-overlap-checked against a normalized corpus built from the bank (skills, work bullets,
   titles, companies, metrics, certifications, education). An item is *supported* when ≥ 60% of
   its content tokens appear in a single corpus entry (or substring containment either way).
3. **Unsupported "yes" fails closed.** A `yes`/`partial` verdict with **zero supported evidence**
   is downgraded to `review` (`needs_review=True`) with the reason attached. Unsupported items are
   flagged and `overclaim_risk` is raised to `high`. A `no` verdict needs no evidence — saying
   "no" honestly is always allowed.
4. **Literal-vs-broad is in the prompt's rules.** "Debezium *or another CDC event tracking
   system*" means log-based event CDC; watermark/timestamp incremental sync is adjacent, not the
   thing. The instructed pattern: **verdict `no` (or `partial`), with the adjacent experience in
   `long_answer`** — "No + here's what I *have* done" beats an agreeable yes in any technical
   screen. The audit can't check this semantically; the prompt rule + the evidence requirement
   together push toward it (a literal "yes" to Debezium has no bank evidence to cite).
5. **Sensitive questions never reach the LLM.** `classify_sensitive` (§8d, reused from the
   resolver) routes work-auth / sponsorship / EEO / salary to the same deterministic policies the
   resolver uses: explicit bank fields or REVIEW, EEO self-ID or "Prefer not to answer", salary
   from the §8d ask (injected by the caller) or REVIEW. The v2 "US-yes" bug stays dead.

### What the copilot is NOT (v1 scope)

- **Not a chat loop.** One question → one structured answer. Conversation memory is a later
  nicety; the structured single-turn covers the real use (a form field is in front of you).
- **Not auto-submit.** Nothing the copilot produces flows into a form unattended. It's a human
  tool; `--save` explicitly stores an accepted answer into the answer bank (where the resolver's
  Tier-1 semantic match can reuse it on real forms — that's the deliberate synergy loop).
- **Not a knowledge engine.** Requirement 4 from the direction memo (visa rules, location codes,
  comp bands — knowledge *outside* the bank) is bounded in v1: the prompt allows general
  domain knowledge for *interpreting the question* (what Debezium is, what `DE-` prefixes mean)
  but verdicts about THE CANDIDATE must rest on bank evidence. A local 8B model's general
  knowledge is not reliable enough to assert visa law; the copilot says what it knows and flags
  the rest (`framing` / `risk_note`).

## Module shape (v3 grain)

- `auto_applier/copilot.py` — top-level module like `research.py`/`analytics.py`/`reconcile.py`.
  - `CopilotAnswer` dataclass (all schema fields + `needs_review`, `audit_notes`,
    `unsupported_evidence`).
  - `Copilot(llm)` — `CompletionClient`-injected, like `StoryGenerator`/`CompanyResearcher`.
    `async answer(question, bank, *, job=None, salary_ask="") -> CopilotAnswer`. Never raises;
    LLM failure → `verdict="review"` with the reason.
  - `audit_evidence(bank, evidence) -> (supported, unsupported)` — the deterministic gate, pure,
    exported for tests.
  - Job context (optional): title / company / location / description excerpt (~1500 chars)
    flows into the prompt so per-job nuance (location, stack) informs the answer.
- Prompt: `COPILOT_ANSWER` (`copilot-answer-v1`) in `llm/prompts.py`, registered in
  `ALL_TEMPLATES`.
- CLI: `av3 ask "<question>" [--job ID] [--save] [--json]`. `--save` upserts
  (question → long_answer) into the answer bank via the existing `store_answer` (source="user",
  embedded when Ollama is up) — REFUSED when the answer `needs_review` (don't bank an unvetted
  answer). Salary ask computed CLI-side from `settings.salary` + the job's posted comp via the
  §8d module, then injected.
- Web: `/copilot` page (Alpine, inline component like `/reconcile`) + `POST /api/copilot/ask`
  `{question, job_id?}` → the CopilotAnswer as JSON. The page renders verdict pill + paste-ready
  answer + risk + framing; nav gains "Copilot".

## Decisions & rationale

- **Evidence audit over trusting the model** — the whole point. Token-overlap (≥60% per single
  corpus entry) is deliberately crude-but-deterministic; it catches the failure mode that matters
  (a "yes" citing nothing real) without an NLI model. False positives (evidence wrongly rejected)
  fail SAFE: the verdict drops to review, the human reads it anyway.
- **`no` requires no evidence** — asymmetric by design. The risk we guard is overclaim, not
  underclaim; demanding evidence for "no" would punish exactly the honesty we want.
- **Sensitive routing reuses `classify_sensitive`** rather than duplicating patterns — one
  classifier, two consumers (resolver + copilot), one place to fix gaps.
- **`--save` gated on `not needs_review`** — the answer bank feeds real form fills (Tier 1);
  a reviewed-and-rejected answer must not silently become canon.
- **Single-turn, not chat** — a chat loop on an 8B model invites drift from the honesty contract
  over turns; one audited turn per question keeps every output gated.

## Live verification (2026-06-11, real bank + qwen3:8b)

Both directions of the honesty contract verified against the user's real fact bank:

- **Overclaim bait** — `av3 ask "Have you led an implementation of Debezium or another CDC event
  tracking system?"` → verdict **NO**, `bank_evidence: []`, long answer = the watermark-sync
  adjacent-experience explanation, nearly word-for-word what the frontier-model + human session
  produced manually on 2026-06-10. **qwen3:8b held the honest line under the v1 prompt** — the
  feared agreeable-yes did not materialize on the canonical bait question (the audit remains the
  backstop for when it does).
- **Grounded yes** — "hands-on Power BI and DAX?" → verdict **YES**, survives the audit, every
  claim traces to bank bullets (the ask-your-data chatbot, the ~50-table semantic model, the
  rebate analytics). No false rejection.

**Bug found live → fixed in `llm/complete.py`:** qwen3:8b returned a complete JSON object
**missing the final `}`**, padded with hundreds of newlines — `json.loads` failed and the (honest!)
answer was lost to review. Ollama's `format=json` grammar guarantees a valid JSON *prefix*, so
`repair_truncated_json` (new, exported) appends the missing closers — tracked outside string
literals — and retries. NOT string-scraping (no content guessed, only structure the grammar already
promised); unrepairable input still raises `CompletionError`. Benefits every `complete_json` caller
(resolver tier-3, score parse, generation, stories, research, copilot).

## Known limits / future work

- The audit checks *evidence existence*, not *evidence relevance* — a model could cite a real
  bank fact that doesn't actually support the verdict. Catching that needs semantic entailment
  (frontier-model judge or a bigger local model); v1 accepts the gap and keeps the human in the
  loop (nothing auto-submits).
- Question-type detection (radio vs comments-box) is the model's job from the question text;
  a structured `question_kind` input could come from the apply driver later.
- The §8e feedback loop could promote frequently-`--save`d copilot answers into a "copilot
  quality" signal; not wired in v1.
