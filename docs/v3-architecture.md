# Auto Applier v3 — Architecture & Design

> Status: **DESIGN — not yet implemented.** This is the blueprint for a ground-up rewrite
> on a new `v3` branch. v2 keeps running until v3 reaches feature parity.
>
> Decisions locked (2026-05-26):
> 1. **Ground-up rewrite** on a new branch; port the proven parts of v2.
> 2. **Centralized free telemetry** — a *scoped, opt-in* exception to v2's "no cloud" rule.
>    Errors only, no PII, default off. Recommended backend: **Turso** (libSQL / SQLite-over-the-wire).
> 3. **Hybrid apply** — full auto on API/safe paths; auto-attempt browser apply with an
>    **assisted fallback** (bot pre-fills, human submits) when detection risk is high.

---

## 1. Why rewrite — what v2 fights against

v2 works, but several foundational choices generate a recurring tax of bugs and low throughput.
The rewrite exists to remove these root causes, not to add features.

| v2 root cause | Symptom | v3 fix |
|---|---|---|
| **CSV as the database** | Schema drift (whole `migrations.py` to cope), no atomic writes (continuous + GUI race), no real queries (dedup loads everything, joins in Python) | **SQLite** as system of record; CSV becomes an *export* format |
| **Browser apply is the spine** | Anti-detect arms race (LinkedIn beats patchright via TLS), selectors "break frequently", CAPTCHA hard-stops | **API for discovery, browser for apply**: cheap structured discovery feeds scoring; submits route auto (clean ATS forms) or **assisted** (hostile boards). See §6a |
| **State is inferred, not stored** | Dedup hacks "key off PROCESSED state"; resumption/retry are special-cased | Explicit **job state machine** |
| **Synchronous per-platform pipeline** | Throughput gated by the slowest step; no batching | **Staged workers** over a status-driven queue |
| **No observability** | "Send me your log files" to debug | **Structured event spine** (local + opt-in remote) |
| **Loose JSON config** | Hand-validated, silent misconfig | **Pydantic** typed config, fail-fast in doctor |
| **No scoring eval** | Can't tell if a prompt change helped or hurt | **Golden-set eval harness** |

---

## 2. Principles (carried + new)

- **Local-first, zero-cost** for the entire core pipeline. (unchanged)
- **One scoped exception:** opt-in error telemetry to a free hosted DB. Errors only, no PII, default OFF.
- **SQLite is the source of truth.** Everything inspectable via DB Browser for SQLite or `cli export`.
- **Manual login only. Headed browser only.** Never automate credentials. (unchanged)
- **Every stage is observable.** Instrumentation is automatic (stage wrapper), not hand-written logging.
- **Capabilities, not monoliths.** A source declares what it can do (discover / describe / apply-how).
- **Idempotent + resumable.** Crash mid-run, restart, continue from the state column.
- **Local web UI + worker service.** The automation runs as an always-on background **worker**; the UI is
  a **locally-hosted web app** (control/monitor from any device's browser, incl. a dedicated runner box).
  Replaces v2's Tkinter GUI. A one-click launcher auto-starts the server + opens the tab for non-technical users.
- **US-first, region-agnostic.** Built and tested for the US, but the data model and question-handling stay
  region-neutral — no hardcoded US assumptions (e.g. work-auth, §6b/§8d). Non-US is a later config add, not a rewrite.

---

## 3. Module layout (target)

> **Package root = `auto_applier/` (built 2026-05-26–05-30 as the `av3` package, renamed to
> `auto_applier` at the v3→master cutover on 2026-05-30 when v3 superseded and replaced v2).** The
> CLI command verb stays `av3` — only the package/import namespace is `auto_applier`. v2 (the old
> Tkinter+CSV app) has been deleted; its lessons live in the git history and the §1 root-cause table.

```
auto_applier/        # the v3 package (built as the av3 package, renamed at the v2 cutover)
  config/          # Pydantic settings models, .env merge, validation
  db/              # SQLite engine, schema, migrations, repositories, CSV export/import
  domain/          # Pure dataclasses + the job/application state machine (no I/O)
  llm/             # Local Ollama completion + embeddings, prompts (Gemini cloud tier removed 2026-06-09 — retired model + local-first)
  scoring/         # Embedding pre-filter → LLM dimension scoring → decision; eval harness
  resume/          # Fact bank (master profile) + per-job generation + fabrication guard + cover letter + stories (§6b)
  sources/         # Capability-based source adapters (see §6)
    api/           #   greenhouse, lever, ashby, workable, smartrecruiters, ... (no browser)
    browser/       #   dice, indeed, ziprecruiter, linkedin (discovery) + anti-detect primitives
  pipeline/        # Staged workers + queue + the @stage instrumentation wrapper
  telemetry/       # Event sink: always-local SQLite; opt-in Turso mirror; scrubber
  worker/          # Always-on background service: staged pipeline on asyncio + Playwright async API; @stage spine
                   #   (async = clean task cancellation for the F6/idle pause, natural fit for IO-bound work)
  web/             # FastAPI backend + browser dashboard (control, review queue, live stats); local launcher
  cli/             # Click commands (incl. errors/stats/telemetry/export-diagnostics)
  doctor.py        # Preflight: LLM backend reachable + model present; DB writable + backups OK + schema current;
                   #   platform logins valid (else flag login-needed); browser/Chromium installed, disk OK, relay reachable
```

**Port vs rebuild vs drop:**
- **Port (proven, reusable):** `llm/` (router, cache, prompts) + add embeddings; ATS API adapters; resume
  parse/skills/tailor/`tailor_validator`/cover-letter/evolution/story-bank; ghost check; anti-detect primitives;
  scoring dimensions + weights.
- **Rebuild:** storage (CSV→SQLite), orchestrator (sync→staged workers), platform ABC (→capability model),
  config (→Pydantic), UI (Tkinter→web). **v2 is reference/lessons, not a base to extend** — port logic, not structure.
- **Drop:** `storage/migrations.py` CSV-drift machinery, the Python-side dedup join workarounds, Tkinter GUI.

---

## 4. Data model (SQLite)

```sql
-- The job and its lifecycle
jobs(
  id TEXT PRIMARY KEY,              -- internal uuid
  source TEXT,                      -- 'greenhouse' | 'dice' | ...
  source_job_id TEXT,
  canonical_hash TEXT,             -- normalize_title+company (cross-source dedup)
  title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT, compensation TEXT, posted_at TEXT,
  ghost_score REAL,
  state TEXT,                       -- see §5 state machine
  discovered_at TEXT, updated_at TEXT,
  UNIQUE(source, source_job_id)
)

-- One score per job: the JD scored against the master profile (no multi-résumé). See §6b.
job_scores(
  job_id TEXT PRIMARY KEY,
  total REAL, dimensions_json TEXT, -- {skills, experience, ...}
  model TEXT, scored_at TEXT
)

applications(
  id TEXT PRIMARY KEY, job_id TEXT,
  mode TEXT,                        -- 'browser_auto' | 'assisted'
  status TEXT,                      -- see §5
  cover_letter_path TEXT,
  generated_resume_path TEXT,       -- the per-job résumé generated from the fact bank (§6b)
  submitted_at TEXT
)

-- THE observability spine.
-- Implementation note (Phase 0): `events` lives in a SEPARATE `events.db` file, not in
-- app.db — so the highest-write table never contends with app writes and is pruned on its
-- own cadence (§9). app.db holds jobs / job_scores / applications / skill_gaps / answers.
events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, ts TEXT,
  stage TEXT,                       -- 'discover' | 'score' | 'apply' | ...
  platform TEXT, job_id TEXT,
  status TEXT,                      -- 'start' | 'ok' | 'error' | 'skip'
  duration_ms INTEGER,
  error_type TEXT, error_msg TEXT,  -- scrubbed before any remote mirror
  context_json TEXT
)

skill_gaps(skill TEXT PRIMARY KEY, count INTEGER, first_seen TEXT, last_seen TEXT, status TEXT)
answers(question TEXT PRIMARY KEY, answer TEXT, source TEXT, embedding BLOB, updated_at TEXT)  -- UPSERT; embedding for semantic match (§8b)
```

Generated artifacts (PDFs, cover letters, tailored resumes) stay as **files**; the DB stores their paths.
`cli export` dumps any table to CSV for the Excel-inspection workflow.

**No migration importer — fresh start.** v3 reconfigures from scratch in its own wizard. v2 data is not
imported; v2 code is treated as lessons, not a base. (Simplifies Phase 1 and avoids carrying legacy cruft.)

**Retention & backup:** auto-prune *ephemera* (`SKIPPED`/`FILTERED` jobs and stale discovery rows after a
configurable window, e.g. 30d) to keep the DB lean; **keep `APPLIED` history indefinitely** (it's the
record of what you applied to, and the dedup source of truth); take **periodic local backup snapshots** of
the SQLite file (timestamped, rotated). `events.db` errors prune on a shorter window; mirrored telemetry is
independent (§9).

**Security at rest:** rely on **OS disk encryption (BitLocker/FileVault) + strict file permissions** on the
data dir, not app-level DB encryption. Rationale — the *always-on key paradox*: an unattended service must
hold the decrypt key automatically, so full-DB encryption (SQLCipher) protects against almost nothing here
while breaking backups + DB inspection. OS disk encryption defends the realistic threat (lost/stolen device)
with the key in the TPM, friction-free. **Field-level encryption of the most sensitive facts is an optional
future add** if v3 ever ships to less-trusted contexts. Sensitive PII (EEO/salary/work-auth) is never
mirrored to telemetry regardless (§9).

---

## 5. Job state machine

```
DISCOVERED ──dedup/ghost──▶ SKIPPED
     │
     ▼
 (embedding pre-filter) ──low rank──▶ FILTERED
     │ top N
     ▼
 DESCRIBED   (full JD scraped — score on full text, never a snippet)
     │
     ▼
   SCORED ──decision──▶ DECIDED ──▶ { QUEUED_APPLY | REVIEW | SKIPPED | APPLIED* }
                                          │  optimize+Strict gate: tailor résumé +
                                          │  cover letter + fabrication guard must PASS,
                                          │  else ▶ REVIEW
                              QUEUED_APPLY ▼            * manual mode: human applied
                                      APPLYING ──┬─ positive confirmation ──▶ APPLIED (terminal)
                                                 └─ no confirmation / mid-form break ──▶ FAILED ──▶ REVIEW
```

`APPLIED` requires a **positive confirmation** — either the bot's detected on-page success signal, OR an
explicit **human attestation** in the manual operating mode (see below) — never assumed from a click (§8).
A mid-form break **fails fast to REVIEW, no retry** (§8). Only `APPLIED` rows count for dedup, so an
unconfirmed apply is safely retryable and never inflates success.

**Manual / human-apply mode.** When the product is run discovery+scoring-only and a human applies
externally (the primary personal-search use), the edges `DECIDED → APPLIED` and `REVIEW → APPLIED` let
`av3 applied` record it (writing an `Application` row with `mode = MANUAL`). A human explicitly attesting
"I applied" is a positive confirmation, not a click inference, so the invariant holds. Because `APPLIED`
is the same terminal/dedup state, manually-applied jobs drop out of `av3 shortlist`/`digest`, are deduped
out of future discovery, are never pruned, and accept `av3 outcome` follow-ups — all unchanged. `av3 pass`
(DECIDED → SKIPPED) records "looked, not interested." Note: a manual apply also counts toward that day's
per-company / daily pacing counters — intentional in mixed mode (you did apply).

Transitions live in **one module** (`domain/state.py`) with an allowed-transitions table. Dedup,
continuous-run resumption, and retries become queries on `state`, not bespoke logic. A crashed run
leaves jobs in `APPLYING`; a sweep on restart re-queues or fails them.

---

## 6. Source capability model

Replace the do-everything `JobPlatform` ABC with composable capabilities a source declares:

- `Discoverer` — search/list jobs.
- `Describer` — fetch full JD (some APIs include it; some browser sites need a page visit).
- `Applier` with a declared `apply_mode`: `BROWSER_AUTO`, `BROWSER_ASSISTED`.

| Source | Discover | Describe | Apply |
|---|---|---|---|
| Greenhouse / Lever / Ashby / Workable / SmartRecruiters | ✅ **API** (no/low auth) | ✅ API | `BROWSER_AUTO` on the hosted form (clean, low anti-bot) |
| Dice / ZipRecruiter / Indeed | ✅ browser | ✅ browser | `BROWSER_AUTO` or `BROWSER_ASSISTED` (router decides) |

**LinkedIn is CUT from v3.** It never submits (discovery-only) and is the most detection-hostile source
(TLS fingerprinting beats patchright). ATS APIs + Dice/ZipRecruiter/Indeed cover discovery without the
arms race. Removing it shrinks surface area and bug count. (Revisit only if a viable stealth path appears.)

**Adding a source** = implement the capabilities it has and register it. No forced stubs.

### 6a. Why no `API_AUTO` apply mode (research finding, 2026-05-26)

Audited Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, iCIMS. **None offer a usable
full-auto apply API for a job-seeker tool.** The first five expose apply-submit endpoints (with résumé
upload + custom-question answers), but **every one requires an API key/token tied to the specific
employer's ATS account** — those APIs exist so an *employer* can build its own careers page, not for a
third party to submit to arbitrary companies. Workday is tenant-specific; iCIMS is partner-gated.

Consequence: **APIs are discovery-only for us; the browser carries 100% of submits.** This is fine —
the throughput win comes from *discovery*, not apply:
- Discovery via API is auth-free or token-cheap (Lever & SmartRecruiters need no auth; Greenhouse/Ashby/
  Workable use public board tokens), structured, login-free, and anti-detection-free. We can list+read
  hundreds of postings per cycle and feed scoring without opening a browser.
- ATS **hosted application forms** (Greenhouse/Lever/Ashby/…) are clean and low-anti-bot, so `BROWSER_AUTO`
  against them is genuinely reliable — unlike LinkedIn/Indeed where assisted-apply earns its keep.

Funnel shape: **wide cheap API discovery → score many → apply few via browser** (auto on clean ATS forms,
assisted on hostile boards).

### 6b. Résumé model — one fact bank, generated per job

No résumé *library*. v3 **seeds the fact bank from multiple sources, merged**: one or more uploaded résumés
+ optional LinkedIn/profile export, into a single **master fact bank** — the single source of truth —
containing: contact info, education, **work history (companies/titles/dates/bullets)**, skills,
certifications, achievements. **Where sources conflict** (dates, titles, bullet wording), variants are
**kept and the user picks the canonical** during the onboarding fact-bank review (no silent auto-resolve).
The bank also stores **work-authorization + sponsorship status (captured explicitly in onboarding — no
silent default)** and **optional EEO self-ID values** (voluntary; blank ⇒ "prefer not to answer"). Richer
bank → better per-job generation. Stored as a validated, user-reviewable doc (`profile/master.json`).

Generated résumés render to **ATS-safe single-column PDF** (no tables/text-boxes/headers-footers, real
selectable text) — maximizing parser pass-through is the point; human prettiness is secondary.

**Cover letters default to concise & tailored (~150–250 words)**, length configurable (concise / standard /
full). Rationale: in a generate-from-fact-bank system, **length is fabrication surface** — longer letters give
the LLM more room to drift into unsupported claims and trip the guard. The §8e outcome loop measures which
length actually converts, so the default is data-revisable rather than a guess.

For every job that passes scoring, v3 **generates a tailored résumé** by selecting, omitting, reordering,
and rephrasing facts from the bank toward that JD (great for ATS keyword matching). This replaces v2's
"multiple résumés + score-all-pick-best", the standalone tailor step, and most of evolution:

- **Scoring scores the JD against the master profile** — one score per job, not N (§5, §7). Cheaper, cleaner.
- **Generation is the default apply path**, not an add-on. Output is the per-job `generated_resume_path`.
- **Evolution = proposing new facts for the bank**, which the user approves. The LLM never silently grows it.

> **Fabrication invariant (load-bearing).** The fact bank is the ONLY source of truth. Generation may
> select/omit/reorder/rephrase, but may **NEVER introduce a company, title, date, credential, or skill not
> in the bank.** The fabrication guard checks every claim against the bank; any unsupported claim drops the
> job to `REVIEW` — never auto-submits. This is what separates "tailored" (good) from "lying" (catastrophic
> on a real application). Borderline cases fail closed to REVIEW.

### 6c. Job targeting — what to search for

Hybrid: **natural-language intent seeds, structured filters refine.** The user types intent
("remote senior data analyst, $120k+, no clearance"); the LLM parses it into a **structured filter set**
(titles + expanded related titles, locations, remote/onsite, salary floor, seniority) that the user can
review and edit. The structured set is what actually drives each source's discovery query.

---

## 7. Pipeline — staged workers over a queue

The queue *is* the `state` column. Workers drain by status; each is independently retryable and observable.

1. **Discovery producer** (concurrent per source) → cheap list/snippet → `DISCOVERED`.
2. **Dedup/ghost filter** → canonical-hash dedup + ghost score → `SKIPPED` or pass. **Re-apply policy:** never re-apply to a job already `APPLIED`; **multiple roles per company allowed but rate-limited** (configurable window, e.g. ≤N/company/day) so it never looks spammy to one employer.
3. **Embedding pre-filter** (free local Ollama embeddings, cosine-rank snippet vs. résumés) → keep top N, rest `FILTERED`. *(Throughput + cost win: don't fetch/LLM-score obvious non-matches.)*
4. **Describe** → scrape the **full JD** for survivors (API: included/cheap; browser: one page visit) → `DESCRIBED`. **Scoring always runs on full text, never a snippet** — "scrape, then score," to get the score as right as possible.
5. **Scoring worker** → LLM dimension-score the full JD **against the master profile** (§6b) → `job_scores` → `SCORED` → `DECIDED`.
6. **Optimize (Strict gate)** → for auto-bound decisions: **generate the per-job résumé from the fact bank** (§6b) + cover letter + run the fabrication guard. Pass → `QUEUED_APPLY`; fail → drop to `REVIEW` (never auto-submit un-optimized).
7. **Apply worker** → drains `QUEUED_APPLY`. Routes via the **detection-risk router** (§8); fills the form via the **answer resolver** (§8b). Requires positive submit confirmation → `APPLIED`, else `FAILED`→`REVIEW`.
8. **Post-apply** → record skill gaps, propose fact-bank additions (evolution). (No follow-up emails — cut.)

Each unit of work runs inside a `@stage("name")` wrapper that emits `start`/`ok`/`error` events with
timing automatically. **That wrapper is the entire observability story** — no scattered logging calls.

### 7a. Operating model: always-on, low-friction (product north star)

The product goal is **minimum user friction, maximum *well-applied* jobs.** That makes continuous
operation the *default mode*, not a `--continuous` flag:

- **The workers just keep running — 24/7 by default**, with optional user-set **quiet hours**, within rate
  limits. The app behaves like a quiet background assistant, not a tool you launch per session. v2's separate
  "continuous mode" disappears — there is only the always-on loop (a one-shot `cli run --once` for testing).
  *(24/7 activity is itself a behavioral signal; the strategy-profile pacing/rotation in §8a is what keeps
  the cadence human even when the fingerprint is maxed — see §8c.)*
- **Maximize auto, batch the assisted.** Assisted-apply requires a human click = friction. So the
  detection-risk router biases hard toward `BROWSER_AUTO` wherever safe (clean ATS forms = the bulk of
  volume), and assisted items **never interrupt one-by-one** — they accumulate in a `REVIEW`/assisted
  queue surfaced as a **count badge in the web dashboard** (no notifications, no interruption); the user
  clears them in a single "review & submit" sitting whenever convenient.
- **"Well applied," not spam — the Strict gate.** A submit only auto-fires when ALL hold: score ≥ threshold,
  a per-job **tailored résumé** generated, a **cover letter** generated (**always**, even if the form has no
  CL field — it's then ready for the field if present / for assisted review), and the **fabrication guard**
  passes. Any failure routes the job to `REVIEW` instead of auto-submitting. Throughput stays high because *discovery
  is cheap and the auto path is clean* — not because the bar dropped.
- **Control handoff via global hotkey (default).** On a shared single machine, **F6 toggles** whether the bot
  may drive the browser: press to hand it the machine, press again to take control back instantly (a
  system-level key hook so it works even while the browser has focus). Optional **idle-detection** complements
  it (auto-pause on input). On a dedicated runner box, neither matters — the bot owns the screen.

### 7b. Batch flow & the optional skill-reconciliation checkpoint

> **Scope:** the batch flow and *passive* gap-recurrence proposals are **v3.0 core**. The **interactive
> skill-reconciliation conversation/toggle is v3.1.**
>
> **Status (Phase 6 5/M, 2026-05-30):** LIVE as a CLI reconciliation loop. `auto_applier/reconcile.py` (deterministic
> JD skill extraction over a curated vocabulary — no LLM; `record_batch_gaps` wires the previously-dead
> `SkillGapRepo`; `build_proposals` ranks gaps; `apply_proposals` additively inserts into the bank) +
> `av3 reconcile [--scan] [--min-count N] [--apply "s1,s2"]`. Preview is read-only; **`--apply` is the only
> fact-bank mutation and is additive + user-named** (Rule 2.6 — the bank is the fabrication-guard source of
> truth). See `research/phase6-v3.1.md` §(5/M).
>
> **Status (Phase 6 7/M, 2026-06-11):** the **interactive web conversation is LIVE** — `/reconcile` page +
> `/api/reconcile/{proposals,scan,apply}`. Same contract as the CLI: surface → user checks the skills they
> actually have → confirm is the only bank mutation (additive). See `research/phase6-v3.1.md` §(7/M).

v3 works in **batches**, not one-job-at-a-time: discovery producers fill the queue across all sources, then
a batch is scored and résumés generated together. An **optional skill-reconciliation checkpoint** sits
between discovery and scoring:

- **Toggle (set before or during a run).** When **ON**, once a batch accumulates **across all sources**, the
  worker pauses for an interactive **skill cleanup/insert conversation** — skills and gaps surfaced by the
  batch's JDs are reviewed, cleaned, and inserted into the fact bank (user-approved) — so the subsequent
  score + résumé-generation run against a freshened bank. Maximizes quality when the user is present.
- **When OFF (unattended/always-on):** scoring proceeds against the current bank; gap-driven proposals still
  queue **passively** (a skill seen ≥ N times → "add to bank?" in the review queue) for later.
- **Recommended scope:** reconcile **once against the combined cross-source batch**, not per-source — fewer
  interruptions, and the bank is armed against the full demand set before any résumé is written.

**Discovery breadth (per-source-type, tunable):**
- **Boards (Dice/ZipRecruiter/Indeed):** pull **all new since last cycle** — full freshness.
- **ATS (Greenhouse/Lever/…):** **bounded per cycle** (cap companies/jobs, rotate across cycles) — "all new"
  across hundreds of company boards would be overwhelming.

---

## 8. Detection-risk router (the "hybrid, auto where safe" decision)

A small policy that picks `BROWSER_AUTO` vs `BROWSER_ASSISTED` per attempt:

- Force **assisted** if: known-hostile platform (LinkedIn), a CAPTCHA fired this session on this platform,
  a login wall is detected, or a JA4/TLS risk signal trips.
- Otherwise attempt **auto**, and **downgrade to assisted on the first detection signal** rather than
  retrying through it (v2's CLAUDE.md rule: never retry through CAPTCHA).
- **Assisted apply UX:** bot opens the page, fills every field it can (personal info → answers → LLM),
  attaches resume + cover letter, then surfaces a GUI "ready to submit — review & click" prompt.
  Near-100% submit success without fighting anti-bot systems.

Thresholds/flags live in typed config so behavior is tunable without code edits.

### 8a. Strategy profiles — Pareto-configurable pacing

> **Scope:** **v3.1.** v3.0 ships sensible *fixed* pacing (basic rate-limit + per-source rotation); the
> configurable Cautious/Balanced/Aggressive profile system below lands in v3.1.
>
> **Status (Phase 6 2/M + 8/M, 2026-05-30):** the profile system is LIVE for ALL §8a knobs. 2/M wired
> **inter-apply delay, soft daily target, per-company/day cap, and risk-router bias** (→ auto-vs-assisted
> *starting* mode). 8/M added **concurrency** (declared parallel-apply ceiling — Cautious 1 / Balanced 1 /
> Aggressive 3; the worker still drains sequentially, so it's read by the scheduler/dashboard, not yet
> acted on) and **session rotation** (`session_rotation_min`; enforced by `SessionRotationPolicy`, which
> the apply loop consults at the top of each job — once the budget on the current source elapses the worker
> softly defers the rest, `summary.rotated`, surfaced as `rotated=` on the CLI line, like the daily-target
> break). Selector is `strategy.profile` in `user_config.json` (default `balanced`, whose preset == the v3.0
> fixed defaults incl. `concurrency=1, session_rotation_min=0.0`, so the default is inert). Profiles +
> presets live in `auto_applier/config/strategy.py`; `ApplyWorker` resolves them once via `resolve_strategy()`. See
> `research/phase6-v3.1.md` §(2/M)+(8/M).

Retire v2's rigid "10 apps/day, 60–180s between." A fixed cap is annoying to maintain *and* paternalistic
toward a user who wants volume. The real tradeoff is a frontier — **throughput ↔ detection-risk ↔ user
effort (auto vs. assisted clicks)** — and you can't max all three at once. So we don't pick for the user;
we expose the frontier as named **strategy profiles**, each a coherent point on it.

A profile tunes these knobs together:
- **Session rotation** — time-box per source (e.g. 30 min) then rotate. Spreads fingerprint exposure and
  avoids hammering one site: an anti-detection win, not just a throughput knob. Replaces the daily counter.
- **Inter-apply delay** range.
- **Concurrency** — how many sources run at once.
- **Risk-router bias** (§8) — how readily it stays `BROWSER_AUTO` vs. drops to assisted.
- **Daily target** — a *soft goal* the scheduler aims for, **never a hard wall that blocks.**

| Profile | Delays | Session/site | Concurrency | Risk router | Daily target |
|---|---|---|---|---|---|
| **Cautious** | long | short | serial | leans assisted | low |
| **Balanced** *(default)* | moderate | moderate | light | balanced | moderate |
| **Aggressive** | short | long (~30 min, rotate) | concurrent | leans auto | high (e.g. 100) |
| **Custom** | every knob hand-set in config | | | | |

**Safety floor — NOT on the frontier, never tunable by a profile:** manual login only, headed browser,
never retry through CAPTCHA, downgrade-to-assisted on any detection signal. Strategy changes *pacing and
volume*, never these invariants. (Honest note: Aggressive volume realistically leans on the clean ATS
auto-path — 100 *assisted* applies/day is a lot of human clicking.)

### 8b. Answer resolver & apply-reliability rules

The reliability rules that keep the auto-path safe and the data honest:

- **Fail fast, no retry.** A mid-form break (selector miss, unexpected required field) drops the job to
  `REVIEW` immediately — no retry loop, because retries risk duplicate or garbled partial submissions.
- **Positive submit confirmation required.** `APPLIED` is set only on a detected on-page success signal,
  **never off a confirmation email alone** (research finding). Per-ATS signals: Greenhouse → `/confirmation`
  redirect; Lever → `/thanks` state; Ashby (React SPA, no redirect) → in-place "Application submitted" panel,
  corroborated by `success:true` on the submit XHR. Require the positive signal AND no validation/CAPTCHA
  error. No signal → `UNCONFIRMED`/`FAILED`→`REVIEW`. Since dedup keys off `APPLIED`, an unconfirmed attempt
  is safely retryable and success never inflates. (Detail: `research/ats-form-automation.md`.)
- **Two-tier answer resolver** for form questions, in order:
  1. **Semantic match** — embed the question, compare to the answer bank; matches a known Q&A even when
     *worded differently* (e.g. "authorized to work in the US?" ≡ "have US work authorization?"). → answer (high trust).
  2. **Genuinely novel (no match) → default bail to `REVIEW`.**
  3. **Confidence-gated backup:** before bailing, the LLM judges whether the **fact bank** holds enough to
     answer confidently. Confident → answer **but flag it**; not confident → bail. Distinguishing (1) from
     (2) well is what makes this safe, hence embeddings on questions.
- **Inferred/novel answers flow to telemetry** (§9) as an iteration signal — promote frequently-inferred
  questions to canonical answers, catch bad ones. The flagged answer value stays local; only the question +
  category + confidence + outcome are mirrored (§9).
- **Session expiry = graceful degradation.** Manual-login-only means the bot can't re-authenticate. When a
  platform's session dies mid-run, that platform is **paused with a "login needed" flag** (surfaced as a
  dashboard badge); **all other sources keep running.** One dead session never stalls the whole bot. No
  interruption — the user re-logs in when convenient and the platform resumes. **Login UX:** the headed
  browser **auto-opens on the host machine** and the dashboard shows the prompt; the user completes the
  manual login there (logins are also done on-demand at first use, §11a).

### 8c. Anti-detection & browser profile

Stance: **maximal stealth, invested proactively** (not reactively), so a future-hostile site doesn't block a
release. Two layers, both required — fingerprint *and* behavior:

- **Fingerprint layer:** **patchright + real Chrome** (via `channel`) as primary; **nodriver / camoufox**
  retained as selectable stealthier backends; attention to JA4/TLS signals. Headed always.
  **Why this is load-bearing:** all three target ATS ship *invisible* behavioral CAPTCHA by default
  (Greenhouse reCAPTCHA/Enterprise, Lever hCaptcha, Ashby reCAPTCHA), and clearing it without triggering a
  *visible* challenge is exactly what determines the auto-vs-assisted split (§11 Phase -1 outcome). Stealth
  quality ≈ auto-apply rate. A visible challenge always → assisted (never solved/retried).
  - **Measured finding (2026-05-26 smoketest, `research/prior-art-and-methodology.md` §6 S1):** our
    patchright + **real Chrome** + **persistent profile** stack scores **0.9 on standard reCAPTCHA v3**
    (≥0.5 pass threshold) — *beating* the "Chromium ceiling ~0.3–0.5" that 2026 stealth blogs report for
    *clean/fresh* fingerprints. The delta is **profile reputation**: reCAPTCHA v3 weights IP reputation +
    browser history + cookies, which our real-Chrome persistent profile already carries. So **profile
    reputation is a first-class stealth lever we already pull**, and a Firefox engine
    (`invisible_playwright`/Camoufox, ~0.9) is a documented **fallback, not a required switch**. Standard
    invisible reCAPTCHA (Ashby's lighter half, non-Enterprise GH) and hCaptcha (Lever) look auto-viable on
    the current stack; **reCAPTCHA *Enterprise* (100% of GH in our survey) remains the sole auto-pass
    unknown** — only a gated real submit resolves it.
- **Behavioral layer:** Bezier mouse paths, typing jitter, randomized delays, distraction pauses, and the
  **strategy-profile pacing + per-source session rotation** (§8a). This is what offsets the behavioral
  signal of 24/7 operation — a maxed fingerprint that applies like a robot still gets caught.
- **Browser profile:** **one persistent shared Chrome profile** across all sites — logins survive restarts
  (manual-login-once holds), one coherent fingerprint to maintain. Simplest and matches v2.
- **Hard invariants (from the safety floor, §8a):** manual login only, never retry through CAPTCHA,
  downgrade-to-assisted on any detection signal.

### 8d. Sensitive fields & salary intelligence

Answer-resolver (§8b) policy for the fields that appear on nearly every form:

- **EEO / demographics** (race, gender, veteran, disability): submit the user's **optional self-ID values**
  collected in onboarding; if the user left them blank, answer **"prefer not to answer."** Never guess a
  sensitive attribute. These values are stored locally only — **never mirrored to telemetry.**
- **Work authorization / sponsorship:** answered from the **explicitly-captured onboarding facts** (§6b) —
  no silent "authorized = yes" default (the v2 behavior, corrected).
- **Salary expectation = salary intelligence.** *(Status: LIVE as of Phase 6 (3/M), 2026-05-30 —
  `auto_applier/resume/salary.py`.)* Compute a recommended ask from three inputs: the user's configured range, the
  job's **posted range** (if any), and **market data**. Priority posted → market → user range; the floor is a
  hard lower bound and the ask never overshoots the posted ceiling. Corrects users who would low-ball or
  overshoot. Market source is **pluggable**; **v3.1 ships it defaulting to `none` (local-first, zero egress)**
  — the BLS OES adapter is an opt-in entry the user wires in `salary.market_source` (accepting its egress),
  not on by default, because the product's hard rule is no network egress in the core pipeline. Adzuna would
  be an optional second adapter. *Glassdoor/Levels.fyi are out of scope — no legitimate free feed; scraping is
  brittle + ToS-risky, exactly what v3 avoids.*
- **Compensation filter (a scoring gate, not just a fill value):** if a job's **posted range is below the
  user's floor / current pay**, the job is **SKIPPED before apply** — saving a wasted application. *(Status:
  LIVE as of Phase 6 (3/M) — the **score worker** runs `is_below_floor` BEFORE the LLM call, so a below-floor
  job costs no scoring/generation work; walks DESCRIBED → SCORED → DECIDED → SKIPPED, bucketed as
  `comp_skipped`.)* If no range is posted, can't filter → proceed and use the computed ask for any salary field.

### 8e. Outcome feedback loop (gets smarter over time)

> **Scope:** **v3.1.** v3.0 records outcomes; the feedback/auto-tuning loop below comes after the core proves out.
>
> **Status (Phase 6 4/M, 2026-05-30):** LIVE as a **read-only insights + advisory** loop. `outcomes` table +
> `OutcomeRepo` + `av3 outcome <job_id> <kind>` record; `auto_applier/analytics.py` (`compute_conversion_report`,
> `recommend_weight_nudges`) + `av3 analytics` surface conversion-by-source/title/score-band and *suggest*
> weight nudges. **Auto-tuning is deliberately NOT auto-applied** — a nudge is a recommendation the user
> applies by editing `user_config.json` (mutating live scoring off sparse early data is the §8e anti-pattern;
> Rule 2.6 "gate the act"). See `research/phase6-v3.1.md` §(4/M).

Recorded outcomes (response / interview / rejection / ghost) feed a **lightweight, local, zero-cost** loop:
surface which **sources, titles, and score-bands actually convert**; gently auto-tune scoring nudges and
sharpen ghost detection; flag cover-letter length / résumé patterns that correlate with responses (§6b). No
heavy ML — insights + bounded auto-tuning, shown in the analytics dashboard (§10). This is the mechanism that
turns "applied a lot" into "applies better over time," and it's what arbitrates open style questions (e.g.
cover-letter length) with data instead of guesses.

### 8f. Application copilot (post-v3.1, 2026-06-11)

> **Status: v1 LIVE.** `auto_applier/copilot.py` + `av3 ask` + `/copilot` (web). Full design rationale:
> `research/application-copilot.md`.

An **interactive, honesty-first screener-question assistant** — distinct from the §8b answer resolver
(which fills *known* form fields from stored answers). The copilot reasons over {fact bank + the specific
job + an arbitrary question} and returns a structured answer: verdict (yes/no/partial/review), a
paste-ready short + long answer, reasoning, overclaim-risk flag, interview framing, and skill gaps.

The design centerpiece is the **evidence audit** — the judgment-call analog of the §6b fabrication guard.
A local model (qwen3:8b) will agreeably overclaim ("Yes" to "have you led a Debezium implementation?"
when the real experience is watermark-based sync), and the fabrication guard can't catch a wrong "Yes"
because it isn't a fabricated noun. So the copilot prompt **demands `bank_evidence`** (the bank facts the
verdict rests on) and a deterministic post-check token-matches each item against the bank corpus: a
yes/partial verdict with **zero supported evidence fails closed to `review`**; unsupported items raise
`overclaim_risk` to high. Saying "no" never requires evidence (the guarded risk is overclaim, not
underclaim — "No + here's the adjacent experience" is the instructed pattern).

Sensitive questions (work-auth / sponsorship / EEO / salary) **never reach the LLM** — they route through
the same `classify_sensitive` + deterministic policy as the resolver. `av3 ask --save` upserts an
accepted answer into the answer bank (so the §8b Tier-1 semantic match reuses it on real forms), and is
refused while the answer `needs_review`. Nothing the copilot produces is ever auto-submitted.

---

## 9. Telemetry (the centralized-error decision)

**Always:** every stage writes events to local `events.db`. `cli errors`, `cli stats`, and any future
Claude session debug straight from SQL — no log files.

**Opt-in remote mirror (default OFF):**
- Backend: **one shared Turso** DB (libSQL), written via a **thin owner-hosted relay** — the app POSTs
  events to a tiny serverless endpoint (free tier, e.g. Cloudflare Workers); the **real Turso write token
  lives only in the relay, never ships in the app.** The relay re-scrubs (second line of defense),
  rate-limits, and rejects malformed rows before inserting. If a client is compromised, there's no token to
  steal and the relay can drop abusive callers. Owner queries the Turso DB directly to triage.
- **Identified within the group, not anonymous.** This is a known 3–4-person group and the owner *wants* to
  know who hit what. At first run we ask for a handle/first name, store it **locally**, and send
  `user_id = sha256(handle)[:10]` — a stable short hex pseudonym (e.g. `a3f9c1d204`). The raw name never
  leaves the machine; the owner keeps the small "hash → person" mapping. Attribution works without putting
  a real name in the cloud.
- **Local `events.db` keeps full detail; the mirror is a scrubbed subset.** Two event classes mirror:
  (a) **errors/critical** → `{user_id, app_version, stage, platform, error_type, scrubbed_error_msg, ts}`;
  (b) **inferred/novel-answer events** (§8b iteration loop) → `{user_id, question_text, category, confidence,
  outcome(answered|bailed), ts}` — **never the answer value** (that's PII; stays local). The scrubber drops
  résumé text, answer values, names, and emails before any send.
- Toggle: `cli telemetry on|off|status`. First run explains exactly what leaves the machine and requires
  explicit opt-in. This is the *only* network egress in the product and it is gated.
- You query the shared Turso DB with `turso db shell` (or any libSQL client) to triage everyone's failures
  centrally, grouped by `user_id` — the "remote debugging without log files" goal.

> This is a deliberate, documented exception to v2's strict no-cloud rule. The core pipeline still runs
> fully offline; telemetry is additive, scoped, scrubbed, and opt-in.

---

## 10. Config, scoring eval, GUI

- **Config:** Pydantic Settings models load `user_config.json` + `.env`, validate on startup; `doctor`
  fails fast on bad config. Scoring weights, risk thresholds, rate limits all typed.
- **Multi-dimensional scoring (ported from v2):** the LLM scores the JD-vs-master-profile on **7 weighted
  axes** — skills 0.35, experience 0.20, seniority 0.15, location 0.10, culture 0.08, growth 0.07,
  compensation 0.05 — and Python computes the weighted total. The **compensation axis** is where the §8d
  comp-filter feeds in. Auto ≥ `auto_apply_min` (default 7), REVIEW ≥ `review_min` (default 4), else SKIP.
- **Scoring control:** smart defaults out of the box (auto ≥ threshold, review band, 7 axis weights); power
  users can retune weights and cutoffs in config. **The quality bar is independent of the volume strategy
  profile** (§8a) — Aggressive applies *more*, never to *worse* jobs. **Ghost-job filter is always ON.**
- **Scoring eval harness:** `evals/golden_set.jsonl` (a dozen job+resume pairs with expected score bands)
  + a pytest that asserts bands. Run on every prompt/model change so scoring quality is measurable.
- **Testing strategy:** a **dry-run mode** (fills every field, never clicks final submit) is the dev default;
  recorded **HTML fixtures** drive form-filler + answer-resolver unit tests; **CI runs against mocked sources**;
  scheduled **live smoke tests** against real sites catch selector drift early (the #1 v2 bug source).
- **Prompt & model management:** prompts live in **versioned template files** (not inline), model choices in
  **config presets**; the eval harness gates prompt/model changes so quality stays measurable. Not user-editable
  (a tweaked prompt can silently break JSON parsing/scoring).
- **UI:** local **web app** (FastAPI + browser dashboard), not Tkinter. Reads live from SQLite + the event
  stream (server-sent events / websocket). **v3.0 surfaces:** **live pipeline status** (per-source, with
  F6/pause state); **review queue + login-needed badges**; **application history + outcomes** (with
  confirmation status). **v3.1 surfaces:** rich **analytics** (funnel, per-source success rates, skill-gap
  "what to learn next" trends), strategy-profile controls, and **branded** visual polish (use the
  `frontend-design` skill). v3.0 UI is **clean/functional**, keyboard-navigable, good contrast. One-click
  launcher starts the server and opens the tab. *(Status 2026-06-11: branded polish + the `/reconcile`
  conversation shipped in Phase 6 (7/M); analytics/learn trends live as CLI `av3 analytics` / `av3 learn` —
  their web panels remain a nice-to-have, not a planned gap.)*

---

## 11. Risks & de-risking

Honest assessment of where this can fail, and how the plan answers each. The recurring theme: **the value
lives in the riskiest 20% (reliable browser auto-apply + the fabrication guard), so prove that first.**

| Risk | Why it threatens the project | De-risking move |
|---|---|---|
| **ATS company-list seeding** — no free "list all boards" API | ATS-first discovery has no *input* without a company→token list | Phase 1 ships a **curated seed list** of target companies; evaluate auto-sourcing later; if unworkable, lead discovery with a browser board |
| **Cross-ATS form automation** (100% of submits are browser) | The hardest, most brittle layer; "clean ATS = auto" is unvalidated; some ATS embed CAPTCHA | **Vertical slice proves it on Greenhouse first**; CAPTCHA → assisted (safety floor); measure the real auto-vs-assisted ratio early |
| **Fabrication guard in a vise** (too strict = all REVIEW; too loose = lies) | The entire résumé model rests on it; subtle exaggeration is hard to catch | **Prototype + eval the guard against a labeled set in the slice** before committing breadth |
| **Confirmation detection** across heterogeneous sites | False FAILED → re-applies; stuck `APPLYING` | Build + test per-source in the slice; default to `UNCONFIRMED` (retry-safe) over guessing success |
| **REVIEW-queue flood** during seeding | Early auto-rate is low; unattended user returns to a huge queue → "it's broken" | **Set the expectation in onboarding copy**; treat the first weeks as a seeding/learning period |
| **Rewrite scope / second-system effect** | Foundation + feature ambition stacked → never reaches parity | Vertical slice first; **defer non-core features to v3.1** (below) |
| **Login-at-scale** (Workday-class per-company accounts) | Breaks the "few known logins" assumption | Scope to **no-login ATS first** (Greenhouse/Lever/Ashby); avoid Workday/iCIMS (also API-closed) |

### Phase -1 research outcome (2026-05-26) — risks 1, 3, 4 retired; risk 2 reshaped

Findings live in `.claude/skills/auto-applier/research/`. Summary:

- **① Seeding — SOLVED, free.** No "list all companies" API, but board IDs are just the public URL slug
  (`boards.greenhouse.io/{token}`), and the read APIs are public/unauthenticated. Phase 1 reuses MIT-licensed
  datasets (`outscal/OpenJobs` ~12k, `ats-scrapers`/jobhive 86k+) → filter → confirm-probe; Phase 2 adds
  `site:boards.greenhouse.io` dork harvesting + name→slug generation. → `ats-discovery-seeding.md`.
- **③ Fabrication guard — SOLVED, free, local.** Layered fail-closed pipeline: L1 deterministic entity/date/
  number matching vs the bank (near-100% precision on the worst lies) → L2 sentence-transformers retrieval →
  L3 local NLI cross-encoder (FEVER DeBERTa) → L4 Ollama self-check (notes only, never clears a flag). Bias to
  REVIEW. → `fabrication-guard.md`.
- **④ Confirmation — SOLVED, concrete signals.** Greenhouse → `/confirmation` redirect; Lever → `/thanks`;
  Ashby → in-place "Application submitted" panel (corroborate with `success:true` on the submit XHR). Mark
  `APPLIED` only on a positive signal; never off email alone; else `UNCONFIRMED`→REVIEW. → `ats-form-automation.md`.
- **② Form automation — RESHAPED (the one real caveat).** Form *mechanics* are friendly (native
  `<input type=file>`, stable per-ATS selectors: Greenhouse element IDs, Lever `name` attrs, Ashby
  `_systemfield_*`). **But all three ship invisible behavioral CAPTCHA by default** (Greenhouse reCAPTCHA,
  often *Enterprise*; Lever invisible hCaptcha; Ashby invisible reCAPTCHA), and **that — not the form — is the
  auto-vs-assisted gate.** Also: **Ashby is a React SPA with no `<form>` and an XHR submit** (trickiest), and
  per-posting custom questions have unstable IDs that must be discovered at runtime by reading labels.

> **The single most important number for the whole project is the invisible-CAPTCHA auto-pass rate** (how
> often patchright + real Chrome + human-like behavior clears the invisible challenge without a visible one).
> If it's high, auto-apply is real and the throughput thesis holds. If it's near-zero (esp. Greenhouse
> Enterprise), v3 is really "great discovery + generation + **assisted** apply" — still valuable, but a
> different value prop. **Measuring this on Greenhouse is the explicit primary goal of the Phase 1 slice.**
> This is also why **maximal stealth (§8c) is justified** — it directly determines that pass rate.

### Scope split — v3.0 (prove it works) vs v3.1 (make it richer)

- **v3.0 core:** foundation (SQLite, state machine, async workers, observability spine); the reliable apply
  pipeline (generate → guard → auto/assisted → confirm); web UI + worker service (clean/functional);
  **relay telemetry + auto-update (KEPT — current dev *and* user pain points)**; onboarding; doctor;
  basic application history + outcomes. Basic pacing/rate-limit/rotation and a simple "skip if posted
  comp < floor" stay in core.
- **v3.1 (after core proves out):** configurable Pareto strategy profiles (§8a), salary intelligence +
  market data (§8d), outcome feedback loop (§8e), interactive batch skill-reconciliation (§7b), story bank +
  company research + rich analytics/what-to-learn trends, branded UI polish.

## 11b. Phased build order (vertical-slice first)

- **Phase -1 — Research & discipline (research-first; NO code).** Before scaffolding anything, research the
  §11 risks and their mitigations and **write the findings into the repo's `auto-applier` skill**
  (`.claude/skills/auto-applier/research/`): ATS company-list seeding, cross-ATS form automation + CAPTCHA +
  confirmation, fabrication-guard techniques (and later, market-salary sources for v3.1). This establishes
  the knowledge base that keeps the build on track and produces a **go/no-go read on the auto-apply thesis**
  that feeds the Phase 1 slice. The `auto-applier` skill is the entry point for every subsequent session.
- **Phase 0 — Foundation. ✅ DONE (2026-05-26, branch `v3`).** `auto_applier/` package skeleton; SQLite schema +
  repositories (`auto_applier/db`, app.db); Pydantic typed config + validation (`auto_applier/config`); `@stage` event spine +
  separate local `events.db` (`auto_applier/pipeline`, `auto_applier/telemetry`); job state machine with allowed-transitions
  table (`auto_applier/domain/state.py`); minimal CLI (`av3 init-db|doctor|status`) + doctor preflight. 37 tests green;
  CLI verified end-to-end. No UI. *Decisions logged: separate `auto_applier/` package (§3); separate `events.db` (§4).*
- **Phase 1 — VERTICAL SLICE (the de-risking spike): Greenhouse end-to-end.** One source, one master fact
  bank, real jobs, crudest CLI/storage: seed company list → discover → score (JD vs profile) → generate
  résumé → **fabrication guard** → `BROWSER_AUTO` on the hosted form → **positive-confirmation** detection.
  **Decision gate:** if generate + guard + auto-apply + confirm hold on real jobs, proceed; if not, revisit
  the auto-apply thesis *before* building the platform. Cheapest place to learn the core risk.
  - **IN PROGRESS (2026-05-26).** Built + verified in code: Greenhouse **discovery** (live API, risk ①
    retired), **fabrication guard L1** (deterministic, 13-case eval, risk ③ retired), **confirmation +
    CAPTCHA detectors** (risk ④ retired), and the **apply driver + dry-run CAPTCHA-presence survey**.
    First live survey result (n=3 real forms, directional): **100% reCAPTCHA *Enterprise* on real GH forms**
    — leading indicator that GH auto-pass will be LOW (→ value prop tilts to discovery+generation+**assisted**
    on GH). Also: ~40% of valid GH tokens redirect to wrappers (skip), and seed tokens decay (confirm-probe
    always). See `research/ats-form-automation.md` "First live CAPTCHA-presence survey".
  - **STILL OPEN (the decision-gate inputs):** (a) the **actual auto-pass rate** — needs gated real submits
    (presence ≠ pass); (b) **score + generate** steps still use stubs (need Ollama/Gemini wiring); (c) a
    **larger confirm-probed survey** to firm up the n=3 Enterprise read; (d) likely **pull Lever/Ashby
    forward** (lighter-touch CAPTCHA than GH Enterprise) before over-investing in the GH auto path.
- **Phase 2 — Source breadth.** Formalize the capability model; add Lever/Ashby/Workable/SmartRecruiters
  (no-login ATS first), then browser boards (Dice/ZipRecruiter/Indeed); discovery producer; canonical dedup
  + ghost filter; per-source breadth policy; resolve the company-list seeding approach.
- **Phase 3 — Pipeline hardening.** Embedding pre-filter; full staged-worker queue; two-tier answer resolver;
  session-expiry graceful degradation; fail-fast→REVIEW; scoring eval harness; mocked-source CI + live smoke
  tests; retention + backups.
- **Phase 4 — Web UI + worker service. ✅ DONE (2026-05-29, six sub-phases).**
  Async :class:`SchedulerService` wrapping the staged-worker scheduler in a FastAPI lifespan + Alpine.js
  dashboard (live pipeline, review queue + login-needed badges, application history + outcomes) + SSE
  event feed + thread-safe :class:`ControlState` union (manual / F6 hotkey / idle-detect sources) +
  headed-browser launcher for login-on-demand + assisted-submit flow + seven-step
  guided-but-skippable onboarding wizard + one-click launcher (`av3 launch` +
  `scripts/av3-launcher.{cmd,sh}` wrappers). 98 new tests across the six sub-phases
  (full v3 suite 495 green, 11 deselected by design). Live-config reload + LLM résumé-extract +
  NL-intent targeting + branded polish → v3.1. See `research/web-ui-and-service.md` for the
  per-sub-phase decision rationale.
- **Phase 5 — Observability & distribution (KEPT pain points). ✅ DONE (2026-05-29, six sub-phases).**
  (1/M) `cli errors|stats` local triage from `events.db`. (2/M) telemetry mirror queue + categorized
  scrubbers (answer value / EEO never mirror — enforced structurally). (3/M) `cli telemetry on|off|status` +
  `cli export-diagnostics` (scrubbed by default, `--raw` PII-bearing escape hatch). (4/M) owner-hosted
  Cloudflare Worker relay template (`relay/`, Turso write token in env) + `MirrorClient` drainer +
  `av3 mirror drain` (out-of-band, never blocks the loop) + `doctor.check_relay_reachable`. (5/M) lean
  PyInstaller installer (`build.py`/`run.py`, Chromium fetched on first run via `av3 install-browser`,
  frozen-launcher fix) + GitHub-Releases auto-update check (`av3 update`, check+prompt, no auto-replace).
  (6/M) fresh v3-first `CLAUDE.md` + this wrap-up. 90 new tests across the six sub-phases (full v3 suite
  **612 green**, 11 deselected by design). Per-sub-phase rationale:
  `research/observability-and-distribution.md`.
- **→ Ship v3.0.** ✅ All v3.0-core phases (0–5) complete. Remaining work is v3.1 (Phase 6).
- **Phase 6 — v3.1 (after core proves out). CORE COMPLETE (2026-05-30).** Independent sub-phases, no mandated
  order; per-sub-phase rationale in `research/phase6-v3.1.md`.
  - **(1/M) per-job résumé-path rewire. ✅ DONE (2026-05-30).** The apply worker now reads the optimize-
    generated per-job résumé + cover letter (derived from `job.id` via `av3.resume.generate`'s path helpers;
    file existence is the durable contract — no DB column) and records both on the `Application` row; the
    single global `artifacts/resume.pdf` is demoted to a fallback for jobs queued before optimize ran. Closes
    the oldest carry-over (auto-apply was uploading a generic résumé). +4 tests (full suite 616 green).
  - **(2/M) configurable Pareto strategy profiles (§8a). ✅ DONE (2026-05-30).** `auto_applier/config/strategy.py`
    (StrategyProfile / RiskBias / EffectivePacing / PROFILE_PRESETS / `resolve_strategy`) + `StrategyConfig`
    on Settings + `PacingConfig.risk_bias`. `ApplyWorker` resolves the active profile once and drives
    inter-apply delay, per-company cap, **soft daily target** (defers, never blocks), and **risk-router
    bias** (Cautious → assisted starting mode; safety-floor downgrade still fires on top). Balanced preset ==
    v3.0 defaults (backward-compat). Concurrency + session-rotation knobs landed in (8/M). +15
    tests (full suite 631 green).
  - **(3/M) salary intelligence §8d. ✅ DONE (2026-05-30).** `auto_applier/resume/salary.py` (SalaryRange,
    SalaryRecommendation, `recommend_ask` posted→market→user, `parse_posted_range`, `is_below_floor`,
    pluggable `MarketDataSource`/`NoMarketData` default-OFF for local-first). `SalaryConfig{floor,ceiling,
    market_source}` on Settings. Apply worker computes a per-job ask (config + posted comp + market) and sets
    it on the resolver's SALARY branch; score worker runs the comp-filter pre-LLM (`comp_skipped`). BLS OES =
    opt-in future adapter (no default egress). +37 tests (full suite 668 green).
  - **(4/M) outcome feedback loop §8e. ✅ DONE (2026-05-30).** `outcomes` table + `OutcomeKind` (funnel-ranked)
    + `Outcome` model + `OutcomeRepo` (record + `applied_with_outcomes` join feed). `auto_applier/analytics.py`:
    `compute_conversion_report` (conversion by source/title/score-band; silent-applied = implicit ghost) +
    `recommend_weight_nudges` (advisory only, gated behind `MIN_SAMPLES_FOR_NUDGE=20`). CLI `av3 outcome` +
    `av3 analytics` (`--json`). Auto-tuning is surfaced-not-applied (Rule 2.6). +24 tests (full suite 692 green).
  - **(5/M) interactive batch skill-reconciliation §7b. ✅ DONE (2026-05-30).** `auto_applier/reconcile.py`
    (deterministic JD skill extraction over a curated vocab; `record_batch_gaps` wires the dead
    `SkillGapRepo`; `build_proposals` ranks; `apply_proposals` additively inserts) + `SkillGapRepo.set_status`
    + `JobRepo.list_all_with_description` + `av3 reconcile [--scan] [--min-count] [--apply]`. Preview is
    read-only; fact-bank insert gated behind `--apply` (Rule 2.6). +22 tests (full suite 714 green).
  - **(6/M) what-to-learn trends §10/§7b. ✅ DONE (2026-05-30).** `SkillGapTrend` + `compute_skill_gap_trends`
    + `ScoreRepo.totals_by_job` + `av3 learn`. Folded into the same commit as 5/M (e7a5546). Full suite 726 green.
  - **(8/M) strategy concurrency + session-rotation knobs §8a. ✅ DONE (2026-05-30).** `EffectivePacing`/
    `PacingConfig`/`PROFILE_PRESETS` gained `concurrency` (declared parallel ceiling — Cautious 1 / Balanced
    1 / Aggressive 3; worker still sequential, read-not-acted) and `session_rotation_min` (per-source
    time-box; Balanced 0.0 = v3.0 invariant). New `SessionRotationPolicy` (pure, clock-injectable);
    `ApplyWorker` gained a `rotation_clock` ctor param + `ApplyRunSummary.rotated`; `run_once` softly defers
    once a source's budget elapses. `av3 apply` line gained `deferred=`/`rotated=`. +8 tests (full suite
    **734 green**). **Phase 6 core complete.**
  - **(7/M) branded UI polish + interactive reconciliation conversation. ✅ DONE (2026-06-11).** Brand layer
    on the dashboard (mark + sticky topbar + pill nav + tokenized radii/shadows/accent-soft, favicon,
    local-first footer tagline; system fonts, no build step, dark mode + keyboard nav preserved) and the
    §7b **interactive skill-reconciliation conversation** as a web surface: `/reconcile` page (Alpine.js) +
    `GET /api/reconcile/proposals` / `POST /api/reconcile/scan` / `POST /api/reconcile/apply` — surface
    gaps, user checks the skills they actually have, confirm is the only fact-bank mutation (additive,
    Rule 2.6). +12 tests.
  - **(9/M) STAR+R interview story bank. ✅ DONE (2026-06-11).** v2 port, rebuilt on the v3 grain:
    `auto_applier/resume/story_bank.py` generates 3 stories per job from the **fact bank** (not raw résumé
    text — same fabrication rule as generation) via local Ollama; append-only `story_bank.json` under the
    data dir; `av3 stories generate <job_id> | list | export`. On-demand only — nothing in the pipeline
    calls it. +24 tests.
  - **(10/M) on-demand company research. ✅ DONE (2026-06-11).** v2 port: `auto_applier/research.py` builds a
    grounded briefing (what-they-do / tech-stack / culture / red-flags / questions / talking-points) from
    **user-pasted** source material via local Ollama — "not in source" beats invention; zero egress by
    construction. Saved md+json under `research/`; `av3 research <company> [--source-file F | stdin]
    [--show]`. +19 tests.
  - **Phase 6 COMPLETE (2026-06-11, full suite 889 green).** All planned v3.1 sub-phases shipped; no
    deferred remainder.

---

## 11a. Distribution, updates & onboarding

- **Installer + auto-update.** One bundled installer (Python + FastAPI server + Playwright Chromium); a
  one-click launcher starts the worker+server and opens the dashboard tab. The app checks a release feed
  (e.g. GitHub Releases) and **prompts to update** — important because selectors and source quirks drift,
  so keeping users current is itself a reliability feature.
- **Onboarding: guided but skippable.** First-run wizard: upload résumé → **review the extracted fact bank**
  (§6b) → set targeting (§6c) → telemetry opt-in (§9) → manual logins **on demand** (when a source first
  needs one). Power users can skip to the dashboard. *(Strategy-profile selection (§8a) appears here only in
  v3.1; v3.0 uses sensible fixed pacing.)*
- **Telemetry relay** is owner-hosted infra, deployed once (free serverless), independent of the client
  installer (§9).

## 12. Open questions to resolve before Phase 2+

- ~~Which API ATS post-endpoints allow programmatic submit vs. discovery-only?~~ **RESOLVED (§6a):**
  none usable for us — APIs are discovery-only, browser carries all submits.
- ~~Turso schema ownership~~ **RESOLVED:** one shared DB; rows keyed by `user_id = sha256(handle)[:10]`
  (§9) so the owner can attribute issues within the known group.
- ~~Embedding model choice~~ **RESOLVED (default):** start with Ollama `nomic-embed-text` (fast, small);
  revisit `mxbai-embed-large` only if golden-set recall is poor. Tunable in config.
- ~~Keep continuous-run mode?~~ **RESOLVED:** always-on *is* the operating model (§7a); no separate flag,
  one-shot `cli run --once` retained for testing.
- ~~UI: Tkinter or web?~~ **RESOLVED:** local web UI + background worker service (§3, §10); Tkinter dropped.
- ~~v2 data migration?~~ **RESOLVED:** fresh start, no importer; v2 is reference/lessons only (§4).
- ~~Focus-stealing default?~~ **RESOLVED:** F6 global control-handoff hotkey + optional idle-detection (§7a).
- ~~Job targeting?~~ **RESOLVED:** NL intent → LLM → editable structured filters (§6c).
- ~~Multi-résumé model?~~ **RESOLVED:** dropped; one master fact bank, résumé generated per job, guarded (§6b).
- ~~LinkedIn?~~ **RESOLVED:** cut from v3 (§6).
- ~~LLM backend?~~ **RESOLVED:** local Ollama (no GPU gate); deterministic bank/rule path is the floor below it. The Gemini cloud secondary tier was **removed 2026-06-09** — `gemini-1.5-flash` was retired by Google (the `v1beta` endpoint 404s for new keys, producing spurious fail-closed jobs), and a cloud tier conflicts with the local-first, zero-cost design. Ollama is the only model tier.
- ~~Mid-form apply failure?~~ **RESOLVED:** fail fast → REVIEW, no retry (§8b).
- ~~Novel form questions unattended?~~ **RESOLVED:** two-tier resolver — semantic match → bail-to-REVIEW default → confidence-gated backup; flagged + telemetry loop (§8b).
- ~~Submit confirmation strictness?~~ **RESOLVED:** positive confirmation required; else FAILED→REVIEW (§5, §8b).
- ~~Which v2 extras survive?~~ **RESOLVED:** keep story bank + company research + skill-gap trends; **cut** follow-up emails + outreach.
- ~~Telemetry credential safety?~~ **RESOLVED:** owner-hosted thin relay holds the token; app never ships it (§9).
- ~~Session expiry unattended?~~ **RESOLVED:** pause that platform + dashboard flag, others keep running (§8b).
- ~~Distribution/updates?~~ **RESOLVED:** bundled installer + auto-update feed (§11a).
- ~~Onboarding depth?~~ **RESOLVED:** guided but skippable wizard (§11a).
- ~~Résumé output format?~~ **RESOLVED:** ATS-safe single-column PDF (§6b).
- ~~Fact-bank seeding?~~ **RESOLVED:** multiple sources merged + user review (§6b).
- ~~Scoring control?~~ **RESOLVED:** smart defaults + power-user knobs; bar independent of strategy profile; ghost filter always on (§10).
- ~~Re-apply policy?~~ **RESOLVED:** never same job; same company rate-limited, configurable (§7).
- ~~Concurrency model?~~ **RESOLVED:** asyncio + Playwright async API (§3).
- ~~Source build order?~~ **RESOLVED:** ATS-first — Greenhouse + Lever, then Ashby/Workable/SmartRecruiters, then browser boards (§11 Phase 2).
- ~~Testing approach?~~ **RESOLVED:** dry-run + fixtures + eval + mocked-source CI + live smoke tests (§10).
- ~~Cover letter timing?~~ **RESOLVED:** always generated for every auto-apply (§7a Strict gate).
- ~~Active hours?~~ **RESOLVED:** 24/7 default + optional quiet hours; pacing keeps cadence human (§7a, §8c).
- ~~Browser profile model?~~ **RESOLVED:** one persistent shared Chrome profile (§8c).
- ~~Anti-detect depth?~~ **RESOLVED:** maximal stealth, proactive — patchright+real Chrome primary, nodriver/camoufox retained, full behavioral layer (§8c).
- ~~Data retention/backup?~~ **RESOLVED:** auto-prune ephemera, keep APPLIED forever, periodic local backups (§4).
- ~~EEO/demographic answers?~~ **RESOLVED:** submit user-provided self-ID, else "prefer not to answer"; never mirrored (§8d).
- ~~Work-auth handling?~~ **RESOLVED:** captured explicitly in onboarding, no silent default (§6b, §8d).
- ~~Salary fields?~~ **RESOLVED:** salary intelligence (user range + posted range + BLS OES market data) + comp filter that skips below-floor jobs (§8d).
- ~~Fact-bank merge conflicts?~~ **RESOLVED:** keep all variants, user picks canonical in onboarding review (§6b).
- ~~Market-data source beyond BLS OES (Adzuna key? acceptable scope)?~~ **RESOLVED (Phase 6 3/M):** market
  data is a pluggable, **opt-in** `MarketDataSource` adapter, default `"none"` (zero egress); the
  recommendation math runs fully locally on posted-range + user-range. Any concrete adapter (BLS OES,
  Adzuna) is a future opt-in entry in `build_market_source` — never a silent default (§8d).
- ~~Data at rest?~~ **RESOLVED:** OS disk encryption + strict file perms (not app-level DB encryption — key paradox); field-level optional later (§4).
- ~~Doctor scope?~~ **RESOLVED:** LLM reachable, DB writable+backups, logins valid, browser/disk/relay ready (§3).
- ~~Prompt/model management?~~ **RESOLVED:** versioned prompt files + config model presets, eval-gated, not user-editable (§10).
- ~~Dashboard scope?~~ **RESOLVED:** live pipeline, review/login badges, history+outcomes, analytics (§10).
- ~~Extras timing?~~ **RESOLVED:** story bank + company research **on-demand only** (§11 Phase 6).
- ~~Fact-bank growth?~~ **RESOLVED:** optional toggled batch skill-reconciliation conversation (combined cross-source) + passive gap-recurrence proposals when off (§7b).
- ~~Discovery breadth?~~ **RESOLVED:** per-source-type — all-new for boards, bounded+rotating for ATS (§7b).
- ~~Locale?~~ **RESOLVED:** US-first, region-agnostic model (no hardcoded US assumptions) (§2).
- ~~Outcome learning?~~ **RESOLVED:** lightweight local feedback loop — insights + bounded auto-tuning (§8e).
- ~~Login UX?~~ **RESOLVED:** auto-open headed browser on host + dashboard prompt (§8b).
- ~~Cover letter style?~~ **RESOLVED:** default concise & tailored (~150–250w), configurable, data-revisable via §8e (§6b).
- ~~UI polish?~~ **RESOLVED:** polished & branded, accessible (§10).
