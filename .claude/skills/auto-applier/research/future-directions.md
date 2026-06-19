# Future Directions — Auto Applier v3.2+ (product-ization)

**Status:** planning / pre-build. Written 2026-06-16 after the first real `--no-dry-run` (PlanetScale
→ Greenhouse emailed-security-code gate; see `automated-apply-go-live.md` "FIRST REAL SUBMIT").
**Purpose:** four product directions the owner wants fully thought through *before* building, so the
build is a breeze. Pros/cons, concerns, and a concrete phased plan for each. Nothing here is built
yet. When a direction is greenlit, its design moves into `docs/v3-architecture.md` (the spec) and
this doc links to it.

## The arc these four share

Today v3 is, in practice, a **personal discovery + scoring tool** for one user
([[project_personal_search_goal]]): it finds and ranks jobs; apply is experimental and the owner
applies by hand. These four ideas together move it toward **an onboardable product that closes the
loop** — anyone can set it up, tell it what they want, and see what happened after they applied.

**Cross-cutting principles (do not break any of these for any direction below):**
- **Local-first, zero-egress.** The core pipeline costs nothing and sends nothing. The only sanctioned
  egress remains opt-in scrubbed telemetry (spec §9). Any new feature either stays on-device or is an
  explicit, optional, off-by-default opt-in with the egress named.
- **Low setup friction.** Target audience includes non-technical people (3–4 person group, friends).
  Every new capability needs a guided, click-through path or it won't get used. "Requires a Google
  Cloud project" is a non-starter for a default path.
- **The honesty invariants are sacred.** Fabrication guard, never-auto-submit-an-essay, never-bypass-a-
  verification-gate, APPLIED-only-on-positive-confirmation. New surfaces must reinforce these, never
  erode them to raise an automation rate.

## Open decisions to settle before building (the forks)

1. **(#1) Slug source:** bundle an open dataset (OpenJobs 12k / ats-scrapers 86k) as the company→ATS→slug
   backbone — dataset-only v1, or add an optional local web-search tool (SearXNG vs `ddgs`)?
   **Recommendation: dataset-only v1; web search is a v2 enhancement, never load-bearing.**
2. **(#4) Email connect method:** IMAP + app-password (provider-agnostic, no developer-side Google
   registration) vs Gmail OAuth (smoother click-auth but heavy developer burden + scary unverified-app
   warnings). **Recommendation: IMAP-app-password first with a guided wizard; OAuth is a later "premium"
   path.** Keep the whole layer provider-agnostic so it's not Gmail-locked.
3. **(#2) Dashboard:** incremental evolution of the current Alpine.js no-build app vs a framework
   rewrite. **Recommendation: incremental; do not rewrite for its own sake.**

## Recommended sequencing

1. **#4 (email outcome loop) first** — independent, local-first-feasible, plugs into an outcome model
   that *already exists*, and immediately makes apply results legible (it would have resolved the
   PlanetScale ambiguity for free).
2. **#1 (onboarding journey) second** — the biggest scope and the thing that makes it not-just-the-
   owner's-tool. Gated on the slug-source decision.
3. **#3 (security-code stand-down test)** — opportunistic: learn it during the next real assisted
   submit, no dedicated build.
4. **#2 (dashboard overhaul) last** — it's the surface that displays what #1 and #4 produce; scoping it
   first would be guessing at data that doesn't exist yet.

---

# Direction 1 — Conversational onboarding + goals → targeting → company slugs

**What it is.** Replace (or front) the current form-filling wizard with an AI intake that does what the
owner experienced in chat: take a résumé, elicit needs/goals/wants, and turn them into the targeting
config **and** a verified set of company board slugs to discover against. The repeatable, productized
version of "the journey."

**What exists today** (`web/onboarding.py`, `onboarding.html/js`): a step-wise wizard (profile → fact
bank → work-auth → targeting → telemetry → web prefs) that **hand-types structured fields**. Two pieces
are missing relative to the journey:
- **Résumé → fact bank by extraction** — the spec deliberately deferred "upload résumé → LLM extract →
  review" (it's research-heavy: model choice, prompt-version pinning, variant-merge — spec §6b).
- **Goals → company slugs** — *entirely* missing. Targeting is a structured filter form + hand-listed
  slugs (`targeting.{greenhouse,lever,ashby}_boards`). Nothing translates "burned-out DBA, remote, EU
  relocation, better WLB" into *which companies on which ATS fit*.

## The hard question: can a local Ollama model do the web research Claude did?

**Honest answer: an 8B-class local model can do *a* version of it, but it is brittle and should NOT be
load-bearing. Architect so the dataset + deterministic probe carry correctness, and the LLM is a
bounded judge/ranker/extractor — not an autonomous researcher.**

Why local web research is weak:
- The model (qwen3:8b — the realistic ceiling on the target hardware: RTX 3080 + 16GB RAM, [[user_profile]])
  does support **tool-calling**, so you *can* build a ReAct loop: LLM emits a query → a local search
  tool runs it → fetch + extract page text → feed back → repeat. The plumbing is free and local
  (**SearXNG** self-hosted metasearch, or the **`ddgs`/DuckDuckGo** python lib — no API key; fetch via
  `httpx` + `trafilatura`/`readability` for text extraction).
- But an 8B model is materially worse than a frontier model at the things web research actually needs:
  multi-step planning, knowing when it has enough, cross-source synthesis, source-quality judgment, and
  **not hallucinating** company facts. Left to roam, it will get stuck, pad, or invent. That is exactly
  the failure mode the fabrication guard exists to prevent — we should not invite it into discovery.

**The reframe that makes #1 buildable — you barely need web research for the slug part:**
- `ats-discovery-seeding.md` already establishes there is **no list-all endpoint** for any ATS, AND
  that **two MIT-licensed datasets already solve seeding**: `outscal/OpenJobs` (~12,144 companies, ATS
  links) and `kalil0321/ats-scrapers` (**86,000+ companies across 47 ATS**, one CSV per ATS). Bundle
  one (or both, deduped) and the "find companies that match criteria" task becomes **filtering an
  offline 86k-row table**, not crawling the web.
- The **confirm-probe** mechanic is already built (`DiscoverWorker`: one public GET per slug → valid/
  empty). So slug *correctness* is deterministic and free.

So decompose the journey into five steps, with the LLM's autonomy bounded at each:
1. **Résumé → fact bank** (the deferred extraction). LLM extracts structured fields from pasted/uploaded
   résumé text → user reviews every field before save. Bounded: extraction + the human is the gate.
2. **Goal elicitation** (conversational). A scripted multi-turn Q&A (role family, location/remote, comp
   floor, relocation, deal-breakers, WLB priorities). Local LLM is *fine* here — it's structured
   conversation, not research. Output: the `TargetingConfig` fields + soft preferences.
3. **Goals → candidate companies** via the **bundled dataset**, filtered deterministically by the
   structured criteria (ATS in {gh,lever,ashby}, region, and — where the dataset has it — industry/size).
   LLM role: *rank/justify* the filtered candidates against the soft preferences (a bounded judge over a
   finite list), never open-ended discovery.
4. **Confirm-probe** each candidate slug (existing `DiscoverWorker` mechanic) → keep only live boards
   with ≥1 open job → write into `targeting.*_boards`.
5. **(v2, optional) Web-search expansion** for companies *not* in the dataset (newer/smaller), behind a
   bounded harness: deterministic code issues the `site:` dorks / `ddgs` queries, the LLM only parses
   slugs and judges fit over the returned snippets. Off by default; the dataset path stands alone.

## Pros
- Turns the tool into something a non-technical person can actually start using — the #1 product unlock.
- The hard correctness work (which company, which slug, is it live) is **deterministic and already
  built**, so quality doesn't ride on an 8B model's research ability.
- Reuses a lot: `av3 ask` copilot (conversation), `av3 research` (company briefings), `DiscoverWorker`
  (probe), `TargetingConfig` (storage), the onboarding wizard shell.
- Résumé extraction is a long-standing spec want; this is the forcing function to finally land it.

## Cons / concerns
- **Résumé extraction is genuinely research-heavy** (the reason it was deferred): model/prompt pinning,
  multi-format parsing (PDF/DOCX → text), and fact-bank variant-merge. Needs its own small eval harness
  (a handful of real résumés with hand-checked expected output) so we don't regress silently.
- **Dataset freshness/coverage:** open datasets lag and miss small/new companies. Mitigation: confirm-
  probe drops dead slugs; the v2 web-search expansion fills gaps; show the user the candidate list to
  approve, never silently commit.
- **Conversation quality on 8B:** goal elicitation can feel robotic or miss nuance. Mitigation: script
  the question flow deterministically; let the LLM phrase/branch, not drive.
- **Scope creep:** this is the biggest of the four. Phase it hard (see plan) and ship the dataset path
  before touching web search.
- **Licensing/attribution:** bundling MIT datasets is clean but needs an attribution note in-repo.

## The plan (phased)
- **Phase A — Résumé → fact bank. ✅ SHIPPED 2026-06-16 (CLI).** `resume/extract.py` (pdfplumber/
  python-docx/txt → text; `extract_factbank` → coerce → `FactBank.from_dict`; `merge_extracted`
  preserves user-entered work-auth/EEO/relocation), `prompts.EXTRACT_FACTBANK` (faithful, individual
  skills, every role), `av3 extract-resume <file> [--save] [--json]`. Live-verified on a real résumé:
  faithful, complete (3 roles + 63 skills), stable. **qwen3 finding:** use Ollama's API `think:false`
  (NOT the in-prompt `/no_think` token — it randomly dropped roles) + a `num_predict` bound;
  `complete_json` now supports both. **Wizard UI (Phase C, slice 1) SHIPPED:** the contact step has a
  résumé upload → `POST /api/onboarding/extract-resume` (base64-in-JSON, no multipart) → pre-fills
  contact/work/skills for REVIEW (no auto-save; per-step Save still the only writer). Browser-verified
  (upload → fields populate, 0 console errors). **Phase C slice 2 SHIPPED:** the targeting step has a
  "Find companies in my field" button → BACKGROUND probe (`POST /api/onboarding/seed-boards/start` +
  `/status` polling; runs off-loop via `asyncio.to_thread` so the ~1 req/s sweep never blocks and the
  user keeps onboarding) → merges verified-live boards into `targeting.*_boards`. Browser-verified
  (live "probed N · found M" counters update, server stays responsive).
  **✅ Extraction eval harness SHIPPED 2026-06-16 — Direction 1 is now FULLY complete.**
  `tests/eval/resumes/{dba_platform,early_career,career_changer}.{txt,golden.json}` (3 synthetic,
  no-PII résumés covering grouped-skill splitting, faithfulness-under-sparsity, and 4-role /
  same-company-two-titles / contractor completeness) + `tests/eval/test_resume_extract_quality.py`,
  mirroring the `test_score_quality.py` pattern. The guard is two-sided: FAITHFULNESS = every
  extracted atom (company/title/skill/cert/institution/degree/metric/name) is token-subset-grounded
  in the source text (the fabrication-guard analog; bullets + normalized dates excluded);
  COMPLETENESS vs golden = greedy role match (catches a DROPPED or MERGED role — the qwen3 trap) +
  skills/metrics coverage. 5 always-run OFFLINE tests validate the golden files + the evaluator
  (incl. a self-faithfulness check that fails if any golden atom isn't literally in its résumé, and
  fabrication / dropped-role detectors); 1 `@pytest.mark.eval` LIVE test runs the real model and
  pins faithful + complete with the prompt version in the failure message. Verified: golden
  self-faithful on the first run; full suite 1178 passed / 12 deselected; **live `pytest -m eval -k
  resume` against real qwen3:8b PASSED first attempt (22.4s)** — all 3 fixtures faithful (zero
  invented atoms), zero dropped roles. Run after any `EXTRACT_FACTBANK`/model change:
  `AV3_DATA_DIR=…/JobSearch/av3data pytest -m eval -k resume`.
- **Phase B — Goal elicitation → TargetingConfig. ✅ SHIPPED 2026-06-16.** A scripted goal-elicitation
  CHAT on the targeting step: `auto_applier/onboarding_chat.py` (deterministic ordered steps roles →
  location → comp → priorities; the FLOW is scripted, the local LLM is only a bounded PARSER of each
  free-text answer, with a deterministic keyword/regex fallback so a missing/erroring Ollama never
  breaks the chat — and `comp` is regex-only). `POST /api/onboarding/goal-chat` (stateless: {step,
  answer, draft} → next question; does NOT persist — returns a DRAFT the wizard fills into the
  targeting form for REVIEW, the existing `/onboarding/targeting` writer stays the single writer).
  Prompt `GOAL_ELICIT` (`goal-elicit-v1`, think=False + num_predict=512 per the qwen3 finding). New
  `TargetingConfig.preferences: list[str]` (soft signals; not yet pipeline-consumed — the forward hook
  for Phase C's bounded ranker). Wizard: collapsible "Not sure what to target?" chat panel (Alpine,
  scrollable log). 33 new tests; full suite **1173 green**. Live-verified through the real qwen3:8b
  (correctly parsed target roles, both locations + remote/onsite flags, $140k floor, clean preference
  phrases) + browser-smoked end-to-end (4-turn walk → "Use these answers" fills the form, 0 console
  errors). **Deliverable met:** the chat produces the structured targeting the pipeline consumes.
- **Phase C — Dataset → candidate slugs → probe → boards.** Bundle one dataset as
  `data/ats_companies.csv` (or shipped resource); deterministic filter by criteria; LLM rank over the
  finite candidate list; confirm-probe via `DiscoverWorker`; user approves; write `targeting.*_boards`.
  **Deliverable:** the journey end-to-end, dataset-only. This is the milestone that matches what the
  owner experienced.
- **Phase D (optional) — Web-search expansion.** Bounded harness (SearXNG or `ddgs`), deterministic
  query issuance, LLM as slug-parser/fit-judge only, off by default. **Deliverable:** coverage beyond
  the dataset for users who want it.

**Effort:** large (A, B, C are each a real sub-project). **Dependencies:** none hard; benefits from #2
having a place to show the candidate-approval UI.

---

# Direction 4 — Email outcome loop (confirmations / rejections / interviews)

**What it is.** Connect the user's inbox so the system reads job-related email locally and learns what
happened after applying: real application confirmations, rejections, interview invites — driving job
state automatically instead of the system going blind after apply.

**Why it's high value AND lower-risk than it looks.** The **outcome model already exists**: `state.py`
has the `OutcomeKind` ladder (GHOST → REJECTION → RESPONSE → INTERVIEW → OFFER), and there's an outcome
/ reconciliation subsystem (`reconcile.py`, `analytics.py`, outcome handling in `repositories.py` /
`models.py`, the §8e feedback loop). **Email is the missing automatic *input* to a loop that's already
built.** Only the ingestion side is greenfield. And the motivating example is fresh: an email reader
would have **instantly resolved the PlanetScale ambiguity tonight** — it would have read the
security-code email and reported "gated, not submitted" without a screenshot.

## The hard question: keep setup dead-simple for non-technical people

**Honest answer: neither Gmail method is truly frictionless, so (a) make it provider-agnostic IMAP with
a guided in-app wizard, and (b) keep it strictly optional/off-by-default.**

- **Gmail OAuth:** smoother *click-to-authorize* for the end user — BUT it requires the *developer* to
  register a Google Cloud project, configure the consent screen, and get **verified for the restricted
  Gmail-read scope**, or every user sees a scary "Google hasn't verified this app" warning. For a 3–4
  person tool that's a heavy, ongoing developer burden. Poor fit for "easily reproducible."
- **IMAP + app-password:** the user enables 2FA, generates an app password, pastes it. A few steps, and
  "app password" is jargon — but it needs **no developer-side Google registration**, works for *any*
  IMAP provider (Outlook, Yahoo, Fastmail, self-hosted), and a **guided wizard with screenshots** makes
  it tractable. Most reproducible. The secret lives in `.env` (settings already reserves `.env` for
  exactly this — never the JSON).
- **Recommendation:** IMAP-app-password as the default path + a guided wizard; OAuth as an optional
  later "premium" convenience. Provider-agnostic from day one so we're not Gmail-locked.

## Pros
- Closes the loop: apply outcomes become legible automatically; the funnel (applied → response →
  interview → offer) becomes real data, feeding the existing analytics + the §8e feedback loop.
- Mostly **plumbing onto an existing model** — the outcome ladder, reconcile, and analytics already
  exist; we add ingestion + a classifier + a matcher.
- Fully **local-first**: IMAP fetch + local-LLM classification, nothing to a new third party. Consistent
  with zero-egress (the user reads their *own* mailbox; processing is on-device).
- Would have prevented tonight's confirmation ambiguity outright.

## Cons / concerns
- **Privacy is serious** — it reads email. Must be opt-in, default OFF, scoped to job-related mail,
  read-only, secret in `.env`, and ideally allow a per-sender/label allowlist. Never mirror any email
  content in telemetry (the scrubber has no field for it; keep it that way).
- **Matching email → job is fuzzy.** Heuristics (company domain, thread, job title in the body) + a
  local-LLM classifier; expect false matches. Mitigation: confidence-gated, and ambiguous matches go to
  REVIEW for the human, never an automatic terminal state on a guess (mirrors the resolver's fail-closed
  posture).
- **The APPLIED invariant still holds:** an email alone CANNOT mark APPLIED (spec invariant). Email
  *corroborates*; APPLIED still needs the on-page positive confirmation. Email CAN drive the post-apply
  states (RESPONSE/REJECTION/INTERVIEW/OFFER) and can flag "PlanetScale emailed a security code → finish
  it assisted."
- **App-password jargon** is the main UX wart — the guided wizard is the mitigation, plus an honest
  "this is optional" framing.
- **IMAP quirks** (Gmail's `[Gmail]/All Mail`, label semantics, throttling) — keep the poll polite and
  incremental (since-UID), cache processed message-ids.

## ⚠️ GROUNDED correction (2026-06-16 — read before building; supersedes the phasing below)

A Plan-agent pass verified the wiring against the actual code and a **build-ready plan now lives in
`.claude/unstuck/plan-email-outcome-loop.md`** (the system-of-record for the build). The key
corrections to the optimistic text above:

- **The loop does NOT wire to `reconcile.py`.** `reconcile.py` is **skill-gap** reconciliation
  (JD→fact-bank skill proposals), not outcomes. There is no "outcome reconcile." The real path is a
  **single line** — `OutcomeRepo(conn).add(Outcome(job_id, kind, note))` (`repositories.py:339-344`,
  the exact call `av3 outcome` uses) → analytics (`applied_with_outcomes` → `compute_conversion_report`)
  consumes the `outcomes` table **unchanged**. So the "reuse existing infra" thesis is *more* true
  than stated, just via a different (simpler) entry point.
- **`OutcomeKind` has no `confirmation`/`interview-invite` value** (ladder is GHOST/REJECTION/RESPONSE/
  INTERVIEW/OFFER). The classifier **projects** onto these 5 ("application received" → `RESPONSE`);
  extending the enum is a bigger change (analytics ranking + CLI choices) → v1 reuses the 5.
- **No domain/email column exists** to match on → the matcher keys on `job.url` substring (strongest) +
  normalized `company` + role-token overlap, confidence-gated, fail-to-review.
- **Build phasing is re-cut** so the offline-verifiable slice ships first: **Phase A** = parse +
  classify + match + fixtures + offline tests (no IMAP, no DB writes); **Phase B** = worker + persistence
  + `av3 inbox --dry-run` (proves the `OutcomeRepo`→analytics round-trip via a STUB fetcher — still
  offline; the demoable milestone *before* asking for the app-password); **Phase C** = real `imaplib`
  fetch + scheduler slot + the Greenhouse security-code flag (GATED on the user's Gmail app-password);
  **Phase D** = onboarding wizard + dashboard surface (needs Direction 2). See the plan doc for exact
  module/function signatures and the four open forks (all with recommended defaults).

## ✅ Phases A + B + C SHIPPED (2026-06-16 → 2026-06-19, NOT pushed — commit-only)

Phases A/B shipped offline (commits `8926dbc`, `937b39e`). **Phase C shipped 2026-06-19** (the real
IMAP source + scheduler wiring) — code is fully offline-verifiable; only LIVE mailbox verification is
gated on the user's Gmail app-password. What landed:

- **`auto_applier/inbox/fetcher.py`** — `InboxCreds`, `creds_from_settings(settings) -> InboxCreds|None`
  (reads `$AV3_IMAP_PASSWORD`; `None` when inbox disabled / no user / no password — never raises), and
  **`ImapFetcher`**: a **read-only-by-construction** (`select(readonly=True)` + only `UID SEARCH`/`UID
  FETCH`; *no* STORE/COPY/EXPUNGE) **re-iterable** source of `(uid, raw_bytes)`. Re-iterability is the
  load-bearing design choice — each `for … in fetcher` opens a fresh session, pulls only mail past the
  persisted `last_uid` cursor (or the `since_days` window on cold start), advances the cursor, closes —
  so the always-on scheduler polls by simply re-iterating the same object every cycle. Handles the
  `UID n:*` IMAP quirk (a no-new-mail poll returns the highest existing UID → filtered to strictly-greater
  so the cursor never moves on empty). `advance_cursor=False` = the `--dry-run` posture (repeatable).
- **Engineering call — synchronous fetch, NOT `asyncio.to_thread`** (the plan sketched to_thread): the
  cursor read/write uses the shared single-thread sqlite connection, and offloading the iteration to a
  thread would trip sqlite's same-thread guard. The fetcher's `Iterable[(uid,bytes)]` contract (shared by
  the test stub, `eml_file_source`, and live IMAP) keeps the worker source-agnostic; a brief per-cycle
  poll stall in `serve` mode is acceptable for a local single-user tool. (If it ever bites, the offload
  must move cursor I/O off the shared conn.)
- **Scheduler wiring** — optional `inbox_worker` as the **last gather stage** (runs every cycle, **never
  quiet-gated** — reading mail doesn't drive the browser), isolated like every stage, **inert/absent**
  when the inbox isn't configured. Wired into both `av3 run` and `av3 serve` via
  `_build_scheduler_inbox_worker` (returns `None` when `creds_from_settings` is `None`). Import kept under
  `TYPE_CHECKING` to avoid an inbox→pipeline.stage import-order edge (the stage is driven duck-typed).
- **CLI** — `av3 inbox` (no `--eml`) now does the live fetch when configured, else prints a specific
  `_inbox_setup_nudge` naming exactly what's missing (enabled / user / `$AV3_IMAP_PASSWORD`) + the Gmail
  2FA→App-Password steps; an `imaplib.IMAP4.error` (bad auth) exits 2 with a clear message, never a traceback.
- **Security-code → "finish assisted" (honest, minimal).** The worker already flags verification/OTP mail
  (`security_code_flags`, kind stays None = never an outcome). Phase C adds a summary **nudge note** ("N
  security-code email(s) seen — if an assisted submit is waiting on a code, open your inbox and finish it;
  email never submits for you"). Deliberately **no speculative job-matching or auto-act** — the precise
  job-linking + a dashboard "enter code" button is **Phase D / Direction 3** (where the REVIEW-job-match
  question and the assisted-finish UI actually get resolved). Honesty invariants intact.
- **Tests:** `tests/test_inbox_fetcher.py` (14 offline via a `FakeIMAP` transport that *asserts* read-only
  by raising on any mutating verb; covers creds gating, cold/incremental fetch, the no-new-mail quirk,
  dry-run hold, re-iterability/poll, max-messages cap, worker+fetcher e2e) + 4 scheduler-wiring tests +
  the security-code nudge test + 1 `@pytest.mark.eval` live IMAP smoke (skips unless `$AV3_IMAP_USER` +
  `$AV3_IMAP_PASSWORD` set). **Full suite 1253 passed / 13 deselected.** Live-CLI smoked: `--status`,
  the setup nudge, the offline `--eml` path, and the security-code nudge all behave.
- **✅ LIVE-VERIFIED + configured in production 2026-06-19** (user supplied the Gmail App Password). See
  the live-findings block below. **Phase D** (wizard + dashboard outcomes surface) still depends on Direction 2.

## ✅ Phase C LIVE FINDINGS (2026-06-19 — real mailbox, jar8510@gmail.com, 29 APPLIED jobs)

Validated rejection auto-detection against 7 hand-marked rejections (snowflake, Dataiku ×?, aircall,
Gusto ×2, Monzo ×2). Read-only throughout; no production writes (dry-run analysis script).

- **✅ Detection works — deterministically, no LLM.** The keyword classifier caught real rejections from
  **Aircall / Snowflake / Monzo / Dataiku** ("Thanks for your interest in…", "Update regarding your
  application", "Your application to…", "About your application to…"). Re-confirmed 3 of the 7 hand-marked
  rejections by EXACT job purely from `_REJECTION` keywords. (Offers/interviews/responses also classify.)
- **🐛 FIXED (commit `a558daf`): cold-start cap took the OLDEST 200 UIDs** → on a busy inbox (~23/day) a
  30-day scan covered only the oldest ~8 days and returned **0 outcomes** (every recent rejection hidden).
  Fix: cold start now takes the **NEWEST** `max_messages`; incremental stays oldest-first (catch-up, never
  skips). Post-fix the default 200-cap/30-day scan covers the recent window and finds the rejections.
- **✅ RESOLVED — matching precision for multi-role-same-company** (commit `a0a6e17`). The role IS in the
  body (Greenhouse/Lever/Monzo templates: "your application for the *<Role>* position", "the *<Role>*
  opening"), so the matcher now scores each same-company candidate by whether its **full title appears
  verbatim** in the email text, preferring the **longest** ("Data Engineer II" beats its prefix sibling;
  the named Monzo role beats the other). A unique winner → `company+role` 0.80; a tie / no role signal →
  `company-ambiguous` (job_id=None, 0.50, sub-floor) → fails CLOSED to review, never a guessed sibling.
- **✅ RESOLVED — Gusto** (commit `2c1c817`; my earlier "no email / ATS-relay hint gap" guess was WRONG —
  verified against the real mail). TWO real causes: (1) **classifier** — Gusto's "Regarding your
  Application" rejection says "we *won't* be moving forward at this time" with no "unfortunately"; the
  curly apostrophe (U+2019) broke the `not be moving forward` keyword, so it fell through to RESPONSE (and
  a confident deterministic miss preempts the LLM). Fix: normalize curly→straight apostrophe in the
  haystack + add the contraction phrase. (2) **company match** — the email's domain hint "gusto" never
  equalled the ATS legal name "Gusto, Inc."; fix: strip trailing legal suffixes (inc/llc/ltd/corp/co/…)
  before comparing. **Result: 7/7 hand-marked rejections now detected, each to the exact job.**
- **⚠️ OPEN (safe) — classifier false positives.** A Quora "BREAKING…" digest and CapitalOne CreditWise
  mails tripped a `_REJECTION` keyword, and some "Thanks for applying" *confirmations* (Gusto, Snowflake)
  classified as INTERVIEW via the over-eager `_INTERVIEW` phrase "next steps". The rejection-FPs are
  UNMATCHED → route to review (no false outcome). The **interview-FP is the more important one**: a
  confirmation mislabeled INTERVIEW that DOES match an applied job would record a wrong INTERVIEW outcome.
  Next classifier-precision pass: tighten `_INTERVIEW` ("next steps"/"schedule a" need an interview cue),
  and split RESPONSE vs INTERVIEW more carefully. (Not yet done — flagged for the next pass.)
- **Net:** rejection auto-detection is complete on real data (7/7, exact-job), honesty/safety held
  throughout (no APPLIED writes, no auto-submit, ambiguous→review). Highest-value next improvement =
  the classifier-precision pass above (interview-FP), before a real recording scan banks outcomes.

## The plan (phased — original sketch; see the GROUNDED correction above for the authoritative cut)
- **Phase A — Read-only IMAP ingestion.** `telemetry`-style local module: connect (host/port/user/
  app-password from `.env`), incremental fetch (since last UID), store processed `message-id`s so we
  never re-handle one. Provider-agnostic config; Gmail defaults pre-filled. **Deliverable:** "pull new
  job-related mail locally," no classification yet.
- **Phase B — Local-LLM classifier → OutcomeKind.** `prompts.CLASSIFY_JOB_EMAIL` (versioned): email →
  {kind ∈ confirmation/rejection/interview/offer/other, company, role hint, confidence}. Schema-
  constrained. **Deliverable:** every fetched email gets a structured, on-device classification.
- **Phase C — Matcher + state drive.** Match classified emails to jobs (domain/thread/title heuristics +
  the classifier's company hint), confidence-gated; feed the existing reconcile/outcome path; ambiguous
  → REVIEW. Special-case the **Greenhouse security-code email** → surface "finish this one assisted"
  (ties to the #3 finding). **Deliverable:** inbox drives the outcome ladder automatically.
- **Phase D — Onboarding + dashboard surface.** A guided "connect your email (optional)" wizard step
  (app-password walkthrough) and a dashboard outcomes view (#2). **Deliverable:** a non-technical user
  can turn it on and see results.

**Effort:** medium. **Dependencies:** verify the exact `reconcile.py` / outcome-repo API before Phase C
(it exists; confirm the insertion point). Phase D depends on #2.

---

# Direction 3 — Does the security-code gate "stand down" after one verified submit?

**What it is.** Empirically test the hypothesis that Greenhouse's emailed-security-code gate (found live
on PlanetScale, 2026-06-16) is **session/profile-bound** — pass it once by entering a code, and
subsequent applies in the same persistent Chrome profile sail through — versus **per-application** (a
fresh code every submit regardless of session). The answer decides whether Greenhouse can ever be
full-auto after a one-time human step, or whether the submit press stays human forever.

## The honest framing
- **Cheap to learn, but every test is a real application.** We can't A/B this freely — each trial is a
  real submit to a real employer under the user's name. So learn it **opportunistically** during normal
  assisted use, not as a dedicated burn of applications.
- **The pessimistic case is at least as likely.** The email said "*your* application" and asked to
  "resubmit" — consistent with a per-application code. The optimistic "stands down" case is plausible
  (anti-bot challenges often gate the session) but not assumed.
- **The safe default is already correct:** human presses submit (assisted). The test only tells us
  whether we're *allowed* to do better; it never weakens the default.

## The test protocol (when the owner next does a real assisted Greenhouse submit)
1. Complete job #1 assisted: bot fills, human enters the emailed code, resubmits → confirm it lands
   (on-page confirmation + the "application received" email, distinct from the security-code email).
2. Immediately attempt job #2 at a *different* Greenhouse company in the **same persistent profile**,
   `--mode auto`, watched.
3. Observe: does #2 hit the security-code field again, or submit clean?
   - **Clean → the gate is session/profile-bound.** Full-auto becomes viable *after* a one-time
     human-verified submit per session. Build: a "prime the session" assisted step, then auto for the
     rest of the batch (still APPLIED-only-on-confirmation).
   - **Challenged again → per-application.** Leave the submit press to the human (assisted is the
     ceiling for Greenhouse). Build: the #4 security-code surface ("enter this code") is the best we do.

## Pros / cons
- **Pro:** the build follows from the result — no wasted work guessing.
- **Pro:** costs no dedicated applications if folded into real assisted use.
- **Con:** N=1 per trial and behavior may vary by company/risk-score, so one clean #2 isn't proof —
  needs a few observations before trusting "stands down."
- **Con:** depends on the #4 security-code handling (or manual code entry) to run cleanly.

**Effort:** tiny (observation + a note). **Dependency:** a real assisted Greenhouse submit happening.

---

# Direction 2 — Dashboard / job-tracker overhaul

**What it is.** Rework the web dashboard (currently Alpine.js, no build step: live pipeline, review
queue, login-needed badges, history, SSE feed) into the product's real cockpit.

**Why it's deliberately last.** It's the **surface that displays what #1 and #4 produce.** Until those
exist, an overhaul is guessing at data. Once they do, the dashboard has clear new jobs:
- An **assisted queue**: "3 jobs filled, waiting for you to enter the security code / press submit"
  (the realistic Greenhouse outcome) — turn the abstract REVIEW state into an actionable to-do list.
- **Outcomes from email** (#4): applied → response → interview → offer, per job, sourced from the inbox.
- **The goals/targeting view** (#1): what the user told it they want, and the candidate companies/slugs
  it found — editable.
- **The apply funnel** as first-class analytics (discovered → scored → queued → applied → outcome),
  building on the existing `analytics.py`.

## Pros / cons / concerns
- **Pro:** a coherent cockpit is what makes the loop *felt* — the difference between a CLI tool and a
  product.
- **Con / concern: do not rewrite for its own sake.** The current Alpine no-build app is a feature
  (zero build, easy to ship). A framework rewrite (React/Svelte) adds a toolchain and risk for marginal
  gain. **Recommendation: evolve incrementally** — add the assisted-queue panel, the outcomes column,
  and the goals view as new sections; only consider a framework if the interaction complexity genuinely
  outgrows Alpine.
- **Concern:** keep the reliability/UX rules already in CLAUDE.md — scrollbars on overflow, good
  contrast, keyboard-navigable, live (not console).

## The plan
- **Phase A — Assisted queue panel** (can ship *before* #1/#4): make REVIEW actionable — list jobs
  waiting on a human step, with the reason (security-code / consent / screener) and a "open the
  pre-filled form" button. Directly useful given the Greenhouse gate reality.
- **Phase B — Outcomes column** (after #4): per-job application status from the email loop.
- **Phase C — Goals/targeting view** (after #1): show + edit what the journey produced.
- **Phase D — Funnel analytics** surfaced from `analytics.py`.

**Effort:** medium, incremental. **Dependencies:** Phase B needs #4; Phase C needs #1; Phase A is
independent and arguably the highest-value standalone slice.

---

## Summary table

| # | Direction | Hardest part | Local-first? | Effort | Recommended order |
|---|-----------|--------------|--------------|--------|-------------------|
| 4 | Email outcome loop | low-friction connect (IMAP app-password) | yes (IMAP + local LLM) | medium | **1st** |
| 1 | Onboarding journey | résumé extraction + *not* needing LLM web research (use the dataset) | yes (dataset + probe; LLM bounded) | large | **2nd** |
| 3 | Security-code stand-down test | each trial = a real application | n/a | tiny | opportunistic |
| 2 | Dashboard overhaul | not rewriting for its own sake | yes | medium | **last** (Phase A early) |

Related: [[project_automated_apply_golive]] (the gate finding that motivated #3/#4),
[[project_personal_search_goal]] (the current discovery+scoring-only reality), [[user_profile]]
(target hardware → 8B local-LLM ceiling), [[feedback_no_cost]] (the zero-egress constraint #4 must
honor), [[project_application_copilot_direction]] (the copilot #1 reuses for conversation).
