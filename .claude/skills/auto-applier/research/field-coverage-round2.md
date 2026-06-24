# Field coverage — Round 2 (onboarding-captured fields + classifiers + Ashby combobox fill)

**Status:** PLANNED (handoff 2026-06-24). Round 1 (discovery rewrites + option-group fill + Lever location)
is SHIPPED + live-verified — commits `abfb4db`, `07cd8b5`, `03ea750` on master. This doc is the next build:
fill the remaining real misses surfaced by the 2026-06-24 live dry-run, per the owner's decisions.

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
