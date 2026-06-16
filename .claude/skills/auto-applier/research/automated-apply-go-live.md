# Automated Apply — Go-Live Handoff (first real submissions)

> **Status: NOT YET LIVE. No real application has ever been auto-submitted.** This doc is the
> plan + blockers for the first `--no-dry-run` run. Written 2026-06-12.
>
> **2026-06-12 update — both blockers BUILT (pre-flight steps 1–3 done), 974 green:**
> - **Blocker A (human-attestation detector): DONE.** `SensitiveClass.HUMAN_ATTESTATION` added
>   to `resume/answer_resolver.py`; `classify_sensitive(label, options=None)` detects it FIRST
>   (highest priority) via label patterns OR an option PAIR (a human affirmation + an AI/automated
>   option). `_resolve_sensitive` ALWAYS returns REVIEW for it → `any_required_unresolved` →
>   `ASSISTED_PENDING`. `CustomQuestion` gained an `options` field; Greenhouse
>   `discover_custom_questions` now scrapes native `<select>` + radio-group option text (best-effort;
>   react-select menus stay closed → label patterns are the fallback). Covered end-to-end in
>   `test_apply_driver.py::test_human_attestation_gate_downgrades_auto_to_assisted_end_to_end` +
>   resolver unit tests. The bot can no longer auto-tick "I am a human being".
> - **Blocker B (hand-crafted résumé): DONE.** `JobSearch/render_resume_pdf.py` rendered
>   `Joseph_Lira_Resume_Solutions_Engineer.docx` → `av3data/artifacts/resume.pdf` (278 KB) via Word
>   COM. Queue the 5 with the NEW `av3 queue` command (skips optimize → no per-job PDF → worker
>   uploads this good fallback).
> - **`av3 queue <ids>` / `--shortlist NAME --all`: DONE.** DECIDED/REVIEW → QUEUED_APPLY guard
>   (rejects APPLIED/other states), mirrors `av3 pass`. Tests in `test_cli_queue.py`.
> - **Remaining = the WATCHED steps only:** pick 5, `av3 queue` them, run the dry-run dress
>   rehearsal, read per-job results together, then watched `--no-dry-run` one at a time. See
>   "The plan" below — nothing autonomous from here; every remaining step is user-watched.
>
> **Goal:** auto-apply to **5 Solutions Engineer positions** for the personal search
> (`AV3_DATA_DIR=C:\Users\jar85\JobSearch\av3data`) via the validated flow. These would be the
> bot's first real submissions under the user's name. Irreversible + outward-facing → every step
> is watched, dry-run first.

## TL;DR — read this before anything else

1. **The chain works in dry-run** (form-fill validated end-to-end on a live GitLab Greenhouse form
   2026-06-11, 6 bugs fixed — see `ats-form-automation.md` §E2E validation). It has **never made a
   real submit**.
2. **Solutions roles are the WORST category for full-auto, not the best.** They are screener-heavy,
   and many screeners are honesty-sensitive judgment calls (years-of-SE = 0, zero-trust = no,
   Kubernetes = no — the exact questions the user has been answering by hand). The copilot-audited
   resolver **correctly bails** on those → the driver **downgrades to ASSISTED_PENDING**. So the
   realistic outcome is *mostly assisted-pending (bot pre-fills, human submits), few full-auto*.
   That is correct behavior, not a bug. "5 fully automatic Solutions applies" may not be achievable;
   "5 bot-pre-filled, human-finished" almost certainly is.
3. **Two blockers MUST be closed before a real run** (details below):
   - **(A) Human-attestation gate detection** — NEW, required. Forms ask "Are you a human or an
     automated program?" The bot must **never** auto-answer "I am a human being." It is an automated
     program; that answer would be a false attestation, and these gates exist specifically to catch
     bots. Currently **undetected** — the resolver could confidently fill "human being." This is an
     integrity + ToS blocker, not a nicety.
   - **(B) Resume quality** — the optimize stage generates a qwen3:8b résumé (weaker, and the live
     run showed cover-letter fabrication risk). For a real submission, upload the user's hand-crafted
     `Joseph_Lira_Resume_Solutions_Engineer.docx` rendered to PDF, not a generated one.

## Dry-run dress rehearsal #1 (2026-06-12) — 3 live bugs found + fixed

Ran `av3 apply --once --limit 5 --dry-run` against the 5 queued Solutions jobs
(`solutions2`). 4 processed (Tailscale skipped on the per-company rate-limit 2/2), 0 errors,
nothing submitted. Reading the `resolver_inferred` events surfaced THREE real bugs — exactly
why we dry-run first:

1. **Human-attestation gate slipped through (CRITICAL).** Grafana's
   `"Which of the following best describes you?*"` was a **react-select** (AI/human options
   NOT in the DOM at discovery) with a **non-descriptive label** — so both Blocker-A layers
   missed it and the copilot *answered* it (conf 0.75). Fix: added a `_DESCRIBES_YOU` label
   rule (`"…best describes you"` with no demographic noun → HUMAN_ATTESTATION; with one → EEO),
   PLUS a value-side backstop `affirms_human()` in `fill_resolutions` that refuses to ever
   TYPE a human affirmation even if classification slips. The bot can no longer auto-answer
   this gate.
2. **Work-auth phrasing missed (MODERATE).** Grafana's
   `"Are you currently eligible to work in your country of residence?*"` didn't match the
   work-auth patterns ("eligible to work" wasn't covered) → it bypassed the deterministic
   fact-bank policy and went to the LLM. Fix: added `eligib(le|ility).*work` patterns →
   now routes through the §8d work-auth policy (fact-bank or REVIEW, no silent default).
3. **Phone filled wrong on EVERY Greenhouse/Lever form (MODERATE, user-reported).** The phone
   field is **intl-tel-input** and its default flag is the **GLOBE — no country selected**.
   Typing the stored `1-682-718-8130` (national) left it without a dial code → invalid.
   Fix: `normalize_phone()` types `+E.164` (`+16827188130`); verified LIVE that
   intl-tel-input then auto-selects 🇺🇸 US and renders `+1 682-718-8130`. Wired into the
   Greenhouse + Lever drivers.

All three covered by tests (979→ green). Re-rehearse to confirm the gate now BAILS (the two
flagged questions should drop out of `resolver_inferred` / show as REVIEW), then proceed.
Use `av3 apply … --keep-open` so the filled forms stay on screen for visual review.

## Dry-run dress rehearsal #2 (2026-06-12, user-watched) — answer QUALITY is the real blocker

User left the browser open (`--keep-open`) and read every filled field. Verdict: the
mechanical safety rails work, but the **answer quality is bad enough that auto-submitting
would harm the candidacy**. This is the decisive finding — bigger than any single bug.
What WORKED (post rehearsal-#1 fixes): the human-attestation gate now BAILS (left blank,
correct); sponsorship answered correctly. What's BROKEN, by category:

**B1 — Open-ended essay prompts get NEGATED into a wrong yes/no (CRITICAL, harmful):**
- "Why are you interested in a Solutions Engineer role?" → **"Not interested in a Solutions
  Engineer role"** (reverses intent — catastrophic on an SE application)
- "Describe your experience working with sales and customer prospects." → "No, I haven't
  worked directly with sales or customer prospects." (false — 100+ stakeholders, $5M revenue)
- "What is your experience with Kubernetes, Grafana and/or Observability?" → "No, I have not
  used Kubernetes, Grafana, or observability tools." (false — DB-monitoring/observability-adjacent)
- "Why are you interested in joining Tailscale?" → "No, I haven't worked with Tailscale or its
  technologies." (answers a DIFFERENT question)
  ROOT CAUSE: the §8f copilot is built for yes/no SCREENERS; on an open-ended "Why/Describe/
  Tell us" prompt it manufactures a yes/no verdict and fills the negation. AND the good
  paragraphs that were hand-written for exactly these questions live only in JobSearch .docx
  files — they were NEVER seeded into the resolver's answer bank, so Tier-1 can't match them.

**C1 — Profile / contact fields treated as yes/no questions instead of pulled from the bank:**
- "LinkedIn Profile" → claimed he has none (he does — it's in contact.links)
- "Website" → "No, I have not built a website." (the field wants his GitHub/portfolio URL)
- "Preferred First Name" → blank (should be "Joseph")
- "Location (City)" → blank (contact.location)
- "What country and time zone are you based in?" → blank
  ROOT CAUSE: no contact/links field-mapping layer; these reach the copilot, which yes/no's them.

**D1 — Dropdowns left blank (value≠option, or no bank answer):**
- Work-authorization dropdown → blank (now classified correctly, but the bank value
  "US Citizen" doesn't match a Yes/No/authorized option → unfilled)
- "Are you located in or willing to relocate to the United States?" → blank (should be Yes)
- "How many years of experience … customer-facing … sales engineering?" → blank (years select)
- "Do you have experience working at a SaaS company …?" → blank
- "How did you hear about this opportunity at Grafana?" → blank (needs a canned source)
- Tailscale "Candidate Privacy Policy / AI Guidelines" acknowledgment → blank (consent — arguably
  the human SHOULD tick this, esp. as it covers AI-tool use in hiring)
- "Have you used Tailscale before?" → no bank entry; the user had no channel to supply the answer

**E1 — EEO self-identification (gender/race/veteran/disability) all blank.** Bank EEO is empty →
should default to "Prefer not to answer", but the option text likely didn't match, or the section
wasn't discovered. (Blank is safe; not the design intent.)

**A1 — Phone still wrong.** After `normalize_phone`, the country selector correctly shows +1 US
BUT the visible number field ALSO shows "+1 …" (doubled). The Hightouch form I tested live did not
double, so some forms use intl-tel-input `separateDialCode`/`showSelectedDialCode` where the input
must hold ONLY the national number. Robust fix: set the country + number via the iti JS instance
(`getInstance(input).setNumber('+16827188130')`) rather than typing the E.164 string, OR detect the
separate-dial-code class and type national-only. NOT yet fixed.

### The architectural conclusion (the important part)

For automated apply to be SAFE on screener-heavy Solutions roles, three things must hold and two
do not yet:
1. The bot must **NEVER invent an essay answer** — an open-ended prompt with no prepared/bank-backed
   answer must BAIL to assisted (blank for the human), never a manufactured negation. (NOT yet — the
   copilot fills negations. This is the harm vector.)
2. The user's **prepared answers must live in the answer BANK**, not just .docx files, so Tier-1
   semantic match fills them well. (NOT yet — they're only in JobSearch/*.docx.)
3. Contact/profile fields (LinkedIn, GitHub/website, location, preferred name, country/timezone)
   must map from the fact bank, not reach the copilot. (NOT yet.)

Until #1 at minimum ships, **do not `--no-dry-run` any essay-bearing role** — the bot would submit
wrong, self-harming answers. #1 makes it SAFE (mostly-assisted); #2+#3 make it GOOD (higher auto/
better pre-fill). This confirms, more sharply than rehearsal #1, that Solutions roles are the worst
full-auto category — and that the failure mode is "confidently wrong," not just "bails."

## Rehearsal #2 fixes SHIPPED (2026-06-12) — "build safe + seed answers" (998 green)

User chose the full path. All five workstreams done + tested + verified offline against the
exact rehearsal questions (`JobSearch/verify_resolver.py`):

- **WS1 — no essay invention (the harm fix).** `is_open_ended()` (textarea, or why/describe/
  tell-us/what-experience labels). Open-ended prompts are **bank-only**: fill from a prepared
  answer or BAIL to assisted — the copilot is never invoked to free-write prose. "Not
  interested in a Solutions Engineer role" can no longer happen.
- **WS2 — seeded prepared answers** (`JobSearch/seed_answer_bank.py`, 10 generic answers from
  `build_application_answers.py` + 3 curated gap-fillers, embedded via nomic-embed-text).
  Company-specific "why us" essays deliberately NOT seeded (would cross-match other companies)
  → they bail to assisted for per-company writing.
- **WS3 — profile/contact fields** (`classify_profile_field` + `_resolve_profile`): LinkedIn/
  GitHub/Website/PreferredName/City/Country/CountryTimezone/Location pull from the fact bank;
  a MISSING value (e.g. no GitHub) BAILS blank, never "No I have not built a website."
- **WS5 — work-auth Yes/No mapping**: `_is_yes_no_question` + `_is_authorized` → an eligibility
  question ("Are you authorized/eligible to work…?") answers "Yes" (matches a Yes/No select)
  instead of the status string "US Citizen"; status questions still return the string.
- **WS4 — phone, mode-aware** (`fill_phone`): tries iti `setNumber()` (not exposed on
  Greenhouse's bundled build), else reads the dial-code mode — separate-dial-code → type
  national digits only (fixes the doubled +1); inline/globe → type +E.164 (verified live:
  flag→US, "+1 682-718-8130"). NOTE: the separate-dial-code branch is logic-correct but was
  not live-reproducible (couldn't identify which job used it) — confirm on the next rehearsal.

**Offline verification (all correct now):** Why-SE-role → fills prepared paragraph; K8s/
observability → honest nuanced answer; describe-sales → grounded answer; Website → bails;
LinkedIn/PreferredName/City/Country+TZ → filled; eligible-to-work → "Yes"; best-describes-you
→ bails; Why-Tailscale → bails (company-specific); how-did-you-hear → bails (select).

**Status:** answer quality is now SAFE (no invented essays) and substantially GOOD (seeded
essays fill, contact fields fill). Remaining bails are correct (attestation, consent, select-
option dropdowns, company-specific essays, missing GitHub). Next: a watched re-rehearsal with
`--keep-open` to confirm the fills land on-screen + the phone in separate-dial-code mode, THEN
the gated watched `--no-dry-run` one job at a time.

## Rehearsal #3 (2026-06-13) — THE discovery bug (why nothing filled) + verified fix

User watched run #3: essays/profile/work-auth all came back BLANK despite rehearsal-#2's
resolver rework. A live ground-truth diagnostic (`JobSearch/diagnose_apply.py` — runs the REAL
driver discovery + REAL resolver against a live form, dumps per-field source/value/landed, no
submit) found the cause:

**The current `job-boards.greenhouse.io` React layout renders each question as a control with
`id="question_<id>"` (textarea / text input / `input[role=combobox].select__input` react-select),
wrapped by sibling `question_<id>-label` and `-description` elements. `discover_custom_questions`
selected `[id^='question_']` — which matched the `-label`/`-description` WRAPPERS, and the
label-dedup pass then kept the wrapper and dropped the real input.** So the resolver produced
perfect answers but `fill_resolutions` typed them into a `<label>`/`<div>` → nothing landed. This
was the entire "nothing fills" failure, not the resolver.

**Fix (`greenhouse_apply.discover_custom_questions`):** tag-qualify the selector
(`input|textarea|select[id^='question_']`) so wrappers are excluded; take the label from
`aria-label` → `#<id>-label` text → `label[for]` → closest label; detect `required` from a
trailing `*` (the control's own `.required` lives on a hidden carrier); keep react-select as
`kind='input'` so the fill path types-and-commits via `settle_open_dropdown` (native
`select_option` doesn't work on react-select).

**Verified LIVE on Grafana (diagnostic, fill + read-back of `.select__single-value`):** 8 questions
discovered with correct ids/kinds; Why-SE + K8s essays committed from the bank; country/timezone +
LinkedIn filled; **eligible→`[committed] Yes`** and **sponsorship→`[committed] No`** (react-select
commits work); attestation + how-did-you-hear correctly BAIL. 6/8 auto-fill, 2 correct bails.

**Known remaining refinements (not blockers for assisted, DO matter for full-auto):**
- **Enumerated react-select option matching.** A copilot/binary answer that's prose ("No, I
  haven't used Tailscale") typed into a Yes/No or ranged react-select won't match an option →
  `settle` dismisses it → blank, but the resolution had `needs_review=False` so it does NOT
  trigger the assisted downgrade. On a real AUTO submit that's a blank required field → FAILED →
  REVIEW (safe, not a wrong answer, but a failed auto). Fix later: for combobox questions, return
  option-canonical values (Yes/No) and/or have `settle` fall back to a yes/no extraction.
- **Cover-letter upload** still not implemented (driver uploads résumé only).
- **Phone is cosmetic, not broken:** Grafana is intl-tel-input INLINE mode, so `+1 682-718-8130`
  with the US flag is the correct, submittable display (`getNumber()` → `+16827188130`). Separate-
  dial-code forms get national-only via `fill_phone`. No fix needed unless a form rejects it.
- **No GitHub link in the fact bank** → the "Website" field correctly bails blank (add a GitHub
  URL to the bank to auto-fill it).

## Rehearsal #3b (2026-06-13) — discovery fix CONFIRMED through the real worker

After the discovery fix, a user re-run still looked empty — but that run executed in the
window before the fix saved / viewed a stale keep-open tab. Settled it for good by adding
**per-field `resolution` events** to the worker (`ApplyWorker._log_resolutions`: local-only —
NOT mirrored, `sink._maybe_mirror` forwards only error/resolver_inferred — metadata only:
`{label, kind, required, source, fills, filled_on_page}`, never the answer value). The lack
of this was *why* "nothing filled" was un-diagnosable from `events.db`.

A real `av3 apply --once --limit 5 --dry-run` (run 192bcf77) then proved it end-to-end —
every `bank`/`profile`/`fact_bank` field `fills=True onpage=True` across all 5 forms:
Grafana (Why-SE, K8s, country/TZ, eligible→Yes, sponsorship→No, LinkedIn), Hightouch
(LinkedIn, describe-sales seeded, work-auth, sponsorship), Cresta (LinkedIn, work-auth,
sponsorship), Tailscale (LinkedIn, relocate-to-US seeded, sponsorship, +inferred), PlanetScale
(LinkedIn). Only correct bails remain (no-GitHub Website, company-specific Why-Tailscale,
attestation, how-did-you-hear). **The chain works through the actual worker.**

**Consent bail added (`SensitiveClass.CONSENT`):** the Tailscale "I have read and understand
the Candidate Privacy Policy and AI Guidelines" acknowledgment was being auto-answered. A bot
must not knowingly consent on the user's behalf (esp. an AI-tool-use-in-hiring policy) → now
always bails to assisted. Patterns: "I have read/agree/acknowledge/consent/certify/understand",
"privacy policy/notice", "terms…", "AI guidelines".

**Remaining refinement — enumerated react-select commit.** Yes/No react-selects commit fine
(verified `[committed] Yes/No` via read-back). But an LLM-inferred PROSE answer typed into a
ranged/enumerated react-select (years-of-experience, SaaS yes/no) filters the menu to empty →
doesn't commit → blank, while `fills=True` (typed) means no assisted downgrade → blank required
on a real AUTO submit → FAILED→REVIEW (SAFE, not a wrong answer). Fix later: a dedicated
`fill_combobox` (open → click the option matching the value, fall back to first-token filter)
and/or return option-canonical short values for combobox questions.

## Rehearsal #4 (2026-06-13, user screenshots) — full field coverage + react-select committer

User's per-field screenshots showed many fields still blank. Diagnostic (`diagnose_apply.py`,
now dumps ALL controls + discovered? + resolve + read-back) on the live Grafana form revealed
the cause + drove the fixes:

- **Discovery was still too narrow.** It only matched `question_*`; Greenhouse's semantic-id
  fields (`preferred_name`, `country`, `candidate-location`, `gender`, `hispanic_ethnicity`,
  `veteran_status`, `disability_status`) were never discovered → blank. Fix: discovery now
  walks ALL `input/textarea/select`, EXCLUDING standard driver fields (first/last/email/phone/
  resume) + noise (requiredInput carrier, iti search, recaptcha, file/hidden/search), keeping
  anything with a label. Discovered fields on Grafana went 8 → 15.
- **react-select committer (`fill_combobox`, kind `combobox`).** react-select dropdowns can't be
  filled by typing prose (filters menu to empty) or native `select_option`. New committer: open
  the menu (scroll-into-view + wait for `.select__menu`), click the matching option by PRIORITY
  (exact → decline-synonym-only for prefer-not values → whole-word containment; word-boundary so
  'No' can't match inside 'not'), else type a short query (city autocomplete) then match; pre-clear
  Escape + post-commit settle + 3 attempts for the flaky sequential-menu intercept.
- **Profile/EEO now fill:** Preferred Name→Joseph, Location(City)→"Dallas, Texas, United States"
  (autocomplete), eligible→Yes, sponsorship→No, how-did-you-hear→LinkedIn (seeded), Gender/
  Hispanic→"Decline To Self Identify", Disability→"I do not want to answer". EEO `_pick_eeo`
  yields "Prefer not to answer"; the committer maps it to the form's decline option via synonyms.

**Verified live (Grafana): 17/19 filled.** Non-fills: "Which best describes you?" (correct
attestation bail) and `veteran_status` (intermittent — a single non-required EEO react-select
flakes ~1/run despite retries; benign since EEO is optional).

**Still TODO (named):** (1) **cover-letter upload** — Greenhouse hides the file input behind an
"Attach" button + per-company letters are separate .docx → needs a reveal step + job→letter map;
(2) **separate-dial-code phone** — the user's Image #1 (separate Country+Phone boxes) is a
different form than Grafana's inline iti; `fill_phone` has the national-only branch but it
wasn't reproduced/confirmed on that form; (3) **Website/Portfolio** bails — no GitHub/website
in the fact bank (needs the URL); (4) `veteran_status` intermittent commit.

## Current state — what is built and validated

The flow (`auto_applier/`, CLI `av3`, runs against `JobSearch\av3data`):

- **discover → filter → score → optimize → apply**, all dry-run-validated.
- **Apply driver downgrade logic** (`sources/browser/greenhouse_apply.py`, mirrored in lever/ashby) —
  the safety rails that already exist:
  - Any **required custom question** the resolver can't confidently answer → `ASSISTED_PENDING`
    (`any_required_unresolved`). This is what makes honesty-sensitive screeners safe: the bot bails
    rather than overclaim.
  - **Visible CAPTCHA** → `ASSISTED_PENDING` (never solved/retried).
  - **Submit button missing** → `FAILED` (fail fast to REVIEW).
  - Auto-submit only on a clean form; **`APPLIED` only on positive confirmation**
    (`detect_confirmation`: GH `/confirmation`, Lever `/thanks`, Ashby success text). Anything else →
    UNCONFIRMED/FAILED → REVIEW.
- **Answer resolver** (`resume/answer_resolver.py`) is **copilot-audited** (2026-06-11): tier-3
  routes through the §8f evidence audit, so an unsupported "yes" fails closed to REVIEW instead of
  overclaiming. Sensitive fields (work-auth/sponsorship/EEO/salary) are deterministic policy.
- **`detect.py`** covers CAPTCHA, submit-confirmation, and login/auth-wall. It does **NOT** cover
  human-attestation gates (blocker A).
- **Resume wiring**: the apply worker uploads the per-job optimize-generated PDF if it exists, else
  the global `artifacts/resume.pdf` fallback.

## Hard constraints (reliability invariants — never compromise for throughput)

1. Manual login only; headed browser; **never retry through CAPTCHA** → assisted.
2. Mid-form break → **fail fast to REVIEW**, no retry.
3. **`APPLIED` only on a positive submit confirmation.**
4. Fabrication guard: a generated résumé/cover may use only fact-bank facts.
5. **NEW — human-attestation gates → always assisted.** The bot must never attest to being human on
   an automated submit. The human attests when they review and submit (assisted), which is truthful.
6. **NEW — honesty-sensitive screeners bail to assisted, not overclaim.** Already enforced by the
   copilot-audited resolver + `any_required_unresolved`. Do not weaken this to raise the auto rate.

## Blocker A — human-attestation gate detector (required dev task)

**Why:** an "I am a human being / I am an AI or automated program" dropdown (seen on a real Solutions
form, 2026-06-12) is a knockout for bots. The honest answer for an *automated* submission is "AI /
automated program," which fails the application; the bot picking "human being" is a false
attestation. The only correct behavior is to **not auto-answer it and hand the form to the human**,
who then truthfully attests as a human reviewing and submitting.

**Design (cleanest seam — reuse the existing sensitive→REVIEW→assisted path):**
- Add `SensitiveClass.HUMAN_ATTESTATION` to `resume/answer_resolver.py`.
- In `classify_sensitive`, detect it from the label/options: any question whose options include both
  a human affirmation and an AI/bot/automated option, or label patterns like
  `are you (a )?human`, `human being`, `automated (program|agent)`, `\bare you a (ro)?bot\b`,
  `I am not a robot` (when it is a real radio/select, not the reCAPTCHA widget).
- In `_resolve_sensitive`, `HUMAN_ATTESTATION` **always returns `_review`** (never a value), with a
  clear note. It then flows through `any_required_unresolved` → `ASSISTED_PENDING`. The bot stops; the
  human finishes and truthfully ticks "human being."
- The driver's question discovery already captures select options? **Check:** the current
  `discover_custom_questions` captures label + kind + required, but not the option *values*. The
  detector may need the option text. Either (a) extend discovery to capture select options, or (b)
  match on the label alone for the common phrasings. Start with (b) (label-only) — it covers
  "Which of the following best describes you?" only if the label is descriptive; the safer bet is to
  ALSO capture options for select/radio and flag on the AI/human option pair. Recommend (a)+(b).
- Tests: label-only matches; option-pair matches; a normal select (e.g. country) does NOT match;
  resolver returns REVIEW → driver downgrades to assisted.

**Until A is built, do NOT run `--no-dry-run`** on any form that might carry this gate. A dry-run
will reveal which of the 5 have it (the resolver's per-question events show the label).

## Blocker B — use the hand-crafted résumé, not the generated one

- The optimize stage's qwen3:8b résumé is weaker than the user's polished
  `Joseph_Lira_Resume_Solutions_Engineer.docx`, and cover-letter generation has shown fabrication
  (the Kubernetes/Terraform incident). For a *real* submission, upload the hand-crafted résumé.
- **How:** render `Joseph_Lira_Resume_Solutions_Engineer.docx` → PDF and place it at
  `JobSearch\av3data\artifacts\resume.pdf` (the global fallback the worker uploads when no per-job
  PDF exists). Then **do NOT run optimize** on the 5 (so no per-job PDF is generated and the worker
  uses the good fallback). Render options: open the .docx in Word → Save As PDF; or `docx2pdf`; or
  the product's `render_resume_pdf` fed the Solutions profile content.
- Trade-off: skipping optimize means the 5 jobs need another route to `QUEUED_APPLY` (see plan
  step 3). That is fine — the fabrication guard exists to vet *generated* résumés; a hand-verified
  résumé doesn't need it.
- Cover letter: the Greenhouse/Ashby drivers currently upload résumé only (cover-letter field-fill is
  a future driver concern). If a target form has a *required* cover-letter field, the resolver won't
  fill it → assisted. Acceptable.

## The plan (watched, dry-run first)

**Pre-flight (once):**
1. Confirm the persistent browser profile is logged in where needed (most GH/Ashby apply pages are
   public; auth-wall detection will catch any that aren't and bail to REVIEW).
2. Render the hand-crafted Solutions résumé to `artifacts\resume.pdf` (blocker B).
3. Build blocker A (human-attestation detector) + tests; full suite green.

**Select the 5 candidates:**
4. They must be **new** Solutions roles NOT already applied to (the 16 from the last batch are
   terminal APPLIED; the 4 dead are SKIPPED). Build a fresh shortlist excluding those, and prefer
   roles with the **fewest required screeners** (those are the only realistic full-auto candidates):
   ```
   av3 shortlist --family solutions --location remote --limit 25 --name solutions2
   ```
   Pick 5 from `solutions2` that aren't in the prior batch. (There is no per-job screener count until
   the form loads, so the dry-run in step 6 is how we actually learn which are clean.)

**Queue them (without optimize, using the good résumé):**
5. Move the 5 chosen `DECIDED` jobs to `QUEUED_APPLY`. There is **no CLI for this yet** (optimize is
   the normal DECIDED→QUEUED_APPLY path). Options:
   - (a) Add a tiny `av3 queue <job_ids>` command (DECIDED→QUEUED_APPLY guard), OR
   - (b) one-off DB transition for the 5 (set state QUEUED_APPLY) for this run.
   Recommend (a) — it is the missing piece for any manual-resume auto-apply and is a clean ~30-line
   addition mirroring `av3 pass`.

**Dry-run dress rehearsal (NO submits):**
6. ```
   av3 apply --once --limit 5 --dry-run --mode auto
   ```
   Read `av3 errors` + the resolver events: for each of the 5, see how many required questions
   resolved vs bailed, whether a CAPTCHA/auth-wall/human-attestation gate is present. This tells us
   which (if any) will full-auto vs assist-pend. **Expect most to assist-pend** on a honesty-sensitive
   required screener — that is the honest result.

**Go live (watched, one at a time):**
7. For the candidate(s) that came back CLEAN in the dry-run (no unresolved required screener, no
   visible CAPTCHA, no attestation gate):
   ```
   av3 apply --once --limit 1 --no-dry-run --mode auto
   ```
   Watch the headed browser. Confirm `APPLIED` only fired on a positive confirmation. Repeat per
   clean job until 5 real submits land (or until we run out of clean candidates).
8. For the assist-pend ones: the dashboard's assisted-submit panel opens the pre-filled form for the
   human to finish + submit (that is the `ASSISTED_PENDING` → human-confirm → APPLIED path).

## Expected outcome (honest)

- **Likely:** of 5 Solutions candidates, **1–2 full-auto** (clean forms: basic fields + résumé only)
  and **3–4 assisted-pending** (bot pre-fills, the honesty-sensitive required screener bails, human
  finishes). The bot still does 70–90% of the typing on the assisted ones.
- If the user wants a higher full-auto count, the lever is **role selection, not weakening the
  guards**: roles with minimal required screeners (often smaller companies / simpler ATS configs)
  full-auto cleanly. Solutions roles at big platforms (Databricks, Vanta, Tailscale) are screener-
  heavy by nature.
- **Never** raise the auto rate by letting the resolver answer honesty-sensitive screeners or the
  human-attestation gate. That trades a real interview-burn / ToS risk for a vanity metric.

## Risks & gotchas

- **The human-attestation gate is the sharpest one.** Until blocker A ships, a real run risks the bot
  ticking "human being." Do not `--no-dry-run` before A.
- **First-submit confirmation:** verify `detect_confirmation` fires correctly on the *actual* target
  ATS (Ashby in-place panel vs Greenhouse `/confirmation`). The dry-run won't exercise the post-submit
  page; the first watched `--no-dry-run` is the real test. Watch it.
- **Duplicate-apply safety:** `APPLIED` is the dedup key; an UNCONFIRMED submit stays retry-safe and
  is NOT marked APPLIED. Good. But a *confirmed* real submit is irreversible — hence watched.
- **Pacing/detection:** the strategy profile governs delays; for a 5-job manual run it is moot, but
  keep the headed real-Chrome profile (never headless for real submits).
- **Résumé format:** confirm the target ATS accepts the PDF; some prefer .docx. `set_input_files`
  takes either; the pre-flight checks for `resume.pdf` specifically.

## Open decisions for the user

1. **Full-auto vs assisted tolerance:** is "bot pre-fills, you click submit" (assisted) an acceptable
   outcome for most of the 5, or do you specifically want fully-hands-off submits (which means
   accepting a smaller count and/or simpler-ATS roles)?
2. **Résumé:** confirm we upload the hand-crafted Solutions résumé (recommended), not the generated
   one.
3. **Scope of build:** ship blocker A (human-attestation detector) + the `av3 queue` command now, then
   do the watched dry-run? That is the next concrete step and the gate to any real submit.

## Next concrete step

Build blocker A + `av3 queue`, render the résumé PDF, then run the **dry-run dress rehearsal on 5
fresh Solutions candidates** and read the per-job results together. No real submit until the user
watches the first one.

## ⭐ FIRST REAL SUBMIT (2026-06-16) — Greenhouse emails a SECURITY CODE; auto-submit is GATED

The first-ever real `av3 apply --no-dry-run --mode auto` ran on ONE isolated mid-scoring job
(other 7 queued jobs parked QUEUED_APPLY→REVIEW via a reversible round-trip, then restored):
**PlanetScale — Solutions Engineer** (8.75, the cleanest form of the batch). Pre-flight was watched
+ verified in a keep-open dry-run (résumé `Joseph Lira Resume.pdf` + an auto-gen cover + all 4
custom questions all landed on the live form, conf 1.00). **Result: the application did NOT go
through.**

**What happened.** The bot filled everything and clicked submit (`outcome.submitted=True`,
`submitted_at` recorded, status **UNCONFIRMED**, run_id `ed8307703886`, ~17s). Greenhouse did NOT
accept it — it **emailed a one-time security code** (`Greenhouse <no-reply@us.greenhouse-mail.io>`,
subject *"Security code for your application to PlanetScale"*: "Copy and paste this code into the
security code field on your application: `cvBXeW0v`. After you enter the code, resubmit your
application.") and rendered a **security-code field** on the post-submit page. So submission is
gated behind an **emailed email-ownership / anti-bot verification step**.

**System behaved CORRECTLY (no false positive):** `detect_confirmation` saw no `/confirmation` URL,
no success text, no validation error, no recognized CAPTCHA → **UNCONFIRMED** → APPLYING→FAILED→
REVIEW. **APPLIED was NOT set** (so it's retry-safe, no duplicate risk, doesn't count as applied),
and the bot did NOT try to bypass the gate (it has no email access — correct per the never-bypass-
a-verification-gate invariant). PlanetScale sits in REVIEW with an UNCONFIRMED Application row.

**STRATEGIC IMPLICATION (big).** The new `job-boards.greenhouse.io` React layout gates the SUBMIT
itself behind an emailed security code. Whether universal or anti-bot-triggered, it REQUIRES a
human-in-the-loop email step, so **full-auto submit on Greenhouse is generally NOT achievable** —
the realistic path is **ASSISTED**: the bot fills, the human reads the emailed code, enters it, and
resubmits. This is broader than the "Solutions roles assist-pend on screeners" finding — the gate
is the submit step, so it applies even to a screener-clean form like PlanetScale.

**Detector gaps to fix (next build):**
1. **Classify the email-security-code page as a verification gate → ASSISTED_PENDING** (like CAPTCHA)
   with a note "Greenhouse emailed a security code; enter it and resubmit (assisted)". Candidate
   patterns: "security code", "enter the code", "resubmit your application". Today it falls through
   to generic UNCONFIRMED → the human gets no hint why.
2. **Log the post-submit URL + a content snippet** (local event, metadata only, never PII) on every
   real submit. The post-submit page is NOT recorded today, which made this slow to diagnose — same
   lesson as the "nothing filled" → resolution-event logging gap.

**To complete PlanetScale:** an assisted run (`av3 apply --mode assisted --keep-open` on the isolated
job) — bot fills, Joseph enters the emailed code + submits. The code `cvBXeW0v` is likely
attempt-bound (a fresh attempt emails a new one). Until then PlanetScale stays in REVIEW.
