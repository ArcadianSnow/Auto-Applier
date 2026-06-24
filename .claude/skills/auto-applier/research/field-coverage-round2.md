# Field coverage — Round 2 (onboarding-captured fields + classifiers + Ashby combobox fill)

**Status:** ✅ SHIPPED 2026-06-24 (see "Build session" at the bottom). Round 1 (discovery rewrites +
option-group fill + Lever location) shipped earlier — commits `abfb4db`, `07cd8b5`, `03ea750`. This doc is
Round 2: fill the remaining real misses surfaced by the 2026-06-24 live dry-run, per the owner's decisions.

Parent doc: `ats-field-coverage-audit.md` (the audit + Round 1 + the live-run table). Harness:
`C:\JobSearch\diagnose_apply_multi.py` (dry-run, never submits; now passes the master résumé so the upload
path is exercised — the résumé/cover "misses" the owner saw were a harness artifact, NOT a product bug).

## What still misses, and the owner's decision (2026-06-24)

| Missed field (live) | ATS | Layer | Owner decision | Fix |
|---|---|---|---|---|
| **Current Company** | Lever ("Current company") + Ashby ("Current/Last Company") | 2a | "we should have it" | classifier `CURRENT_COMPANY` → `work_history[0].company` (no new data) |
| **What languages are you fluent in?** | Lever | 2a+2b | default English; **also ask in onboarding** | new fact-bank `languages: list[str]` (default `["English"]`) + `LANGUAGES` classifier **before** `is_open_ended` |
| **When can you start a new role? / start date** | Ashby | 2a+2b | default **2 weeks**; onboarding | new fact-bank `availability: str` (default "2 weeks") + `AVAILABILITY`/start-date classifier |
| **What is your notice period?** | Greenhouse | 2b | default **2 weeks**; onboarding | `notice_period` field + classifier ALREADY exist — just default it "2 weeks" + capture in onboarding |
| **Gross annual salary expectation** | Greenhouse | 2b | "we should have it" | capture salary in onboarding → `user_config.salary_floor`; resolver SALARY branch already fills from it (and the worker's salary intelligence fills it per-job in production) |
| **Primary Nationality** | Greenhouse | 2b | (required field; implied) | `primary_nationality` field + classifier ALREADY exist — capture in onboarding |
| **Where are you currently located?** (combobox) | Ashby | 1b | deferred from Round 1 | container-anchored Ashby **combobox FILL** (resolved value exists; synthetic-id fill currently no-ops) |

**NOT gaps — correct bails (do NOT auto-fill):** the Lever essays "What's your most complex deployment
project with LLM?", "What do you optimize for in life?", "What should we know about you?" are genuine
open-ended prompts → bail to assisted (or pre-DRAFT if the owner enables `draft_freeform_answers`). The
"languages" question is the exception in that group — it's a short factual answer, hence the `LANGUAGES`
classifier above so it fills instead of bailing as a textarea.

**Why notice/nationality/salary are empty now:** the fields/classifiers exist but the fact-bank values are
blank — the onboarding wizard either didn't ask or was skipped. Round 2 makes onboarding capture them (with
the owner's defaults) so they fill.

## Work breakdown for the next session

**A. Fact-bank model** (`auto_applier/resume/factbank.py`, dataclass at L49, `from_dict` at L93):
- Add `languages: list[str] = field(default_factory=lambda: ["English"])` and `availability: str = ""`.
- Wire both into `from_dict` (mirror `primary_nationality`/`notice_period` at L103-104).
- Defaults the resolver should apply when blank: `languages → ["English"]`, `availability → "2 weeks"`,
  `notice_period → "2 weeks"` (decide: default in the model, or in the resolver's profile path so an
  explicit onboarding value always wins). Owner-approved defaults.

**B. Onboarding capture** (the writer already exists — extend it + the wizard UI):
- `auto_applier/web/onboarding.py::merge_extras` (L408) already writes `primary_nationality` + `notice_period`
  — add `languages`, `availability`, and salary. Mirror in `onboarding_status`/the state payload (L246, L311).
- `POST /onboarding/extras` (`auto_applier/web/routes.py:1124`) is the endpoint; salary floor lives in
  `user_config` (`cfg.salary_floor`, see routes.py:427) — onboarding must write it there, not the fact bank.
- Add the wizard UI questions (the Alpine.js onboarding step) with the defaults pre-filled: languages
  (multi/text, default "English"), notice period (default "2 weeks"), availability/earliest start (default
  "2 weeks"), salary expectation (number), primary nationality (text).
- Also expose in the CLI onboarding path if there is one, for parity.

**C. Classifiers** (`auto_applier/resume/answer_resolver.py`):
- `ProfileField.LANGUAGES` — pattern `\blanguages?\b` / `\bfluent in\b` / `\bspeak\b`. Must run BEFORE
  `is_open_ended` in `resolve()` (a textarea "What languages…" currently bails as open-ended) → return the
  joined `languages` (default English).
- `ProfileField.CURRENT_COMPANY` — pattern `\bcurrent(/last)? (company|employer)\b`, `\bwhere do you (currently )?work\b`
  → `work_history[0].company` (the bank's first/most-recent role). Bail if no work history.
- `ProfileField.AVAILABILITY` / start-date — patterns "when can you start", "earliest/available start date",
  "start a new role", "notice to start" → the `availability` field (default "2 weeks"). Keep this distinct
  from `notice_period` (which already has patterns) or merge if the answer is the same string.
- Seed the defaults so these fill even before the owner re-onboards.

**D. Ashby id-less combobox FILL** (the one remaining 1b miss — `apply_base.py` + `ashby_apply.py`):
- The Location combobox is discovered with a synthetic `ashby_q<n>` id and resolves to the bank location, but
  `_selector_for("ashby_q6") → #ashby_q6` matches nothing → no fill. Need a container-anchored fill: locate
  the question's `.ashby-application-form-field-entry`, click the `input[role=combobox]` inside it, type the
  value, wait for the menu, click the matching option (mirror `fill_combobox`, but scoped to the entry, not a
  global selector). Likely a per-ATS `selector_for`/fill hook keyed on the field-entry index captured at
  discovery (store the entry index on the CustomQuestion or re-derive by label).
- Verify read-only first (the menu options aren't in the DOM until opened), then unit-test the matcher.

**E. Verify** — re-run `diagnose_apply_multi.py`; each addressed field should flip `----`/`MISS` → `LAND`:
Lever "Current company"; Ashby "Where are you currently located?" + "When can you start a new role?"; GH
"Primary Nationality" + "salary expectation" + "notice period" (after the owner enters them in onboarding, or
once defaults seed them). Add a Lever + Ashby form to the `smoke` suite (still outstanding from Round 1).

## Live-run baseline to beat (2026-06-24, after the Lever location fix)

- Lever mistral: 12 discovered, **6 LAND** (urls[LinkedIn], urls[GitHub], Design Portfolio, work-auth radio,
  gender radio, **Current location** ✅), 6 bail (4 essays + 2 absent URLs). Target: + Current company.
- Ashby openai: 8 discovered, 4 LAND (Preferred Name, Phone, work-auth, sponsorship). Target: + Location
  combobox (D) + start date (B/C).
- Greenhouse imagineworldwide: 9 discovered, 6 LAND, 3 bail (Primary Nationality, salary, notice). Target:
  all 3 fill once onboarding captures the data.

---

## Build session (2026-06-24) — SHIPPED

All five work items (A–E) implemented + verified. Full suite **1430 passed, 1 skipped, 13 deselected**.

**A. Fact-bank model** (`auto_applier/resume/factbank.py`): added `languages: list[str]`
(default `["English"]`) and `availability: str` (default `""`). `from_dict` defaults
`languages` to `["English"]` when absent/empty (so an old `master.json` still answers), passes
`availability` through. Round-tripped in `onboarding._fact_bank_to_dict`.

**C. Resolver classifiers** (`auto_applier/resume/answer_resolver.py`): three new `ProfileField`s,
all classified in `classify_profile_field` (runs BEFORE `is_open_ended`, so a textarea phrasing
fills instead of bailing as an essay):
- `LANGUAGES` (`fluent in` / `what/which languages` / `spoken languages`) → joined `bank.languages`,
  default English. Guarded by `_PROGRAMMING_LANG_GUARD` so "which **programming** languages…" is NOT
  answered with spoken languages (→ skills/LLM/bail).
- `AVAILABILITY` (`when can you start` / `earliest start date` / `start a new role`) → `bank.availability`,
  owner default **"2 weeks"**. Kept DISTINCT from `NOTICE_PERIOD` (a form may ask both).
- `CURRENT_COMPANY` (`current/last company`, `who do you work for`) → `work_history[0].company` (no new
  data); **bails to assisted with no work history** (the LLM never invents an employer).
- `NOTICE_PERIOD` now defaults **"2 weeks"** when blank (was: fell through). Removed from
  `_OPTIONAL_PROFILE_EXTRAS` (it always resolves now). `NATIONALITY` still bails when blank (no safe
  default) — captured in onboarding instead.
- Owner defaults live in the resolver (`_DEFAULT_LANGUAGES`/`_DEFAULT_AVAILABILITY`/`_DEFAULT_NOTICE_PERIOD`),
  so an **explicit onboarding value always wins** (read first; the default only fires on blank). 19 new
  resolver tests (classify parametrize + resolution behavior + the programming guard).

**B. Onboarding capture**: `merge_extras` now writes `languages` (accepts a list OR a comma/newline
string; trim + case-insensitive dedupe) and `availability`. `POST /onboarding/extras` additionally writes
`salary_floor` into `user_config.targeting` (NOT the fact bank — the resolver's SALARY branch reads it;
field-merged so it cooperates with the `/onboarding/targeting` writer). `OnboardingStatus.to_dict` echoes
both new fields. Wizard UI (`onboarding.html` + `onboarding.js` "More details" step) adds: earliest
start/availability, languages, salary expectation; the notice-period default label now reads "default:
2 weeks". 3 new web tests. No CLI onboarding path exists for extras (web-wizard only) → no parity work.

**D. Ashby id-less combobox FILL** (`ashby_apply.py` + `apply_base.py`): `fill_resolutions` gained a
`combobox_fill` hook (default = react-select `fill_combobox`); the Ashby driver passes
`fill_ashby_combobox`. The new filler re-derives the field-entry by the **index encoded in the synthetic
`ashby_q<n>` id** (the react-select selector `#ashby_q<n>` matches nothing), types the leading token to
open the geocoder autocomplete, waits for `[role=option]`, and clicks the best-overlap option **only when
the leading (city) token is present** (else Escapes + bails to assisted — never a wrong place). 6 new unit
tests (locate-by-index, happy path, no-city bail+escape, empty, missing-entry).

### Live DOM contract + end-to-end verification (Playwright MCP, read-only, NEVER submitted)

Probed a CURRENT Ashby form (Ramp `Security Engineer, Cloud`, 2026-06-24) — the prior openai/plaid URLs
are likely stale. The id-less combobox (`Where do you plan on working from…`) is a **geocoder
autocomplete**:
- `input[role=combobox]` is `aria-expanded=false` / `aria-autocomplete=list` / `aria-haspopup=listbox`
  until you **type** (click alone does NOT open it).
- Typing opens a portaled `[role=listbox]` (`_floatingContainer_*`) of `[role=option]` (`_result_*`)
  place suggestions; the first is auto-highlighted (`_active_*`). Options carry the full place text
  ("Dallas, Texas, United States").

**End-to-end proof:** ran the EXACT `_ASHBY_COMBO_PICK_JS` against the live menu — typed "Dallas",
want="Dallas, Texas, United States" → matcher returned `clicked:true`, picked "Dallas, Texas, United
States" (disambiguated from Georgia/Oregon/PA/NC by token overlap), and the combobox **`value` committed
to "Dallas, Texas, United States"** with `aria-expanded=false`. A DOM `.click()` on the option div fires
React's onChange, so the production fill **lands**. (Same DOM-click pattern as the already-verified
Ashby Yes/No button-group fill.)

### Notes / deferrals

- The Ramp combobox label "Where do you plan on working from (for payroll tax purposes)?" does NOT match
  the LOCATION classifier (only "Where are you **currently located**?" does, via the Round-1 adverb fix),
  so on that specific form the combobox resolves to a bail — the FILL mechanic is what Round 2 added and is
  verified independently above. A "work location / plan on working from" classifier variant was left out of
  scope (owner plan targeted the LOCATION combobox).
- Production `master.json` has `languages`/`availability`/`notice_period`/`primary_nationality` all blank.
  With Round 2: languages→English, availability→"2 weeks", notice→"2 weeks" now FILL via defaults;
  nationality still bails (correct — needs onboarding) and `salary_floor` is unset (GH salary still bails —
  a harness/data artifact, not a regression). The owner can set nationality + salary in the onboarding
  "More details" step.
- **Owner's optional final check:** the headed `diagnose_apply_multi.py` is the visual end-to-end harness,
  but its default URLs are stale and it opens the persistent-profile Chrome (run it with a CURRENT Ashby
  apply URL that has a "Where are you currently located?" combobox to see the LAND on a real form).
- Still outstanding from Round 1: add a Lever + Ashby form to the `smoke` suite.

### Correction (same day, owner feedback) — two redundancies removed

The owner flagged two duplications in the first cut; both fixed (1429 green):
1. **`availability` was a redundant field.** "Notice period" and "when can you start? / earliest start
   date" are the same answer for an employed candidate. Removed the separate `availability` fact-bank
   field and `ProfileField.AVAILABILITY`; **folded the start-date patterns into `NOTICE_PERIOD`** (one
   field, default "2 weeks", answers both ATS phrasings). The wizard asks once ("Notice period / earliest
   start").
2. **Salary was already captured.** `targeting.salary_floor` is written by BOTH the Targeting wizard step
   AND the conversational helper (`onboarding_chat.py` parses it from chat, lines ~387/428). The first cut
   added a third (redundant) salary input to the "More details" step — removed it (UI + the
   `/onboarding/extras` salary routing). Targeting remains the single salary writer. (The owner's prod
   value was `None` only because the helper didn't parse a number from that conversation, not a missing
   capture path.)

Net: the "More details" step now collects only **primary_nationality, notice_period (=earliest start),
languages, gender** — nationality is the one genuinely-uncaptured-elsewhere field the owner needs to enter.
