# Phase -1 — Conclusions & Go/No-Go (2026-05-26)

Consolidated read of the three Phase -1 risk investigations. Source files in this folder:
`ats-discovery-seeding.md`, `ats-form-automation.md`, `fabrication-guard.md`. Spec cross-ref: `docs/v3-architecture.md` §11.

## Verdict: **GO** to Phase 0 → Phase 1 vertical slice — with one decisive unknown to measure.

Three of the four top risks are **retired with concrete, zero-cost, local solutions**. The fourth
(form automation) is **reshaped, not blocking**: auto-apply is real, but its *reach* hinges on one number we
can only get by building the slice.

| Risk | Status | One-line resolution |
|---|---|---|
| ① ATS company-list seeding | **RETIRED** | Public URL slugs + public read APIs; seed from MIT datasets (OpenJobs, jobhive) + confirm-probe; scale via dork harvest |
| ③ Fabrication guard | **RETIRED** | Layered fail-closed: deterministic entity/date/number match (L1, near-100% precision) → ST retrieval (L2) → local NLI (L3) → Ollama self-check notes (L4); bias to REVIEW |
| ④ Submit confirmation | **RETIRED** | Positive on-page signal only (GH `/confirmation`, Lever `/thanks`, Ashby in-place panel + `success:true` XHR); never email-alone; else REVIEW |
| ② Cross-ATS form automation | **RESHAPED** | Mechanics friendly (native file inputs, stable selectors); **invisible behavioral CAPTCHA is the real auto-vs-assisted gate**; Ashby is a React SPA/XHR (trickiest) |

## The one number that defines the project's value prop

**Invisible-CAPTCHA auto-pass rate** on real Greenhouse postings — how often `patchright + real Chrome +
human-like behavior` clears the invisible challenge *without* a visible one.

- **High** → auto-apply is real; the throughput thesis holds; build the platform as specced.
- **Near-zero** (esp. Greenhouse *Enterprise* reCAPTCHA) → v3 is really "great discovery + résumé generation
  + **assisted** apply." Still valuable — and the architecture already handles it (detection-risk router →
  assisted; badge-only review queue) — but it's a different pitch and changes how we set user expectations.

This is **the explicit primary success metric of the Phase 1 vertical slice.** Everything else in Phase 1
(seed list, score, generate, guard, confirm) is plumbing around measuring this.

## What Phase 1 (Greenhouse vertical slice) must do

1. Seed ~50–200 Greenhouse tokens from `outscal/OpenJobs` (filter to target titles), confirm-probe live.
2. Discover → fetch full JD → score vs master profile → generate résumé → **run the L1 fabrication guard**.
3. Auto-fill the Greenhouse form (IDs: `#first_name`, `#email`, `#resume` via `set_input_files`), discover
   custom questions at runtime by reading labels.
4. Attempt submit; **record whether the invisible CAPTCHA passed, a visible challenge appeared (→ assisted),
   or it failed**; detect confirmation via `/confirmation` redirect.
5. **Report the auto-pass rate, confirmation reliability, and guard reject rate.** These three numbers decide
   how the rest of v3 is scoped.

## Spec changes made from these findings

- §11 risk table → added "Phase -1 research outcome" block (this verdict, condensed).
- §8b confirmation rule → corrected to "never APPLIED off email alone" + per-ATS signals.
- §8c anti-detect → noted invisible CAPTCHA is *why* maximal stealth is load-bearing (stealth ≈ auto rate).
- New known constraint: **Ashby = React SPA, no `<form>`, XHR submit** — plan its adapter accordingly.

## Open items deferred (not blocking Phase 0/1)

- Market-salary source (BLS OES / Adzuna) — research at v3.1 (§8d).
- Lever/Ashby-specific selector + confirmation hardening — Phase 2 (slice is Greenhouse-only).
