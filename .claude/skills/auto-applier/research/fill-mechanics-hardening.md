# Fill-mechanics hardening (Round 3) — fail-closed scoping + option snapping

**Status:** findings CONFIRMED 2026-07-31 with a real headless Chromium against DOM fixtures
(read-only, no network, never submitted). Fixes implemented the same session.

Parent docs: `ats-field-coverage-audit.md` (Round 1 — discovery), `field-coverage-round2.md`
(Round 2 — classifiers + Ashby combobox fill). Rounds 1–2 fixed **discovery** (finding the
field) and **classification** (knowing the answer). Round 3 is about the **last inch**:
whether the correct answer lands on the **correct control**.

## Why this round exists

Rounds 1–2 measured coverage as `filled[q:<id>] == True` ("LAND"). That signal is
**self-reported by the filler** — it never verified that the value landed on the question it
was meant for. This round tested the actual fill JS in a real DOM and found that a "LAND"
can be a **wrong answer on a different question**.

## Verification harness

`verify_fill_js.py` (scratchpad) launches headless Chromium via the project's own patchright,
sets HTML fixtures modeled on the live Ashby / Greenhouse DOM contracts recorded in Rounds 1–2,
arms a click recorder, and calls the **real** `apply_base` functions. No network, no live ATS,
no submits. Re-runnable; it is the shape a future browser-level regression test should take.

## Findings (all CONFIRMED, real browser)

### F1 — `fill_option_group` FAILS OPEN to whole-document scope (severity: HIGH, honesty-critical)

`_OPTION_GROUP_CLICK_JS` resolves the question container from `field_id`:

```js
let anchor = document.querySelector(`[name='${fid}']`) || document.getElementById(fid);
const container = anchor ? anchor.closest('.application-question, .ashby-…-field-entry, …') : null;
const scope = container || document;      // <-- FAIL-OPEN
```

When the anchor can't be resolved, the option search widens to the **entire document** and
clicks the first element whose text matches. Two independent ways to reach it:

1. **Ashby synthetic ids.** `ashby_apply.discover_custom_questions` assigns `ashby_q<n>` to
   id/name-less widgets. Ashby renders Yes/No questions as `<button>Yes</button>` +
   (frequently) a `type=hidden` carrier, and the discovery input filter drops `type=hidden`
   — so `named` is undefined and the question gets a **synthetic id that matches no element**.
2. Any DOM change that removes the expected container class (selector drift — the #1 v2 bug
   source), even when the anchor resolves.

**Observed on the fixture** (two Ashby-style Yes/No questions, both synthetic ids):

| Call | Intent | Actual click |
|---|---|---|
| `fill_option_group(q="ashby_q1", "Yes")` | work-auth = Yes | `workauth=Yes` ✅ |
| `fill_option_group(q="ashby_q2", "No")` | sponsorship = No | **`workauth=No`** ❌ |

Both returned `True` (reported as LAND). Net effect on a real form: the applicant is recorded
as **NOT authorized to work**, sponsorship is left blank, and the run reports two successful
fills. This directly violates the project's fail-closed invariant *and* the honesty invariant —
a wrong answer is worse than a bail, because a bail routes to assisted where the human fixes it.

This also means the Round-2 live-run "work-auth → Yes LAND, sponsorship → No LAND" result on the
openai Ashby form **cannot be trusted as evidence of correct answers** — it is consistent with
both questions' buttons being clicked inside the first field-entry. (Whether it actually misfired
depends on whether that form's carriers were `type=hidden`; the LAND signal cannot distinguish.)

**How often does it actually fire? — live probe, Vanta Ashby form, 2026-07-31** (read-only).
Discovery replayed against a current form gave 11 field-entries:

| Entry | Title | kind | id | synthetic? |
|---|---|---|---|---|
| 1–3 | Full Name / Email / Resume | — | `_systemfield_*` | (driver-filled) |
| 4–7 | LinkedIn / Phone / Current Company / **Current Job Title** | input | real UUID | no |
| 8 | **Location** | combobox | `ashby_q8` | **YES** |
| 9 | Are you legally authorized to work…? | radio | real UUID | no |
| 10 | Will you… require visa sponsorship? | radio | real UUID | no |
| 11 | What are your pronouns? (Optional) | input | real UUID | no |

So on **this** layout the Yes/No radios carry real UUID ids and resolve their container correctly —
the wrong-question click did **not** fire here. The synthetic id lands on the **combobox**, which
routes through `fill_ashby_combobox`, i.e. **F5 (positional re-derivation) is the live-relevant
one on current Ashby, and F1 is a latent fail-open** that fires only on forms whose choice entry
has no non-hidden named input. Both are fixed; do not downgrade F1 on that basis — a latent
fail-open in the one place the project promises fail-closed is still the defect.

**Greenhouse live probe (Monzo, same day):** 21 text inputs, **12 react-select comboboxes, 0
native `<select>`, 0 radios, 1 checkbox** (a GDPR demographic-data consent gate). Consequences:
(a) the GH radio/checkbox-grouping gap is **not** a live concern on the current layout — deprioritize;
(b) the one checkbox on the form is exactly the F2 hazard, and it is a *consent* gate — the resolver
bails it today, but before F2 any classifier miss would have TICKED a GDPR consent box on the
owner's behalf; (c) `snap_to_option` doesn't help GH (no native selects, and react-select options
aren't in the DOM until opened) — it's a Lever/Ashby win.

**Fix:** never widen to `document`. Resolve the container by, in order: `[name=fid]` →
`getElementById(fid)` → for `ashby_q<n>`, the nth `.ashby-application-form-field-entry` →
a container whose question-title/label text matches the question's `label`. No container →
return `false` → the required field routes the job to assisted. Fail closed.

### F2 — a checkbox reached via `kind='input'` is CLICKED regardless of the answer (severity: HIGH)

`fill_resolutions`' text branch calls `human_type`, which **clicks the element to focus it**
before typing. On a checkbox that click is a **toggle**, and the typed characters are discarded.

**Observed:** `human_type(page, "#question_1", "No")` on an unchecked
`<input type=checkbox>` returned `True` and left the box **checked**. A resolved answer of
"No" therefore affirms the question.

Reachable on Greenhouse: `greenhouse_apply.discover_custom_questions` maps every non-textarea,
non-select, non-combobox control to `kind='input'` — including `type=checkbox` and
`type=radio`. (Lever and Ashby type their choice widgets as `radio` during discovery, so they
route to the option-group path; Greenhouse does not.)

**Fix:** in `fill_resolutions`, detect the control's real type at fill time and route
checkbox/radio to `fill_option_group` regardless of the discovered `kind`. Discovery
mis-typing then degrades to "bails to assisted", not "answers the opposite".

### F3 — `settle_open_dropdown` commits the WRONG react-select option (severity: MEDIUM)

`settle_open_dropdown` (the cleanup path after typing into a field that turned out to be a
react-select) matches with `want == text or want in text or text in want` — the loose
substring test that `_click_combobox_option` deliberately replaced with whole-word matching.

**Observed:** value `"No"` against options `["Nope, never used it", "No"]` committed
**"Nope, never used it"**.

**Fix:** delegate to `_click_combobox_option` (exact → decline-synonym-only → whole-word),
so both combobox paths share one conservative matcher.

### F4 — resolved values are never SNAPPED to the form's own options (severity: MEDIUM, coverage)

The resolver returns canonical values ("Yes", "No", "Prefer not to answer", "6") but never
consults `question.options`, even though discovery scrapes them for native `<select>` and for
Lever/Ashby choice groups. The fillers then need an exact-ish match:

* `page.select_option(sel, "Yes")` sends `{valueOrLabel: "Yes"}` — Playwright requires an
  **exact** match on the option's `value` **or** its label (verified in
  `patchright/_impl/_element_handle.py::convert_select_option_values`). So "Yes" against
  `<option>Yes, I am authorized to work in the US</option>` **fails**.
* `fill_option_group` bails on an ambiguous whole-word match (correct, but a miss).

Every such miss is a coverage loss (job → assisted) rather than a wrong answer, so this is a
throughput issue, not a safety one — but it is the bulk of the remaining "it didn't fill
everything" surface the owner reported.

**Fix:** one shared `snap_to_option(value, options)` helper using the SAME conservative
ladder as the click matchers (exact → decline synonyms when the value is a decline →
single unambiguous whole-word hit → else `None` = bail). Apply it in `fill_resolutions`
for `select` (then `select_option(label=…)`) and `radio`. Ambiguity still bails.

### F5 — Ashby combobox re-derivation is positional (severity: LOW-MEDIUM)

`_locate_ashby_combobox` re-derives the field-entry from the **index** encoded in
`ashby_q<n>`, captured at discovery time. Ashby reveals conditional questions as you answer
earlier ones, so the entry list can grow between discovery and fill — shifting every later
index and typing a location into the wrong widget. Same class of bug as F1.

**Fix:** verify the re-derived entry's question title against `question.label` before using
it; on mismatch, search the entries for the matching title; no match → `None` (bail).

## Invariant this round adds

> **A fill must be provably scoped to the question it was resolved for.** If the filler cannot
> identify the exact control for a question, it must report `False` (→ assisted), never widen
> its search. A wrong answer is strictly worse than no answer, because no answer routes to a
> human and a wrong answer does not.

Corollary for measurement: `filled == True` is **not** evidence of a correct fill unless the
fill was container-scoped. Live-run LAND tables from Rounds 1–2 should be re-read with that
caveat.

---

## Build session (2026-07-31) — SHIPPED

All five findings fixed; verified by re-running the browser harness (every check PASS) plus new
unit tests. Full suite green.

| # | Change | File |
|---|---|---|
| F1 | `_OPTION_GROUP_CLICK_JS` resolves the container by name/id → Ashby synthetic index (title-confirmed) → matching question label → a **bounded** 4-ancestor walk from the anchor. No container ⇒ `false`. **The `scope = container \|\| document` fallback is gone.** `fill_option_group` now passes `question.label` through. | `apply_base.py` |
| F2 | `_control_kind(page, selector)` probes the live control; `fill_resolutions`' text branch routes a real checkbox/radio to `fill_option_group` instead of `human_type`. Stubs without `get_attribute` yield `''` (unchanged behaviour). | `apply_base.py` |
| F3 | `settle_open_dropdown` delegates matching to `_click_combobox_option` — one conservative ladder for both combobox paths. | `apply_base.py` |
| F4 | New `snap_to_option(value, options)`; applied to `radio` and `select`. A `select` with scraped options and no confident match now bails explicitly instead of burning a `select_option` timeout. | `apply_base.py` |
| F5 | `_locate_ashby_combobox` confirms the positional entry against the question title and re-finds it by title when the list shifted; unreadable title ⇒ keep positional (no regression). | `ashby_apply.py` |
| F6 | New `ProfileField.CURRENT_TITLE` → `work_history[0].title` (bails with no history, like `CURRENT_COMPANY`). Found on the live Vanta form: "Current/Most Recent Job Title" classified as NONE while the company sibling filled. Patterns deliberately exclude **desired/preferred** title (a targeting preference, not a bank fact). | `answer_resolver.py` |

**Harness results after the fixes** (real headless Chromium, DOM fixtures):

```
H1  scoping     : returns=(True,True) clicks=['workauth=Yes', 'sponsorship=No']   PASS
H1b fail-closed : returned=False clicks=[]                                        PASS
H2  checkbox    : fill_resolutions('No') -> filled=False, checked=False           PASS
H3  dropdown    : committed=True clicked=['option:No']                            PASS
H4  select snap : filled=True selected value='7001' (Yes, I am authorized…)       PASS
```

New tests: `test_apply_driver.py` (unknown-container bail, label passthrough, `snap_to_option`
ladder, select snap + select bail, live-checkbox routing) and `test_ashby_apply.py`
(entry-shift follows the label, no-title-match bails). `_OptionGroupPage` now models the
fail-closed contract — an unknown `field_id` returns `False` rather than matching some other
question's options.

### Production evidence gathered this session (owner's live data dir, read-only)

`av3 errors --since 60d` over `C:\Users\jar85\JobSearch\av3data` — 43 errors total, so the
system is healthy, but two are recurring and self-inflicted:

* **27 × `EmbeddingError` in the `filter` stage** ("Ollama embeddings unreachable: Server
  error"). Fail-open is working as designed (those jobs route to DESCRIBED), but each one then
  pays a full JD-scrape + LLM score that the pre-filter existed to avoid. **A single retry with
  a short backoff around the per-job embed would recover most of them** — the errors cluster in
  time, which reads as transient Ollama 500s, not a config fault.
* **13 × `GreenhouseError: board token 'dbtlabsinc' not found (404)`** — the *same dead token*,
  once per daily discovery run, for weeks. Seed lists should auto-disable a token after N
  consecutive 404s (a 404 is permanent, unlike a timeout) instead of re-erroring forever.

Pipeline state at the time: `DECIDED 892 / APPLIED 29 / SKIPPED 20`, nothing in
QUEUED_APPLY/REVIEW — consistent with the owner's discovery+scoring+manual-apply mode
([[project_personal_search_goal]]), not a stalled pipeline.

### Still open (not done this round)

* **Greenhouse radio/checkbox grouping.** `greenhouse_apply.discover_custom_questions` maps
  every non-textarea/select/combobox control to `kind='input'` and does not group choice inputs
  by `name`, so a GH radio group would surface as N questions each labelled with its OPTION text
  ("Yes", "No"). F2 makes this SAFE (no wrong toggle). **Deprioritized**: the live Monzo probe
  found 0 radios and 0 native selects on the current GH layout, so there is nothing to group
  today. Revisit only if a GH form with radios shows up.
* **Options are never shown to the LLM tier.** `_resolve_via_llm` answers in free prose with no
  knowledge of the allowed choices, so an option-constrained question often produces a value
  that `snap_to_option` can't map. Passing `question.options` into the copilot prompt would
  convert a chunk of the remaining bails into fills.
* **No read-back verification.** `filled[q:<id>]` is still self-reported by the filler. A
  post-fill pass that re-reads each control's value/checked state and compares would make
  coverage measurable rather than asserted — and would let the driver downgrade to assisted
  when a required field silently didn't take. This is the single highest-value follow-up.
* **Lever / Ashby live `smoke` tests** — outstanding since Round 1.
