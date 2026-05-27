# Fabrication Guard — Verifying LLM-Generated Résumés Against a Fact Bank

> **Goal:** Ensure a tailored résumé generated from a "master fact bank" (the user's
> real skills, work history, education, metrics) never introduces a company, title,
> date, credential, skill, or metric that isn't in the bank — and ideally flags
> subtler exaggeration (inflated scope, implied seniority).
>
> **Constraints (inherited from Auto Applier):** zero cost, local-first, runs on a
> consumer machine (RTX 3080 + 16 GB RAM target). LLM via Ollama; small classifier
> models acceptable if they run on CPU/GPU locally. The guard is **load-bearing** — a
> fabricated claim that slips through can get a user caught lying on an application.

The core design principle: **fabrication is a precision problem, not a recall-on-paraphrase
problem.** We do NOT need to understand every nuance of phrasing. We need to guarantee that
every *checkable fact* (entity, date, number, credential) traces back to the bank, and we
treat anything we can't verify as suspect. A layered guard — deterministic hard gate first,
fuzzy/semantic layers second — gives high precision on the things that matter and routes
the uncertain remainder to human REVIEW rather than auto-applying.

---

## 1. Natural Language Inference (NLI) / Textual Entailment

### What it is
NLI models classify a (premise, hypothesis) pair into **entailment / neutral /
contradiction**. For grounding, you set:
- **premise** = the source facts (the relevant slice of the fact bank, or the whole bank).
- **hypothesis** = a single generated claim/sentence from the résumé.

A generated claim is *grounded* only if the bank **entails** it. `neutral` means
"unsupported — the bank neither confirms nor denies it" (this is the dangerous case for
résumés — neutral ≈ probably fabricated). `contradiction` means the résumé directly
disagrees with the bank (e.g. wrong dates). This premise→hypothesis framing is the standard
recipe used across hallucination-detection literature: split the answer into sentences, use
each sentence as a hypothesis against the source as premise.

### Local/free models
All of these are open-weight, run locally via HuggingFace `transformers`, and are small
enough to run on CPU or a modest GPU (no API, no cost):

| Model | Base | Size | English NLI acc. | Notes |
|---|---|---|---|---|
| `cross-encoder/nli-deberta-v3-xsmall` | deberta-v3-xsmall | ~70 MB | lower | Fastest; good for a cheap pre-pass |
| `cross-encoder/nli-deberta-v3-small` | deberta-v3-small | ~140 MB | good | Solid CPU option |
| `cross-encoder/nli-deberta-v3-base` | deberta-v3-base | ~360 MB weights | strong | Common default |
| `cross-encoder/nli-deberta-v3-large` | deberta-v3-large | ~870 MB | strongest | Best precision, heavier |
| `MoritzLaurer/deberta-v3-base-mnli-fever-anli` | deberta-v3-base | 0.3B | strong | Trained on MNLI+FEVER+ANLI — FEVER is literally a fact-verification dataset, very on-point |
| `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | mDeBERTa-v3-base | 0.3B | 88.3% (EN), 80.8% multiling. avg | Multilingual; useful if résumés/JDs aren't English-only |
| `facebook/bart-large-mnli` | bart-large | ~1.6 GB | good | The classic zero-shot/NLI baseline |

`cross-encoder/nli-deberta-v3-*` outputs three logits (contradiction, entailment, neutral);
softmax → probabilities. `MoritzLaurer` variants order as (entailment, neutral,
contradiction). The MoritzLaurer FEVER/ANLI-trained variant is the most directly relevant
because FEVER's whole task is "is this claim supported by this evidence."

**DeBERTa-v3 caveat:** does not support FP16 inference (use FP32 or BF16); requires
`transformers >= 4.13`.

### How it's applied to grounding / hallucination detection
- Decompose the résumé into atomic sentences/bullets.
- For each, run NLI with the bank (or a retrieved subset of the bank) as premise.
- Flag any claim whose **entailment probability is below a threshold** OR whose
  **contradiction probability is high**. A common operating point: require
  `P(entail) >= 0.5–0.7` to accept; `P(contradiction) > 0.5` is a hard fail.
- NLI is the standard backbone for "is the answer supported by the source" — the task is
  closely related to hallucination detection, and DeBERTa-v3-large-mnli is repeatedly cited
  as the strongest general-purpose option.

### Precision/recall tradeoffs and pitfalls
- **Premise length matters.** NLI cross-encoders have a token limit (~512). You cannot
  shove the entire fact bank in as premise reliably. **Retrieve the relevant facts first**
  (embedding search, see §4) and use those as premise. A too-long or off-topic premise
  drives spurious `neutral`.
- **`neutral` vs partial support.** A résumé bullet that combines two true facts plus a
  connective phrase may come back `neutral` even though it's grounded. This produces
  *false positives* (flagging real claims). For a fabrication guard that's the safe
  direction — false positives just send a job to REVIEW, they don't auto-submit a lie.
- **NLI doesn't "know" your scope semantics.** "Led a team of 10" vs "worked on a team of
  10" can both look entailed by "team of 10" depending on the model. NLI catches *some*
  exaggeration (seniority verbs) but isn't reliable for it — pair with deterministic checks
  on numbers/titles.
- **Strength tradeoff:** xsmall/small are fast but miss subtle contradictions; large is the
  most reliable but ~5–10x slower. For a guard, use a small model as a cheap pre-pass and
  the large/FEVER model on anything that fails or is borderline.

---

## 2. Claim Extraction + Verification Pipelines

The general pattern: **decompose → verify each atomic claim → aggregate.** Established work:

### FActScore (Min et al., 2023)
Breaks a long generation into **atomic facts** (short, self-contained statements), then
checks each against a knowledge source, scoring the *fraction supported* (factual
precision). The original uses GPT for decomposition+verification, but the **decomposition
idea is the reusable part** — and decomposition can be done locally with a small LLM or even
rule-based sentence/bullet splitting. The key insight for us: *atomic granularity*. Verify
"Senior Analyst" and "2019–2022" and "Acme Corp" as separate facts, not one fused sentence.
Note: different decomposition strategies produce different atomic sets and that variance
affects score reliability — so keep decomposition deterministic where possible (résumés are
already semi-structured: one bullet = one-or-few claims).

### SelfCheckGPT (Manakul et al., 2023)
**Zero-resource, black-box**: sample the model multiple times at temperature; if samples
disagree with each other, the content is likely hallucinated (an inconsistent fact is an
invented one). Requires **no external database**. *But for our use case the fact bank IS the
ground truth*, so self-consistency is weaker than checking against the bank directly. Its
known failure modes (see §5) make it a supplement, not the core gate.

### RARR (Retrofit Attribution using Research and Revision)
Research-and-revise: generate verification questions, retrieve evidence, then **edit the
output to be attributable** to evidence while preserving style. Conceptually relevant: rather
than only flag fabrication, you could auto-revise an ungrounded bullet back toward what the
bank supports. For v3 this is an *optional auto-repair* step, not the gate.

### Chain-of-Verification (CoVe, Meta AI 2023)
The model **plans verification questions**, answers them independently, then produces a
revised, fact-checked response. On biography generation it raised FActScore from ~63.7 to
~71.4. It's a *self*-check (no external source), so same caveats as §5 — it reduces
hallucination but doesn't guarantee grounding to YOUR bank.

### FaStfact / FactSelfCheck / GraphCheck (2024–2025)
Newer long-form factuality evaluators: faster decomposition, fact-level (not just
sentence-level) black-box checks, and knowledge-graph-powered verification. Mostly research;
the takeaway is the field has converged on **atomic-claim decomposition + per-claim
verification against evidence** as the reliable recipe.

### Feasibility / tradeoffs
- Fully local: decomposition via Ollama or rule-based; verification via local NLI + entity
  matching. No paid API needed.
- Cost is per-claim LLM/NLI calls — fine for one résumé at a time; cache aggressively (the
  project already has a 72h SHA-256 LLM cache).
- Precision depends almost entirely on **good decomposition** + **good evidence retrieval**.
  Résumés are easier than free-form bios because they're already chunked into bullets.

---

## 3. Entity / Structured Grounding (the deterministic, high-precision layer)

This is the **most important layer for résumés** and the one that should be the hard gate.
Résumé facts are overwhelmingly structured: company names, job titles, employment dates,
degrees, certifications, skill keywords, and numeric metrics. These can be matched
**deterministically** against the structured fact bank — no model needed, ~100% precision on
the categories it covers.

### Approach
1. **Structure the bank** (it already should be): companies, titles, date ranges,
   degrees/credentials, skills list, and a set of allowed metrics ("managed $2M budget",
   "team of 10").
2. **Extract entities from the generated résumé**:
   - **Skills:** spaCy `EntityRuler` / `PhraseMatcher` over a controlled skill vocabulary —
     deterministic, fast, exact/lemma match. Any skill token in the résumé that isn't in the
     bank's skill set = hard fail. (spaCy's pretrained NER isn't domain-tuned for skills; the
     ruler/phrase-matcher pattern is the reliable approach.)
   - **Companies / titles / degrees:** match against the bank's known values with
     normalization + fuzzy match (RapidFuzz `token_sort_ratio`) to tolerate formatting
     ("Sr. Analyst" vs "Senior Analyst") while still catching invented employers.
   - **Dates:** regex/`dateparser` for year and month ranges; every date range in the résumé
     must fall within a known employment/education span in the bank. A new or stretched date
     = hard fail.
   - **Numbers/metrics:** regex for `$`, `%`, "team of N", "Nx", "N years". Every metric must
     match an allowed metric in the bank (exact or within a small tolerance). An invented or
     inflated number = hard fail. This is the single highest-value check because fabricated
     impact metrics are the most common and most damaging résumé lie.
3. **Allow-list, not block-list.** The guard verifies that each extracted entity *exists in
   the bank*, rather than scanning for known-bad phrases. Anything not present is unverified
   → suspect.

### Local/free feasibility
100% local and free. spaCy (`en_core_web_sm/md`), RapidFuzz, `dateparser`, and regex are all
pip-installable, CPU-only, millisecond-fast. No GPU, no API.

### Precision/recall tradeoffs
- **Precision: extremely high** for the categories it covers — if a company isn't in the
  bank, it's genuinely not the user's. Near-zero false negatives on *novel* entities.
- **Recall limitation:** it only checks the *categories it knows about*. It won't catch a
  fabricated qualitative claim with no entity/number ("excellent communicator across
  cross-functional teams"). That's what NLI/self-check layers (§1, §5) cover.
- **Tuning:** fuzzy thresholds need care — too loose lets "Acme Corp" match "Acme Corp
  Holdings"; too tight rejects legitimate abbreviations. Normalize (lowercase, strip
  punctuation, expand common abbreviations) before matching, and log near-misses to REVIEW.

---

## 4. Embedding Similarity / Semantic Overlap (cheap pre-filter)

### What it is
Encode each generated bullet and each fact-bank entry with a sentence-embedding model
(`sentence-transformers`, e.g. `all-MiniLM-L6-v2`, ~80 MB, CPU-fast) and compute cosine
similarity. Used two ways:
1. **Retrieval** — for each generated bullet, pull the top-k most similar bank facts to use
   as the **NLI premise** (this is what makes §1 tractable given the 512-token limit).
2. **Coarse pre-filter** — a bullet whose best similarity to *any* bank fact is very low
   (e.g. cosine < ~0.3) is almost certainly unsupported; flag/route to REVIEW without even
   running NLI.

### Local/free feasibility
Fully local, free, fast. `all-MiniLM-L6-v2` or `bge-small-en` run on CPU in milliseconds.
Embeddings of the (small) fact bank can be precomputed once and cached.

### Precision/recall tradeoffs / pitfalls
- **Semantic ≠ factual.** High cosine similarity means "talks about the same topic," NOT
  "is supported." "Managed a team of 5" and "Managed a team of 50" are near-identical in
  embedding space but one is a lie. **Embeddings must never be the gate** — they're a
  retrieval + cheap-reject tool only. The deterministic layer (§3) handles the number.
- Good **recall** as a pre-filter (rarely misses topical overlap), poor **precision** as a
  truth check. Exactly why it sits in front of NLI/entity checks, not in place of them.

---

## 5. LLM Self-Check Prompting

### What it is
Ask the (local) LLM to verify its own output against the source: "Here is the fact bank.
Here is the generated résumé. List any statement not supported by the bank." Or the
structured CoVe variant: generate verification questions, answer each from the bank, revise.

### Local/free feasibility
Free and local via Ollama. Cheap to run on one résumé. Can reuse the existing prompt/cache
infrastructure. CoVe-style decomposition is well-suited to a local small model.

### Reliability and pitfalls (important — this is why it can't be the only gate)
- **Self-consistency false negatives:** if the model *consistently* invents the same false
  fact, self-checking won't catch it — it agrees with itself. A confidently-wrong claim
  sails through.
- **False positives:** higher-temperature self-check samples can contradict a *correct*
  original, flagging good content. Noisy.
- **Inherent ceiling:** black-box self-consistency methods have been shown to sit near a
  supervised-oracle ceiling — little headroom — and **cannot detect failures that violate a
  constraint space** (e.g. "this number must equal the bank's number") as opposed to
  token-probability uncertainty. Résumé fabrication is largely a *constraint* violation, so
  self-check is structurally the wrong tool for the hard cases.
- **Small local models are weaker self-critics** than GPT-4-class models the papers used;
  expect degraded reliability with Gemma-class local models.

**Verdict:** use self-check as a *soft, supplementary* layer for the qualitative claims that
entity-matching and NLI can't reach (tone, implied seniority, vague scope), and to draft a
human-readable "why this was flagged" explanation. Never let it override the deterministic
gate, and never trust it to *clear* a claim the entity/date/number layer flagged.

---

## Recommended Fabrication-Guard Design

A **layered, fail-closed pipeline.** Each résumé bullet/sentence passes through cheap→
expensive layers; **any unsupported checkable claim routes the whole job to REVIEW (never
auto-apply)**, and any direct contradiction is a hard block.

```
Master Fact Bank (structured: companies, titles, date ranges,
                  degrees, certs, skills[], allowed metrics[])
                          │
        ┌─────────────────┴─────────────────┐
        │  Generate tailored résumé (Ollama) │
        └─────────────────┬─────────────────┘
                          │  decompose into atomic bullets/claims
                          ▼
  LAYER 0 — Decomposition (deterministic + light LLM)
    Split résumé into bullets; within each, isolate entities/dates/numbers.

  LAYER 1 — DETERMINISTIC HARD GATE  (spaCy ruler + RapidFuzz + dateparser + regex)
    • Every SKILL in résumé ∈ bank.skills            → else HARD FAIL
    • Every COMPANY/TITLE/DEGREE/CERT matches bank    → else HARD FAIL
      (normalized + fuzzy; near-miss → REVIEW, not pass)
    • Every DATE range ⊆ a known bank span            → else HARD FAIL
    • Every NUMBER/METRIC matches an allowed metric    → else HARD FAIL
      (exact or small tolerance — invented/inflated number = fail)
    100% local. Highest precision. Catches the lies that get people fired.

  LAYER 2 — EMBEDDING PRE-FILTER  (all-MiniLM-L6-v2, cached bank embeddings)
    For each surviving bullet, retrieve top-k bank facts (→ NLI premise).
    If best cosine < ~0.3 → unsupported → REVIEW.

  LAYER 3 — NLI ENTAILMENT  (MoritzLaurer deberta-v3-base-mnli-fever-anli
                             or cross-encoder/nli-deberta-v3-large)
    premise = retrieved bank facts; hypothesis = the bullet.
    • P(contradiction) > 0.5  → HARD FAIL
    • P(entail) < ~0.6        → REVIEW (unsupported / neutral)
    Catches phrasing-level grounding the entity layer can't see.

  LAYER 4 — LLM SELF-CHECK  (Ollama, soft layer, optional auto-repair)
    Ask model to list unsupported/exaggerated phrases vs the bank;
    surface as human-readable REVIEW notes. May suggest a grounded rewrite
    (RARR-style). NEVER clears a Layer-1/3 fail; advisory only.
                          │
        ┌─────────────────┴─────────────────┐
        │ All layers clean → ELIGIBLE to auto-apply │
        │ Any HARD FAIL    → BLOCK + regenerate     │
        │ Any REVIEW flag  → route job to REVIEW    │
        └───────────────────────────────────────────┘
```

### Decision rules
- **HARD FAIL (block + regenerate, or drop the bullet):** invented company/title/degree/cert,
  out-of-range date, invented/inflated number, NLI contradiction. These are unambiguous lies.
- **REVIEW (do not auto-apply; show user):** entity near-miss, low embedding overlap,
  NLI `neutral`/low-entail, self-check flag. The existing `USER_REVIEW` routing already
  exists in `scoring/scorer.py` — reuse it: an ungrounded claim drops the job's
  auto-apply eligibility to REVIEW regardless of its match score.
- **PASS:** clears all layers.

### Why layered, in this order
- **Cheap, deterministic, highest-precision checks run first** and catch the most damaging
  fabrications (numbers, dates, employers) with near-zero false negatives and no model
  needed. This is the load-bearing gate.
- **Embedding retrieval** makes NLI feasible (premise stays under the token limit) and
  cheaply rejects wholly-unsupported bullets.
- **NLI** adds phrasing-level grounding that pure entity matching misses, and is the only
  layer that reliably flags direct contradictions of supported facts.
- **Self-check** mops up qualitative exaggeration the structured/NLI layers can't formalize,
  and produces explanations — but is never trusted to clear a flagged claim, because
  self-consistency methods structurally cannot catch confident, constraint-violating lies.

### What runs locally for free (all of it)
- Layer 1: spaCy + RapidFuzz + dateparser + regex — CPU, pip, free.
- Layer 2: `sentence-transformers` MiniLM/bge-small — CPU, free, cached.
- Layer 3: HuggingFace `transformers` DeBERTa-v3 NLI — CPU-capable, small (~0.3 GB),
  faster on the RTX 3080; free open weights. (DeBERTa-v3 = FP32/BF16, not FP16.)
- Layer 4: existing Ollama LLM + 72h SHA-256 cache — free, local.

No paid API, no cloud, fully within the project's zero-cost / local-first constraints.

### Implementation notes for v3
- Keep the **fact bank strictly structured** — the deterministic gate's power scales directly
  with how well the bank enumerates skills, date spans, and *allowed metrics*. Maintain an
  explicit `allowed_metrics` list (each impact number the user actually owns).
- **Bias toward REVIEW.** False positives cost the user a manual review click; false
  negatives can cost them a job offer / reputation. Fail closed.
- **Cache** NLI and embedding results keyed on (bank-version, bullet) — re-tailoring the same
  résumé for similar JDs reuses verdicts.
- **Log every flag with its reason** so the user (and the refine loop) can learn which
  generation prompts tend to fabricate, and tighten the generation prompt over time.

---

## Sources

- [Hallucination Detection: NLI, Self-Consistency & Learned Models — Michael Brenndoerfer](https://mbrenndoerfer.com/writing/hallucination-detection)
- [Understanding and Mitigating LLM Hallucinations — Towards Data Science](https://towardsdatascience.com/understanding-and-mitigating-llm-hallucinations-be88d31c4200/)
- [Chain of Natural Language Inference for Reducing LLM Ungrounded Hallucinations (arXiv 2310.03951)](https://arxiv.org/pdf/2310.03951)
- [Explainable Hallucination through Natural Language Inference (ACL 2025 Findings)](https://aclanthology.org/2025.findings-acl.96.pdf)
- [FActScore: Fine-grained Atomic Evaluation of Factual Precision (EmergentMind summary)](https://www.emergentmind.com/topics/factscore)
- [SelfCheckGPT: Zero-Resource Black-Box Hallucination Detection (arXiv 2303.08896)](https://arxiv.org/pdf/2303.08896)
- [SelfCheckGPT topic overview — EmergentMind](https://www.emergentmind.com/topics/selfcheckgpt)
- [SAC3: Reliable Hallucination Detection in Black-Box LLMs via Semantic-aware Cross-check (arXiv 2311.01740)](https://arxiv.org/html/2311.01740v2)
- [Verify when Uncertain: Beyond Self-Consistency in Black-Box Hallucination Detection (arXiv 2502.15845)](https://arxiv.org/pdf/2502.15845)
- [Chain-of-Verification Reduces Hallucination in LLMs — OpenReview](https://openreview.net/forum?id=VP20ZB6DHL)
- [Chain of Verification (CoVe) — LearnPrompting](https://learnprompting.org/docs/advanced/self_criticism/chain_of_verification)
- [FaStfact: Faster, Stronger Long-Form Factuality Evaluations in LLMs (arXiv 2510.12839)](https://arxiv.org/html/2510.12839)
- [FactSelfCheck: Fact-Level Black-Box Hallucination Detection (arXiv 2503.17229)](https://arxiv.org/pdf/2503.17229)
- [GraphCheck: Knowledge-Graph-Powered Fact-Checking (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12360635/)
- [cross-encoder/nli-deberta-v3-base — Hugging Face](https://huggingface.co/cross-encoder/nli-deberta-v3-base)
- [cross-encoder/nli-deberta-v3-large — Hugging Face](https://huggingface.co/cross-encoder/nli-deberta-v3-large)
- [cross-encoder/nli-deberta-v3-small — Hugging Face](https://huggingface.co/cross-encoder/nli-deberta-v3-small)
- [MoritzLaurer/mDeBERTa-v3-base-mnli-xnli — Hugging Face](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli)
- [facebook/bart-large-mnli — Hugging Face](https://huggingface.co/facebook/bart-large-mnli)
- [Sentence Transformers — Semantic Textual Similarity docs](https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html)
- [sentence-transformers (GitHub)](https://github.com/huggingface/sentence-transformers)
- [Named Entity Recognition — extract skill entities from resumes using spaCy (Medium / HR AI)](https://medium.com/hr-ai/named-entity-recognition-how-to-extract-skill-entities-from-resumes-using-spacy-865476b5771e)
- [Named entity recognition in resumes (arXiv 2306.13062)](https://arxiv.org/abs/2306.13062)
- [Aman's AI Journal — Factuality in LLMs](https://aman.ai/primers/ai/factuality-in-LLMs/)
