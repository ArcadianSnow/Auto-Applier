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

---

## Findings (run 2026-06-22)

**Verdict in one line:** the RESOLVER (layer 2) is in good shape — when the true label reaches it, it
fills correctly. The losses are almost all in the **DRIVERS' DISCOVERY (layer 1)**: Lever and Ashby
silently drop high-value fields (incl. required ones) and mangle question labels, so the resolver never
gets a chance. Greenhouse discovery is healthy; its only soft spot is react-select FILL mechanics.

### Method / data sources (all read-only)

Three complementary sources, cross-referenced:
1. **`events.db` `resolution` events** — 210 events across the 8 real Greenhouse QUEUED_APPLY/REVIEW targets
   (the only ATS ever dry-run-applied, because no Lever/Ashby job was ever queued). Each row carries
   `{label, kind, required, source, fills, filled_on_page}` — the driver's *actual* discovered set + what
   each resolved to + **whether the value landed on the page**, with no answer value. This is layer-1+2
   ground truth for Greenhouse.
2. **Playwright MCP ground-truth probe** on 5 live forms (1 GH baseline + 2 Lever + 2 Ashby). One reusable
   `browser_evaluate` probe enumerates every native control AND every custom role-based widget with its
   label/required/options, and runs each driver's *exact* discovery selector beside it — so "what's on the
   page" vs "what the driver finds" is an apples-to-apples diff. Forms: imagineworldwide (GH baseline),
   mistral + qonto (Lever), openai + plaid (Ashby).
3. **Classifier-prediction harness** (throwaway script, since deleted) — loaded the **production** fact bank
   (`JobSearch/av3data`, NOT `JobSearch` itself — that's the real `data_dir`; `FactBank.load(data_dir /
   "profile" / "master.json")`) and ran every *true* question label through `classify_sensitive` /
   `classify_profile_field` / `_is_how_heard` / `_is_relocation_question` / `is_open_ended` (mirroring
   `AnswerResolver.resolve`'s order, minus the async embedding/LLM tiers) to predict the resolution tier.
   Proves "if discovery captured the real label, would the resolver fill it?" Trivially reconstructable from
   the "Classifier entry points" section above.

Production fact bank state (for honest 2b attribution): name ✓, location `Dallas, Texas, United States`,
linkedin ✓, github ✓, work_authorization `US Citizen`, requires_sponsorship `False`,
**primary_nationality `''`**, **notice_period `''`**, relocation `{willing:[Netherlands], unwilling:[Canada]}`,
eeo `{gender,hispanic,veteran,disability}`, years_experience(computed) `6`.

### The layer model needs a THIRD bucket — 1b "fill mechanics"

The doc's 2-layer model (1 Discovery / 2 Resolution) misses a failure mode the events.db made unmissable:
a field can be **discovered AND resolved to a value, yet the value never lands on the page**
(`fills=true, filled_on_page=false`). Call it **Layer 1b — fill mechanics** (the driver found it and has an
answer, but can't drive that widget type). It lives in `apply_base.fill_resolutions` + the per-ATS fill path,
and it is the dominant Lever/Ashby loss for yes/no questions (checkbox/radio/button groups the filler can't
operate) and an intermittent Greenhouse loss (react-select menu intercept).

### Per-ATS discovery health

| ATS | Discovery selector | Health | Headline gaps |
|---|---|---|---|
| **Greenhouse** | walks ALL `input/textarea/select` w/ a label; flags react-select as `combobox`; native-`<select>` options scraped | **Healthy** — baseline form discovered all 9 custom Qs, labels clean, chrome correctly skipped | only **1b**: custom react-select comboboxes intermittently `filled_on_page=false` |
| **Lever** | ONLY `[name^='cards['] , [name^='eeo['] , pronouns` | **Leaky** — finds ~6 of ~20 fields; **labels empty/wrong**; **never scrapes options** | required `urls[LinkedIn]` + all `urls[*]` missed; `location` unfilled; EEO is `surveysResponses[*]` not `eeo[*]` → missed; card labels grabbed from inner `<div>` → "Yes"/"No"/"" |
| **Ashby** | `input/textarea/select` minus `_systemfield_*` | **Leaky on widgets** — native inputs OK, but **id/name-less comboboxes & date pickers skipped**, yes/no **button-groups** discovered as empty-label checkboxes | `Location`/`Education` comboboxes (no id/name) dropped; work-auth/sponsorship/relocation Yes/No buttons → empty label → bail; UUID `Phone` bails (no phone classifier) |

### Field inventory + gap table (consolidated across the 5 forms)

Layer key: **1**=discovery (field never found) · **1L**=discovery label-quality (found but label empty/wrong)
· **1b**=fill mechanics (resolved but didn't land) · **2a**=classifier gap · **2b**=fact-bank gap ·
**2d**=honesty bail (CORRECT) · **2e**=open-ended bail (CORRECT unless drafting) · **OK**=fills today.

**Greenhouse (imagineworldwide baseline — regression target, all healthy):**

| Field | Kind | Result | Layer |
|---|---|---|---|
| First/Last/Email/Phone, Résumé, Cover Letter | input/file | FILLED (driver standard) | OK |
| Country*, Country of Residence, LinkedIn Profile | combobox/textarea/input | FILLED | OK profile |
| Primary Nationality | input | bank empty → falls through → likely BAIL | **2b** |
| Number of relevant work experience* | combobox | FILLED "6" → matches "6 to 9 years" | OK (computed) |
| Gender* (required EEO) | combobox | FILLED self-id/prefer-not | OK (EEO) |
| How did you hear about this role? | input | FILLED "Company Website" | OK (derived) ✅ |
| Gross annual salary expectation (USD) | input | FILLED from config | OK |
| Notice period | input | bank empty → falls through → likely BAIL | **2b** |

**Greenhouse (real targets, from events.db) — the 1b fill-mechanics signal:** Tailscale custom react-select
comboboxes ("How many years…", "have you used Tailscale before?") resolved (`inferred`/`bank`, `fills=true`)
but `filled_on_page=false` on the 2026-06-15 run, while EEO/profile comboboxes on the same form landed. →
intermittent react-select fill failure (the documented "menu intercept / opened-against-closing-menu"
issue). **Re-verify with a fresh dry-run** — the fill path has been iterated since. CORRECT bails on those
forms: "Why are you interested in joining Tailscale?" (textarea → 2e) and "I have read… Candidate Privacy
Policy and AI Guidelines" (→ 2d consent). Postman discovered only board chrome ("Search/Department/Office")
— the apply form wasn't present (closed/redirected); driver should detect "no form" rather than scrape
listing filters. PlanetScale discovered only 4 fields — run truncated by its emailed-security-code gate.

**Lever (mistral + qonto):**

| Field | Kind | Driver result | Layer | Fix |
|---|---|---|---|---|
| `urls[LinkedIn]` **(REQUIRED)** | input | **NOT discovered** (driver scans only `cards[`/`eeo[`/`pronouns`) | **1** | discover the `urls[*]` family → resolver fills LinkedIn/GitHub/website |
| `urls[GitHub]`, `urls[Dribbble]`, `urls[Design Portfolio]`, `urls[Other]` | input | NOT discovered | **1** | same; resolver maps GitHub/portfolio, bails unknown (Twitter/Scholar absent in bank — correct) |
| `location` (Current location) | input | **NOT filled** (standard set is name/email/phone/org only) | **1** | add `location`/`selectedLocation` to Lever standard fill (bank has it) |
| Work-auth card ("Do you have the authorization to work…?") | checkbox Yes/No | discovered but **label="Yes"** (grabbed option, not question) → misclassified | **1L + 1b** | read label from `.application-question > .application-label`; add checkbox/radio option-click fill |
| Sponsorship card ("Do you need a visa sponsorship…?") | checkbox Yes/No | label-from-inner-div → bail | **1L + 1b** | same |
| Essay cards (languages / "complex LLM deployment" / "optimize for in life" / "what should we know" / dbt / achievement) | textarea | discovered, **label=""** (closest `<div>` had no label) | **1L** → then **2e** | fix label; then correctly bails to assisted (or DRAFT if `draft_freeform`) |
| EEO "What gender do you identify as?" | radio | **NOT discovered** — Lever uses `surveysResponses[*]`, driver scans `eeo[*]` | **1** (low harm) | add `surveysResponses[*]` to discovery (EEO then fills/prefer-not) |
| Pronouns | checkbox | discovered (`pronouns`) → EEO; **can't fill checkbox** | **1b** | checkbox option-click fill |
| `consent[marketing]` | checkbox | not discovered | — | leave (don't auto-consent; correct) |

Root cause of the Lever **1L** label gap: the driver's `el.closest('div,fieldset,li,section')` stops at the
inner field `<div class="application-field">` (no label inside) instead of the `li.application-question` that
holds `.application-label`. The question text is *reliably* available at `.application-question >
.application-label` — verified on both forms. Lever has **no native `<select>` and no react-comboboxes**; its
custom questions are text / textarea / checkbox / radio, so the 1b fix it needs is **checkbox/radio
option-clicking**, not combobox handling.

**Ashby (openai + plaid):**

| Field | Kind | Driver result | Layer | Fix |
|---|---|---|---|---|
| `Location` / "Where are you currently located?" | react combobox | **NOT discovered** (input has no id/name → `if(!id) return`) | **1 + 2a** | anchor discovery to `.ashby-application-form-field-entry`; classifier: "Location"/"currently located" |
| `Education History` | repeatable combobox | NOT discovered (no id/name) | **1** | low priority (complex; usually optional) |
| "When can you start a new role?" | date picker | **NOT discovered** (no id/name); **REQUIRED** on openai | **1 + 2a + 2b** | discover via container; add start-date/availability pattern + fact-bank field |
| Work-auth ("Are you authorized to work…?") | Yes/No button-group | discovered as **empty-label** hidden checkbox → bail | **1L + 1b** | title from `.ashby-application-form-question-title`; button-group click fill (resolver KNOWS the answer) |
| Sponsorship ("require sponsorship…/ immigration sponsorship?") | Yes/No buttons | empty-label → bail | **1L + 1b** | same (resolver fills from bank) |
| Prev-employment / "able to work onsite?" | Yes/No buttons | empty-label → bail | **1L + 1b (+2a)** | label fix + button fill; onsite is a 2a work-model gap |
| `Phone Number` (UUID-named) | input | discovered, but **resolver has no PHONE classifier** → bail | **2a** | add PHONE profile field (`contact.phone`); Ashby never fills phone as standard |
| LinkedIn / GitHub / Portfolio / Other URL | input | discovered + labeled → FILLED | OK | (Ashby native URL inputs work, unlike Lever's `urls[*]`) |
| Legal Name / Email / Résumé | input/file | `_systemfield_*` → driver standard | OK | |
| Additional Information | textarea | discovered → 2e bail (correct) | 2e | |
| "I confirm I have read the above." (certification) | checkbox | → CONSENT bail | **2d** ✅ | do not auto-fill |

Root cause of the Ashby gaps: discovery is keyed on the input's `id`/`name`, but Ashby's React widgets
(comboboxes, date pickers) render an `<input>` with **neither**, and yes/no questions render a hidden
empty-label checkbox + `<button>Yes/No</button>`. Every question *does* sit in a
`.ashby-application-form-field-entry` with a reliable `.ashby-application-form-question-title` — verified on
both forms — so anchoring discovery to that container fixes both the dropped fields and the empty labels at
once. Ashby native inputs (incl. URL fields) discover + label fine today.

### Prioritized fix list

**(c) Driver DISCOVERY fixes — the biggest wins, do these first:**

1. **Lever — discover the `urls[*]` family** (`urls[LinkedIn]` is frequently REQUIRED and the bank HAS it; a
   blank required field forces every Lever auto-apply to assisted). Map by label to the existing
   LINKEDIN/GITHUB/WEBSITE profile fields. *(layer 1, HIGH)*
2. **Lever + Ashby — label from the question CONTAINER, not the closest `<div>`.** Lever: prefer
   `el.closest('.application-question')` → `.application-label`. Ashby: discover by
   `.ashby-application-form-field-entry` → `.ashby-application-form-question-title`. This single change fixes
   the empty/"Yes"/"No" labels that currently sink work-auth, sponsorship, and every essay card. *(layer 1L,
   HIGH — unlocks fields the resolver already answers)*
3. **Ashby — anchor discovery to the field container so id/name-less widgets are found** (Location combobox,
   start-date picker). *(layer 1, HIGH)*
4. **Lever — add `location`/`selectedLocation` to standard fill; add `surveysResponses[*]` to discovery.**
   *(layer 1, MED)*
5. **Greenhouse — detect "no apply form present"** (Postman scraped board chrome) and **re-verify react-select
   1b fill landing** with a fresh dry-run. *(layer 1/1b, MED)*

**(a) New classifier label patterns (`answer_resolver.py`) — cheap, deterministic:**

6. **LOCATION variants** — `classify_profile_field` misses **bare "Location"** and **"Where are you
   *currently* located?"** (the `\bwhere are you (located|based)\b` pattern doesn't allow an adverb between
   "you" and "located"). Add `r"\blocation\b"` (→ LOCATION/CITY) and allow `(currently|presently|now)?`
   between "you" and "located/based". Bank has location → immediate fill on every Ashby form. *(2a, HIGH)*
7. **PHONE profile field** — add `ProfileField.PHONE` (`r"\bphone\b"`, `r"\bmobile\b"`, `r"\btelephone\b"`)
   pulling `contact.phone`; Ashby never fills phone as a standard field and relies on the resolver. *(2a,
   MED-HIGH)*
8. **START-DATE / availability** — add a pattern for "when can you start", "earliest/available start date",
   "availability/notice to start". Pairs with fix #10. *(2a, MED)*
9. **Onsite / work-model** — extend relocation handling (or a new pattern) for "able to work onsite?",
   "in-office", "remote/hybrid preference". Lower confidence (no clean bank field; relocation prefs partly
   cover). *(2a, LOW)*

**(b) New fact-bank fields + onboarding capture:**

10. **`availability` / earliest start date** (e.g. "2 weeks", "Immediately") — no field exists; required on
    some Ashby forms. *(2b, MED)*
11. **Seed `primary_nationality` + `notice_period`** — the code paths exist (optional profile extras) but the
    production bank values are **empty**, so the GH baseline's "Primary Nationality" and "Notice period"
    still fall through and bail. An onboarding step (or `av3 ask --save`) closes them. *(2b, MED)*

**(1b) Fill-mechanics (after labels/discovery land the questions on the resolver):**

12. **Checkbox/radio option-click fill** (Lever yes/no cards, pronouns) and **Yes/No `<button>`-group click
    fill** (Ashby) — `fill_resolutions` currently handles only input/textarea/combobox/`<select>`. Without
    this, even a correctly-resolved "Yes" can't be applied on Lever/Ashby. *(1b, HIGH — gates all the
    layer-1 wins on those two ATSes)*

### Honesty-correct misses — VERIFIED, do NOT "fix"

- Greenhouse "I have read… Candidate Privacy Policy and AI Guidelines" → CONSENT bail. ✅
- Ashby "I confirm I have read the above." (certification attestation) → CONSENT bail. ✅
- All EEO/demographic (Gender, Pronouns, "What gender do you identify as?") → self-ID-or-prefer-not, never
  invented. ✅
- All essay/open-ended cards (languages, "complex LLM deployment", "what should we know", achievement,
  Additional Information) → bail to assisted (or DRAFT when `draft_freeform` is on). ✅
- Blank "How did you hear?" is by design (DISCOVERY-derived) — not flagged. ✅

### Suggested next build session

Implement fixes #1–#3 + #6 + #12 first (they unlock the most fields for the least code), reseed #11, then add
**one Lever and one Ashby apply form to the `smoke` suite** as a live selector-drift guard (the GH baseline is
already the regression anchor). The reusable Playwright ground-truth probe (native + custom-widget
enumeration beside each driver's exact discovery selector) is the smoke-test shape — keep it.

---

## Build session (2026-06-22) — fixes SHIPPED

Implemented the top recommendations the same day. All changes verified read-only (no live fill/submit).

**What shipped:**

- **#6/#7 classifier patterns** (`answer_resolver.py`): `LOCATION` now catches bare "Location" + an adverb
  between "you" and "located/based" ("Where are you *currently* located?"); new `ProfileField.PHONE` pulls
  `contact.phone` (Ashby renders phone as a UUID custom Q the resolver was bailing on). CITY still wins for
  "Location (City)". 9 new unit tests in `test_answer_resolver.py`.
- **#1/#2/#4 Lever discovery** (`lever_apply.py`): now scans `urls[*]` (incl. the **required `urls[LinkedIn]`**
  the bank fills), `surveysResponses[*]` (EEO), and `location`; reads the question text from
  `.application-question > .application-label` (was grabbing "Yes"/"No"/""); groups checkbox/radio by name into
  one `kind='radio'` question with `options`; filters hidden carriers. **Live-verified** on mistral + qonto:
  6 mangled → 13/9 well-labeled discoveries.
- **#2/#3 Ashby discovery** (`ashby_apply.py`): container-anchored on `.ashby-application-form-field-entry`
  with the title as the label, widget-typed (`radio` for Yes/No `<button>` groups w/ option texts, `combobox`,
  `select`, `input`/`textarea`); id/name-less widgets get a synthetic `ashby_q<n>` id (so a required one routes
  to assisted, not silence); a leftover pass keeps the consent checkbox (CONSENTISH-gated) without enumerating
  multi-select option-checkboxes as bogus questions. **Live-verified** on openai (8 clean, work-auth/sponsorship
  now `radio[Yes,No]` w/ correct labels) + plaid (11 clean; the 18 matrix-junk rows eliminated).
- **#12 option-group fill** (`apply_base.py`): new `fill_option_group` + `kind=='radio'` branch in
  `fill_resolutions` — clicks the option matching the resolved value within the question's container (works for
  both Lever input/label and Ashby `<button>`). **Conservative match**: exact, else a single unambiguous
  whole-word hit — a bare "No" against several "No - …" options bails (→ assisted), never guesses. 5 unit tests.

**Verification:** discovery JS confirmed against the 4 live forms (read-only); classifier + fill-mechanics
changes covered by unit tests to the repo's fake-page standard. Full `pytest tests/` re-run after the batch.

**Deferred (documented, not done):** #8/#10 start-date pattern + `availability` fact-bank field (needs a model
+ onboarding change); #11 reseed `primary_nationality`/`notice_period` (user data entry); **fill for Ashby
id/name-less comboboxes** (Location) — discovered + surfaced now, but the synthetic-id fill no-ops, so it lands
in assisted rather than auto-filling (a container-anchored combobox fill is the follow-up); adding a Lever +
Ashby form to the `smoke` suite. The honesty bails (consent/EEO/essays) are unchanged and still correct.

---

## Testing session (live dry-run validation) — READY TO RUN

The build was verified read-only + unit tests; the one thing those can't prove is that the new **fill**
mechanics actually LAND on a real Lever/Ashby form (a dry-run fills but never submits). The harness
`C:\JobSearch\diagnose_apply_multi.py` runs the REAL driver path (`prepare_application(dry_run=True)`) for
Greenhouse/Lever/Ashby and prints, per discovered field: kind, the resolution (source/value/fills/review),
and `LAND`/`MISS`/`----` from `outcome.filled["q:<id>"]`. It opens a headed real-Chrome window and **never
clicks submit**. No login is needed to view these forms.

**Run (PowerShell, from anywhere):**
```powershell
$env:AV3_DATA_DIR='C:\Users\jar85\JobSearch\av3data'; $env:PYTHONIOENCODING='utf-8'
python C:\Users\jar85\JobSearch\diagnose_apply_multi.py            # default: 1 Lever + 1 Ashby + GH baseline
python C:\Users\jar85\JobSearch\diagnose_apply_multi.py <url> ...  # or specific apply URLs
```
Ollama must be up (the resolver embeds for semantic match). `resume_path=""` is fine — the audit confirmed
every field renders without an upload; pass a résumé only if a form gates questions behind résumé-parse.

**Pass criteria (what "the build works" looks like):**

| ATS (sample) | Must LAND (fills + on-page) | Must BAIL (review = correct) | Known-acceptable MISS |
|---|---|---|---|
| **Lever** mistral | `urls[LinkedIn]` (profile/linkedin), `urls[GitHub]`, `location` (profile/location), work-auth radio → "Yes" (fact_bank) | the 4 essay textareas (open-ended), gender survey only if no self-ID banked | Twitter/Scholar/Portfolio URLs (absent in bank → bail) |
| **Ashby** openai | `Phone Number` (profile/phone), work-auth + sponsorship radios (fact_bank Yes/No, clicked via button group) | "When can you start?" (no availability field → assisted), consent checkbox, Additional Information | **Location combobox** — resolves to the bank location but the synthetic-id fill no-ops → `MISS` (deferred; lands in assisted) |
| **Greenhouse** imagineworldwide | all profile/EEO/salary/how-heard (regression baseline) | — | Primary Nationality + Notice period (bank empty, 2b) |

**The headline checks:** (1) Lever `urls[LinkedIn]` shows `LAND` with `src=profile` — proves the #1
discovery fix + that the required LinkedIn the bank holds now fills. (2) Ashby work-auth + sponsorship show
`LAND` via the radio/button option-group — proves #12 fill mechanics on a real React form (the part unit
tests couldn't reach). (3) The Greenhouse baseline is unchanged.

**Capture for the next session:** paste the per-field tables + the RESULTS summary. Anything that resolves
`fills=True` but shows `MISS` (other than the known Ashby Location combobox) is a fresh fill-mechanics gap to
chase. After a run, `av3 errors --since 30m` and the `resolution` events in `events.db` (now populated for
Lever/Ashby too) corroborate the on-page table.
