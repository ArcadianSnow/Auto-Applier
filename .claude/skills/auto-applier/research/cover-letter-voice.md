# Cover-letter voice — the canonical definition (BUILD 5)

**Status:** DEFINED 2026-06-15, refined same day to `gen-cover-v4` (the I-list fix — see the
v4 section at the bottom). Encoded in `GENERATE_COVER_LETTER` (`gen-cover-v4`) +
the deterministic backstops in `auto_applier/resume/cover_autogen.py`. This is the
single source of truth for what a generated cover letter must sound like.

The user delegated the voice definition ("define the voice"); the no-AI-tells bar is
his ([[feedback_writing_voice_no_ai_tells]]). This doc records the decisions so a future
session changes the voice *here + in the prompt version*, not by re-deriving from chat.

## The voice in one line

A competent person stating plainly what he has actually done and how it maps to the role.
Not a brochure. Honest before helpful. Short, varied sentences. No AI tells.

## The rules (each maps to a guard)

| # | Rule | Where enforced | Strength |
|---|------|----------------|----------|
| 1 | **No em-dash / en-dash** (the #1 tell) | `_strip_ai_tells` deterministic strip → comma, AND the prompt | HARD — a dash can never ship regardless of model |
| 2 | **First person throughout** — never the candidate's name or "He/His" as a subject; first sentence starts with "I" or "At/When/After <company>, I" | prompt (emphatic) + `_opens_in_third_person` detect → **regenerate once** in `generate_one` | HARD-ish — qwen3 drifted to third person ~25% before the fix; prompt + one retry makes a shipped third-person letter negligible |
| 3 | **No "excited / thrilled / passionate / delighted / enthusiasm"** in any form | prompt (categorical family ban) | prompt |
| 4 | **Anti-overclaim / honesty** — never claim an experience, responsibility, domain, skill, or soft capability the bank doesn't show | prompt (the guard only vets *technical* claims, so the prompt is the ONLY thing stopping invented *soft* claims) | prompt — the honesty crux |
| 5 | **No buzzwords** (leverage, synergy, proven track record, spearheaded, "I'm confident my", …) | prompt list | prompt |
| 6 | **No rule of three** — no stacked triple adjectives/phrases | prompt | prompt |
| 7 | **Vary sentence openings** — no monotone "I did X. I did Y. I did Z." run; at most ~one sentence opens with "I" | prompt (hard cap + BAD/GOOD example) + `_excessive_i_openings` scores the draft and `generate_one` keeps the **less-monotone of two attempts** | **HARD-ish (v4)** — was the user's explicit complaint (v3 shipped 6 "I [verb]" sentences in a row). v4 + the backstop fixed it; a monotone letter is never *skipped* (still honest), just deprioritized vs the retry |
| 8 | **Don't parrot the JD's marketing adjectives** (scalable, robust, seamless, innovative, cutting-edge, world-class) as descriptions of the company/role/needs/your contribution. A bank fact that literally contains the word (e.g. "scalable upsert frameworks across 190+ tables") is the one allowed use | prompt | mostly works; an occasional "scalable data pipelines" still slips. Minor |
| 9 | **Exactly 3 short paragraphs** (hook / 1-2 accomplishments / short close) | prompt + `_ensure_paragraphs` deterministic regroup when the model returns one block | HARD — the backstop splits a dense block (first=hook, last=close, rest=middle), only when the model didn't paragraph it itself |
| 10 | **Concise (150-250 words)** — length is room to drift | prompt | prompt |
| 11 | Deliver as **.docx** (md doesn't paste into web fields) | `render_cover_letter_docx` | HARD |

## Why some rules are deterministic and others are prompt-only

- **Dashes (#1), 3-paragraph shape (#9)** are deterministic because the mechanical fix is
  content-preserving (dash→comma; insert paragraph breaks at sentence boundaries) and can't
  mangle meaning. **Third person (#2)** is detected mechanically (name/He/His opener) and cured
  by a single regenerate, because rewriting subjects in place would mangle grammar.
- **Voice/honesty rules (#3–#8, #10)** can't be fixed by blind substitution without wrecking
  meaning, so they live in the prompt. They're softer: a local model drifts. The mitigation
  is the **sample-audit gate** (regenerate the canonical near-bank + far-from-bank samples
  and eyeball them after any voice change) and the **fabrication guard** for tech claims.

## Residual risks the user should know (don't pretend these are solved)

- **Far-from-bank overclaim (#4).** On Solutions / Value-Engineer roles the model reaches for
  the role's language. Verified on Cockroach (Value Engineer): an early draft said "ROI models"
  ("ROI" is NOT in the bank) and "executive financial storytelling". Joseph DOES have
  finance-adjacent bank facts (Atyeti, forecasting, cost-impact, 100+ stakeholders, VBA, 66%),
  so it's a stretch of a real area, not pure invention — but the tech-only guard can't catch
  the word "ROI". **Skim far-from-bank letters before sending.** The honest close pattern
  ("these experiences align with <company>'s need for X") is the right bridge and the model
  uses it; watch for it sliding into a flat claim of having done X.
- **Sentence I-runs (#7)** and **occasional adjective parrot (#8)** persist at a low rate.
- **n8n-style typos.** qwen3 once mangled "n8n" → "n8," — a rare token artifact; not guarded.
  These letters are drafts "ready just in case", polished by the user before external use.

## The two canonical audit samples (regenerate after ANY voice change)

- **Near-bank:** cube, Data Engineer (job `982f047e…`) — should read naturally; his bank fits.
- **Far-from-bank:** aircall, Forward-Deployed/AI-Solutions (job `940a9071…`) — the honesty
  stress test. v1→v2 caught qwen3 *inventing* Solutions experience (financial modeling, TCO
  models, exec presentations to CTOs/CFOs). The anti-overclaim clause (#3) must keep it honest.

Regenerate both with `av3 cover --generate <id> --force` (export `AV3_DATA_DIR` first), then
audit against the table above before trusting a bulk run.

## How to change the voice

1. Edit `GENERATE_COVER_LETTER` in `auto_applier/llm/prompts.py` and **bump the version**
   (`gen-cover-vN` → `vN+1`) — the version is the contract the voice test asserts.
2. Update the voice-contract assertions in `tests/test_cover_autogen.py`.
3. Regenerate + audit the two canonical samples.
4. Update this doc's rule table.

## Big-JD "timeouts" were really qwen3 DEGENERATION — three layers

The root cause was misdiagnosed at first as slow reasoning. Measured 2026-06-15: the Mistral
"Forward Deployed ML Engineer" JD with thinking ON and a 600s timeout ran **328s and produced
208,079 chars** — a repetition loop ("I've built… I've implemented…" hundreds of times). The
180s "timeout" was just that loop being cut off. /no_think loops too (faster: ~20K chars). So
**raising the timeout makes it WORSE** (a bigger monstrosity); the real fix is to detect and
reject the degenerate output. Three layers, in order:

1. **JD-head trim** (`_trim_jd_for_cover`, `_COVER_JD_MAX_CHARS=4000`) — a cover letter only
   references 1-2 front-loaded requirements. Took Cockroach (8015 ch) from a >180s timeout to
   **3.7s**. Genuinely fixes the *slow-but-fine* cases.
2. **`/no_think` fail-safe retry** — `generate_one`'s 2nd attempt appends qwen3's `/no_think`.
   It is a coin-flip on looping JDs: it gave a CLEAN Mistral-Montreal letter in ~2s but a
   degenerate Mistral-Singapore one. Kept because it recovers the clean cases fast; the bad
   ones are caught by layer 3. First attempt keeps thinking on (matches the bulk of letters).
3. **Degeneracy guard** (`_is_degenerate`) — the actual safety net, three checks, tuned against a
   live 469-letter batch (p95 1292 ch / 5-7 sentences for clean letters):
   - **>2000 chars** (250-word target ≈ 1700 ch; catches loops + padding),
   - **>12 substantial sentences** (a wall of 13+ "I built… I designed…" is a list-dump, not a
     letter — qwen3's far-from-bank failure mode; the sentences are DISTINCT so the repeat check
     misses it; 12 is ~2x the largest clean letter so it never clips a real one),
   - **any substantial sentence repeated** (doubled-sentence padding under the length cap).
   The fab guard PASSES all of these (every claim is bank-supported) and the dash/paragraph
   backstops don't check length, so this guard is the only thing between a degenerate generation
   and a shipped monstrosity. A degenerate final draft → `SKIPPED_DEGENERATE` (no letter, like a
   guard-skip); does NOT exit non-zero (expected, safe).

Net: a looping JD costs ~180s (thinking attempt) + a fast /no_think retry, then either a clean
letter or a clean rejection. It NEVER ships a runaway or list-dump letter.

**Sweep after any bulk run that predates a guard change.** The first 525-job drain ran before the
guard existed; a post-drain sweep (`_is_degenerate` over every `uploads/<id>/*Cover Letter.docx`,
measuring BODY only — exclude the name/contact/greeting/closing, ~120 ch) found and deleted 17
over-length + 10 list-dump letters, then `av3 cover --generate-all` regenerated them with the
guard active. A few far-from-bank ML/FD JDs (Mistral, some elevenlabs) repeatedly hit the guard
and end up letterless — that is the correct outcome (qwen3:8b can't write a tight honest letter
for them; better none than a wall of text).

## v4 (2026-06-15, session 2): the I-list fix, the parroting trap, and the qwen3:8b ceiling

User feedback on a shipped v3 letter: *"I am not sure this is my voice as it just keeps saying
I did this I did this I did this."* The v3 prompt ALREADY told the model to vary openings and
use "one or two accomplishments" — qwen3 ignored both and crammed 6 "I [verb]" sentences into
paragraph 2. **Same lesson as third-person and degeneracy: soft style rules don't hold with
qwen3:8b at greedy temp 0; they need deterministic teeth.**

What v4 changed:
- **Prompt:** hard "at most ONE sentence may begin with 'I'" cap; "AT MOST TWO accomplishments
  (two, not six; a letter, not a résumé)"; "pick the OPENING by relevance to THIS job so letters
  don't all start the same way"; "plain words, not a spec sheet" (no parenthetical tech dumps, no
  colon feature-lists); "the close makes no new claim".
- **Backstop:** `_excessive_i_openings` (>60% of a multi-sentence letter opens with "I", OR 3+ in
  a row) + `generate_one` now does **best-of-two**, keeping the draft with the lowest
  `(degenerate, third_person, i_ratio)` key. Monotony never *skips* a letter (still honest); it
  just prefers the better retry.

**TRAP — a concrete GOOD example gets copied verbatim.** v4's first cut put a concrete GOOD
example built from his real facts in the prompt ("At Neurolens I rebuilt a legacy SQL authoring
tool…"). qwen3 then opened EVERY letter with that exact sentence — a worse, role-blind canned
opening. Fix: the GOOD example must be a **non-copyable placeholder skeleton**
(`<At <company>>, <the most role-relevant thing he did>…`) + the explicit relevance rule. After
that, openings diverged correctly by role. **Never put copy-pasteable real content in a prompt
example for a weak local model.**

**The qwen3:8b voice ceiling — stop prompt-stacking here.** After 3 prompt iterations the
residuals on FAR-FROM-BANK roles (Solutions/Sales/AI Engineer) did not converge; each fix traded
for a regression elsewhere: aspirational filler the anti-overclaim rule can't kill ("I am eager
to…", "positions me to contribute…"), banned buzzwords reappearing ("dynamic", "fast-paced",
"leverage") despite the explicit ban, spec-sheet density only partly suppressed. These
concentrate on roles he's a weak fit for anyway. Treat them as hand-edit drafts; do NOT keep
stacking prompt rules (diminishing returns, model-capability bound).

**NEW failure mode — meta-response hallucination (NEEDS a guard, not yet built).** On the
Databricks "Database Engine Internals" JD, qwen3:8b returned, as the letter body: *"Joseph, your
resume is impressive, but we need to see more about your experience with distributed systems…
Could you please provide additional details?"* — it role-played the recruiter. This PASSES every
current guard (no fabricated tech, not over-length, doesn't open with He/His — it opens "Joseph,")
and **would ship**. Needed: a deterministic guard that rejects a body addressing the candidate by
name, asking the candidate questions, or written in second person ("your resume") → SKIPPED.

**qwen3:14b experiment — don't swap.** Temporarily set `ollama_model` to qwen3:14b (pulled
locally) and regenerated the 3 problem letters. It FIXED the Databricks meta-response (real
letter) but ERRORED with empty bodies on 2 of 3 (new reliability failure) and STILL produced
aspirational filler + "leveraging". A bigger model does not solve the voice problem and costs
reliability + speed. Reverted to qwen3:8b. The ceiling is not primarily a model-size problem.
