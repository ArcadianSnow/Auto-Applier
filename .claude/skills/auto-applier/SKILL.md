---
name: auto-applier
description: Single source of truth + working discipline for the Auto Applier v3 rebuild. Routes to the architecture spec, the Phase -1 research findings, and the reliability/anti-stuck rules that keep the build on track. Invoke at the start of any Auto Applier work session, or when asked about v3 design decisions, risks, ATS specifics, or "how should this work".
user-invokable: true
---

# Auto Applier — v3 knowledge base & discipline

Single source of truth for the **Auto Applier v3** rebuild. Routes a question to the right doc/research
instead of re-deriving from scratch. **Read this first when starting an Auto Applier session.**

## Context

- **v3 is a ground-up rewrite** (decided 2026-05-26). v2 code = lessons, **not** a base to extend.
- **v3.0-core (phases 0–5) COMPLETE (2026-05-29); Phase 6 / v3.1 COMPLETE (2026-06-11).** 944 tests green.
  All planned sub-phases shipped — strategy profiles, salary intelligence, outcome feedback loop,
  reconciliation (CLI + the `/reconcile` web conversation), learn trends, branded UI, story bank, company
  research, the manual/human-apply mode, and the **application copilot** (§8f, shipped 2026-06-11 as the
  first post-plan scope). **There is no planned backlog**; new work is new scope — see spec §11b.
- **`CLAUDE.md` is now v3-first** (rewritten in Phase 5 6/M); it describes the `av3/` package directly.
- Repo: this checkout's root (the `Auto Applier` working tree).
- **The spec** (authoritative): `docs/v3-architecture.md`. Every design decision and its rationale lives there.
- Working memory: your Claude projects memory dir for this repo (`~/.claude/projects/<this-project>/memory/`) — start with [[project_v3_rewrite]].
- For long iterative/debug sessions, **also invoke the `unstuck` skill** at session start.

## Where to look — decision tree

| If the question is about... | Read |
|---|---|
| Overall architecture / any design decision | `docs/v3-architecture.md` (the spec) |
| Why a decision was made / what's deferred (v3.0 vs v3.1) | `docs/v3-architecture.md` §11 + [[project_v3_rewrite]] |
| The named risks + their mitigations | `docs/v3-architecture.md` §11 |
| Build order / which phase we're in | `docs/v3-architecture.md` §11b |
| **Phase -1 verdict / go-no-go / what Phase 1 must measure** | `research/_phase-minus-1-conclusions.md` |
| How to seed ATS company lists (board tokens) + **the wired discovery producer** (`av3 discover`, `DiscoverWorker`, `canonical_hash`, scheduler head) | `research/ats-discovery-seeding.md` |
| How ATS apply forms behave / CAPTCHA / submit confirmation (+ live survey results) | `research/ats-form-automation.md` |
| Phase 3 pipeline staging (embedding pre-filter, score/optimize workers, scheduler) | `research/pipeline-staging.md` |
| Phase 4 web UI + worker service (FastAPI, SchedulerService, dashboard, onboarding) | `research/web-ui-and-service.md` |
| Phase 5 observability CLI (errors/stats) + telemetry mirror + relay + installer | `research/observability-and-distribution.md` |
| Phase 6 / v3.1 sub-phases — per-job résumé rewire, strategy profiles, salary intel, feedback loop, analytics, branded UI + `/reconcile`, story bank, company research | `research/phase6-v3.1.md` |
| Prior art — other auto-apply tools/repos, methodologies, what we adopt + smoketests (reCAPTCHA v3 score, JobSpy) | `research/prior-art-and-methodology.md` |
| ATS market share by segment + what tier v3 can reach + source prioritization | `research/ats-market-landscape.md` |
| How to stop résumé fabrication (the guard) | `research/fabrication-guard.md` |
| Cover-letter voice / no-AI-tells rules / cover autogen (BUILD 5, `av3 cover --generate[-all]`) | `research/cover-letter-voice.md` (+ `research/automated-apply-next-build.md` BUILD 5) |
| Résumé model (fact bank → per-job generation) | `docs/v3-architecture.md` §6b |
| Manual / human-apply mode (`av3 shortlist`/`applied`/`pass`, job-family classifier, DECIDED→APPLIED) | `research/manual-apply-mode.md` |
| Application copilot (`av3 ask`, `/copilot`, the evidence audit / honesty gate, sensitive routing, the **freeform/essay DRAFT path** for open-ended questions — BUILD 6) | `research/application-copilot.md` |
| **Automated apply GO-LIVE** — first real `--no-dry-run` submissions, blockers (human-attestation gate, hand-crafted résumé), why Solutions roles assist-pend, the watched plan | `research/automated-apply-go-live.md` |
| **Automated apply NEXT BUILD** — forward plan after the field-fill overhaul: cover-letter upload (`#cover_letter`), Tailscale enumerated dropdowns, the diagnostic validation path | `research/automated-apply-next-build.md` |
| **FUTURE DIRECTIONS (v3.2+ product-ization)** — the 4 forward bets w/ pros/cons/concerns/plans: (1) conversational onboarding + goals→targeting→slugs (and the honest "can a local Ollama model do web research" answer: use the bundled 86k-company dataset, LLM bounded), (4) local-first email outcome loop (IMAP app-password), (3) Greenhouse security-code stand-down test, (2) dashboard overhaul | `research/future-directions.md` |
| **MVP readiness / first-run setup friction** — non-technical-friend gaps + what shipped 2026-06-20: Ollama setup (`av3 setup-llm`), the onboarding `resume.pdf` cliff (gate relaxed to fact-bank-only), guided email setup (wizard step + `/api/onboarding/inbox`), company-bank refresh cadence + daily-discovery Task Scheduler helper, the `av3.cli.main` launcher fix | `research/mvp-readiness.md` |
| **EXE distribution viability + onboarding restructure** (RESEARCH, pre-build) — can a non-tech friend double-click an exe? Verdict: **embedded-Python + pip via Inno installer**, NOT PyInstaller (patchright has no PyInstaller hook). The real work is restructuring first-run setup (no model-pull/browser orchestration exists today) into a guided in-app flow. SmartScreen/signing reality, phasing | `research/exe-distribution-viability.md` |
| **Onboarding/setup restructure — Phase 1 SHIPPED 2026-06-21** (terminal-free first run for exe + pip): `setup_ops.py` shared helpers (`pull_models` via HTTP `/api/pull` NDJSON, `install_browser`, `readiness`, `ensure_data_dirs`), new `doctor.check_browser` (WARN-only), `/api/setup/readiness` + `/api/setup/{action}/start\|status` jobs (seed-boards pattern), a first **"Set up the AI engine"** wizard step + dashboard **"Setup needed"** card. Surfaced-never-gated. 1327 green. **+ Live-test issue fixes SESSION 2 (2026-06-21):** #1 artifact legacy-name fallback (`resolve_generated_resume/_cover_letter`), #2 browser-clash apply-only auto-pause (`ManualTakeover` + scheduler `apply_gate`), **how-heard DERIVED from `job.source` (ATS⇒"Company Website", boards⇒their name; never seed, never dump on the human; `ResolutionSource.DISCOVERY`)**. Phase 2 (Inno installer) still deferred | `research/onboarding-setup-restructure.md` |
| **E2 "fill what it can on demand" — DESIGN ONLY** (owner chose design-first 2026-06-21): `POST /api/jobs/{id}/assisted/prepare` borrows the scheduler's ApplyWorker via `prepare_single`, promotes a "Needs your decision" REVIEW job into the "Ready to finish" lane; depends on the #2 takeover gate. 3 open owner questions | `research/e2-on-demand-fill-design.md` |
| Answer resolver / sensitive fields / salary | `docs/v3-architecture.md` §8b, §8d |
| Telemetry (relay + Turso) / observability | `docs/v3-architecture.md` §9 |

## Working discipline (non-negotiable)

**Research-first.** When genuinely uncertain about a risk or approach, **research and write a doc BEFORE
coding** — then update this skill. This is the approach that's been working; honor it. Phase -1 exists for
exactly this.

**Anti-stuck** (mirror of the `unstuck` skill — invoke it for iterative work):
- 3 iterations of the *same* approach → STOP. Write the pattern + 2 genuinely different alternatives; ask or plan.
- ≥2 "build → run → check" cycles → build automation (test harness / replay / preflight) before the next cycle.
- Don't stop at an intermediate step — verify the actual outcome.

**Reliability invariants** (from the spec — never compromise these for throughput):
- Manual login only; headed browser; **never retry through CAPTCHA** → downgrade to assisted.
- Mid-form break → **fail fast to REVIEW**, no retry.
- Mark `APPLIED` only on a **positive submit confirmation**.
- **Fabrication guard**: a generated résumé may only use facts in the bank; any unsupported claim → REVIEW.

## Definition of done

A task is NOT done until **(a)** the outcome is verified (not just code written), **AND (b)** any durable
finding is written into this skill (`research/`) or the spec. A finding that lives only in chat is one the
next session wastes time re-deriving — writing it down is part of finishing, like passing tests.

## How to extend this skill (mandatory)

When a session learns something durable, record it before calling the task done:
1. ATS seeding source/technique → `research/ats-discovery-seeding.md`
2. ATS form quirk / CAPTCHA / confirmation pattern → `research/ats-form-automation.md`
3. Fabrication-guard technique/result → `research/fabrication-guard.md`
4. Phase 4 sub-phase / web UI design decision → `research/web-ui-and-service.md`
5. Phase 5 sub-phase / observability CLI / telemetry mirror / relay / installer decision → `research/observability-and-distribution.md`
6. Phase 6 / v3.1 sub-phase decision (strategy profiles, salary intel, feedback loop, per-job résumé rewire, analytics) → `research/phase6-v3.1.md`
7. A changed design decision → `docs/v3-architecture.md` **and** [[project_v3_rewrite]]
8. A completed phase / new known-unknown → note in `docs/v3-architecture.md` §11b and memory

## When this skill doesn't know something

Say so explicitly — don't fabricate. The honest answer is "not in the knowledge base yet; here's the
research approach that would settle it." Then go find out and write it down.
