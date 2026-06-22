# Handoff — ATS field-coverage audit via a Playwright session

**Status:** PLANNED (handoff written 2026-06-21). This scopes a dedicated **Playwright MCP session**
that enumerates every field + clickable on real ATS apply forms, determines which the bot misses, and
produces a prioritized gap list. Written because we've been finding missed fields one screenshot at a
time ("it's missing quite a few fields") — this turns that into a systematic, repeatable audit.

## Goal

For representative live Greenhouse / Lever / Ashby application forms, produce:
1. A **field inventory** per form (every input/select/textarea/combobox/radio/checkbox/file + label +
   required flag + options) — the ground truth from a Playwright accessibility snapshot.
2. A **gap table**: for each field, does the bot fill it? If not, **which layer failed** and the fix.

## The key framing — a miss can fail at TWO layers

A field the user sees blank on a submitted form failed at one of two independent layers. The audit MUST
attribute each miss to the right one, because the fix differs:

| Layer | What it is | How to detect | Fix lives in |
|---|---|---|---|
| **1. Discovery** | The driver's form-scraper never found the field (selector miss) | It's in the Playwright snapshot but NOT in the driver's discovered `CustomQuestion` list | `sources/browser/{greenhouse,lever,ashby}_apply.py` field enumeration / `apply_base.py` |
| **2. Resolution** | Discovered, but the resolver bailed (no classifier match / no data / honesty bail) | It IS discovered but resolves to `needs_review` / blank | `resume/answer_resolver.py` |

Resolution misses sub-divide:
- **2a classifier gap** — the label matches no pattern → falls to the LLM/bail. *Fix: add a label pattern.*
- **2b fact-bank gap** — classified but the bank has no value. *Fix: add a fact-bank field + onboarding step.*
- **2c answer-bank gap** — needs a seeded answer. *Fix: seed it (but NOT how-heard — derived, see below).*
- **2d honesty bail (CORRECT)** — EEO/consent/human-attestation → must bail to the human. *No fix; verify.*
- **2e open-ended (CORRECT-ish)** — essay → draft (if `draft_freeform`) or bail. *No fix unless drafting wanted.*

## Tooling — Playwright MCP

The session has `mcp__playwright-mcp__*`. The **form VIEW is public** on Greenhouse/Lever/Ashby (no login
to render the fields), so:
- `browser_navigate` → the apply URL.
- `browser_snapshot` → the accessibility tree = every field + role + name(label) + required + options.
  THIS IS THE GROUND TRUTH. (Prefer the a11y snapshot over a screenshot — it's the structured field list.)
- `browser_evaluate` if needed to dump `document.querySelectorAll('input,select,textarea,[role=combobox]')`
  with their labels for fields the a11y tree under-describes (react-select gates hide options until open —
  see the human-attestation note in `answer_resolver.py`).
- **NEVER submit.** Snapshot/enumerate only. Do not click submit; do not fill (this is read-only audit).

## Method (step by step for the executing session)

1. **Gather real apply URLs** — use the USER'S actual targets so the audit reflects what they hit:
   - `av3 status` / the QUEUED_APPLY + REVIEW jobs in `app.db` carry `url`. Pull 3-5 per ATS.
   - Or pick from the seeded boards (`auto_applier/data/ats_companies.csv` + the user's targeting). The
     known live one from 2026-06-21: Greenhouse `imagineworldwide/jobs/4186592009`.
2. **Snapshot each form** → build the field inventory (label, kind, required, options).
3. **Compare to driver DISCOVERY** (layer 1): run the driver's field enumeration against the same page
   (or read the `resolution` events in `events.db` from a real `--dry-run` apply of that job — the
   `@stage`/`_log_resolutions` path records every discovered question + what it resolved to, WITHOUT the
   answer value). Any snapshot field absent from the discovered set = a **discovery gap**.
4. **Compare to RESOLUTION** (layer 2): for each discovered field, run its label through the classifiers
   and predict fill vs bail (see "Classifier entry points" below), or read the `resolution` event's
   `source`/`fills`/`filled_on_page`. Categorize each miss 2a-2e.
5. **Write the gap table** → `research/ats-field-coverage-audit.md` (this file — append a "Findings"
   section) + a prioritized fix list.

## Classifier entry points (what to test each label against)

In `auto_applier/resume/answer_resolver.py`, `AnswerResolver.resolve()` runs this order — a field is
filled by the FIRST that hits, else it bails:
1. `classify_sensitive(label, options)` → EEO / WORK_AUTHORIZATION / SPONSORSHIP / SALARY /
   HUMAN_ATTESTATION / CONSENT → routed to `_resolve_sensitive` (EEO/consent/attestation = honesty bail).
2. `classify_profile_field(label)` → LINKEDIN/GITHUB/WEBSITE/CITY/COUNTRY/LOCATION/NATIONALITY/
   NOTICE_PERIOD/YEARS_EXPERIENCE/PREFERRED_*  → `_resolve_profile` (reads the fact bank).
3. `_is_how_heard(label)` → `_resolve_how_heard` → **DERIVED from `job.source`** (ATS ⇒ "Company Website",
   boards ⇒ their name). NOT seeded, NOT human-fill. See [[feedback_how_heard_derive_from_source]]. Do NOT
   flag a blank how-heard as a seed/bank gap.
4. `_is_relocation_question(label)` → `_resolve_relocation` (fact-bank `relocation` prefs).
5. Answer-bank exact/semantic match (`answer_repo`).
6. `is_open_ended(label, kind)` → DRAFT (if `draft_freeform`) or bail.
7. LLM tier (confidence-gated) → else bail to REVIEW.

The fact bank fields available to layer 2b: `contact.*`, `work_authorization`, `requires_sponsorship`,
`relocation`, `primary_nationality`, `notice_period`, `eeo` (gender etc.), computed `years_experience`.

## Grounding — the live Greenhouse form (screenshot 2026-06-21, imagineworldwide)

After this session's fixes, every field on that form should now fill. Baseline to confirm/expand:

| Field | Kind | Bot result | Layer |
|---|---|---|---|
| First/Last Name, Email, Phone, Country(phone) | input | FILLED | profile |
| Resume/CV, Cover Letter | file | FILLED (clean per-job-folder name now) | artifacts |
| LinkedIn Profile | input | FILLED | profile |
| Country of Residence | textarea | FILLED "United States" | profile (US-state fix) |
| Number of relevant work experience | select | FILLED "6 to 9 years" | computed years |
| **How did you hear about this role?** | input | FILLED "Company Website" | discovery-derived ✅ |
| Gross annual salary expectation (USD) | input | FILLED "$82,000" | targeting.salary_floor |
| **Primary Nationality** | input | was BLANK → fixed (fact-bank hot-reload) | 2b |
| **Gender** | select | was BLANK → fixed (set in More details; EEO fills when provided) | 2b/2d |
| **Notice period** | input | was BLANK → fixed (hot-reload) | 2b |

So this form is the regression baseline. The audit's value is the NEXT forms — Lever/Ashby + other
Greenhouse layouts — where new field labels (e.g. "Desired start date", "Are you 18+?", "Pronouns",
"Work model preference", custom EEO variants, demographic dropdowns, multi-select skills) will surface
classifier gaps (2a) the patterns don't yet catch.

## Likely classifier-gap candidates to probe specifically (hypotheses)

- "Desired/earliest start date", "available start date" → no profile field; likely bails (2a/2b).
- "Are you 18 years or older?" / "Are you legally eligible..." variants → may miss work-auth patterns.
- "Preferred pronouns" → EEO (honesty bail, verify 2d) vs a free text.
- "Work model" / "remote / hybrid / onsite preference" → relocation-adjacent, may bail (2a).
- "Years of experience with <specific skill>" → not the computed total; bails (2a).
- "Where are you located?" / "Current location" → CITY/LOCATION; verify it hits.
- Multi-select skills / checkboxes → kind handling in the driver (layer 1 + fill mechanics).

## Deliverable

Append a **"## Findings (run <date>)"** section here with: the per-form field inventory, the gap table
(field → layer → fix), and a prioritized list of (a) new classifier patterns, (b) new fact-bank fields,
(c) driver discovery fixes. Then a follow-up build session implements the top gaps + adds the form to the
`smoke` suite (live selector-drift guard).

## Cautions

- Read-only: snapshot/enumerate, NEVER submit or fill on the live site.
- Login-gated forms (LinkedIn, some custom portals) — note + skip; the bot can't auto-login anyway.
- react-select / combobox options aren't in the DOM until opened — use `browser_evaluate` or open the
  menu to read them (mirrors the live human-attestation gate problem in `answer_resolver.py`).
- Honesty bails (EEO/consent/attestation) are CORRECT misses — don't "fix" them into auto-fills.
- The fact bank must be COMPLETE for an honest resolution test — run against a fully-onboarded dir
  (the user's `C:\Users\jar85\JobSearch`), not a fresh test dir, or layer-2b will over-report.
