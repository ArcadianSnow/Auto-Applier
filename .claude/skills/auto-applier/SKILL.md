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
- Repo: `C:\Users\jar85\AI Projects\Auto Applier\`.
- **The spec** (authoritative): `docs/v3-architecture.md`. Every design decision and its rationale lives there.
- Working memory: `~/.claude/projects/C--Users-jar85-AI-Projects-Auto-Applier/memory/` — start with [[project_v3_rewrite]].
- For long iterative/debug sessions, **also invoke the `unstuck` skill** at session start.

## Where to look — decision tree

| If the question is about... | Read |
|---|---|
| Overall architecture / any design decision | `docs/v3-architecture.md` (the spec) |
| Why a decision was made / what's deferred (v3.0 vs v3.1) | `docs/v3-architecture.md` §11 + [[project_v3_rewrite]] |
| The named risks + their mitigations | `docs/v3-architecture.md` §11 |
| Build order / which phase we're in | `docs/v3-architecture.md` §11b |
| **Phase -1 verdict / go-no-go / what Phase 1 must measure** | `research/_phase-minus-1-conclusions.md` |
| How to seed ATS company lists (board tokens) | `research/ats-discovery-seeding.md` |
| How ATS apply forms behave / CAPTCHA / submit confirmation (+ live survey results) | `research/ats-form-automation.md` |
| Phase 3 pipeline staging (embedding pre-filter, score/optimize workers, scheduler) | `research/pipeline-staging.md` |
| Phase 4 web UI + worker service (FastAPI, SchedulerService, dashboard, onboarding) | `research/web-ui-and-service.md` |
| Phase 5 observability CLI (errors/stats) + telemetry mirror + relay + installer | `research/observability-and-distribution.md` |
| Prior art — other auto-apply tools/repos, methodologies, what we adopt + smoketests (reCAPTCHA v3 score, JobSpy) | `research/prior-art-and-methodology.md` |
| ATS market share by segment + what tier v3 can reach + source prioritization | `research/ats-market-landscape.md` |
| How to stop résumé fabrication (the guard) | `research/fabrication-guard.md` |
| Résumé model (fact bank → per-job generation) | `docs/v3-architecture.md` §6b |
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
6. A changed design decision → `docs/v3-architecture.md` **and** [[project_v3_rewrite]]
7. A completed phase / new known-unknown → note in `docs/v3-architecture.md` §11b and memory

## When this skill doesn't know something

Say so explicitly — don't fabricate. The honest answer is "not in the knowledge base yet; here's the
research approach that would settle it." Then go find out and write it down.
