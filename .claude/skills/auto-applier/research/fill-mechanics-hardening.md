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
  once per daily discovery run, for weeks. **RESOLVED — see "Dead boards" below.**

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
* **Lever / Ashby live `smoke` tests** — outstanding since Round 1.

---

## Round 3b (2026-07-31) — post-fill READ-BACK verification — SHIPPED

The follow-up flagged above, done. `filled[q:<id>]` is no longer the filler's own word.

### Design

`verify_fills(page, questions, resolutions, filled)` runs after `fill_resolutions` in all three
drivers and re-reads the page. It is deliberately **asymmetric**:

> Read-back may only ever **demote** a claim, and only on **positive evidence** (we located the
> control and it is empty, or holds something that disagrees). A control we cannot read keeps
> its claim.

The asymmetry is the whole safety argument: a false negative would push perfectly good applies
into assisted for no reason, so "unknown" must never mean "failed".

`read_back_fills` does it in ONE batched `page.evaluate`, and shares the container-resolution
ladder with the option-group clicker via `_CONTAINER_JS` — one path, so a scoping fix cannot
drift between *where we clicked* and *where we check what landed*.

### Selected-state contracts (from live DOM probes, read-only)

| Widget | Where the committed value lives |
|---|---|
| native `input` / `textarea` | `.value` |
| native `<select>` | the selected option's TEXT |
| radio / checkbox | the `checked` one's label |
| react-select (Greenhouse) | `.select__single-value` — **the text `<input>` is CLEARED on commit** |
| Ashby geocoder combobox | `input[role=combobox].value` |
| Ashby Yes/No buttons | no ARIA state at all (live Vanta: both buttons carry identical hashed classes at rest) |

Two traps found while building it, both caught by the harness:

1. **The react-select input is empty after a successful commit.** Reading only the addressable
   element would have demoted *every* filled Greenhouse combobox — and, with the new required-fill
   gate, pushed every GH auto-apply to assisted. Fix: an empty direct read is NOT conclusive; it
   falls through to the container probes, while still remembering "located, so `known`".
2. **Ashby's Yes/No selection is a class, not an attribute.** The selected button gains an extra
   (build-hashed) class. Rule: take the INTERSECTION of every button's class list as the at-rest
   baseline; the selected button is the single one carrying classes beyond it. Hash-agnostic, and
   it works for a 2-button group where an "odd-one-out by count" rule cannot discriminate (both
   signatures are unique). Nothing diverging ⇒ `known: false`, never a guess.

Agreement is tolerant of legitimate reformatting — **whole-word** containment either direction
("Dallas" ↔ "Dallas, Texas, United States"; "6" ↔ "6 to 9 years") plus digits-only containment
when both sides carry ≥4 digits (intl-tel-input renders `+16827188130` as `+1 682 718 8130`).
Whole-word, not substring: substring would let "No" agree with "Nope, never used it", which is
exactly the wrong-option bug read-back exists to catch.

### New auto-submit gate

`any_required_unfilled(questions, resolutions, filled)` joins the §8b downgrade in all three
drivers. `any_required_unresolved` covers "no confident answer"; this covers **"had an answer,
didn't land"** — previously unknowable, and just as fatal on an auto-submit (validation failure,
or an incomplete application submitted). `q_filled(outcome)` unprefixes the `q:<field_id>` map.

### What this fixes beyond the drivers

* **`events.db` `resolution` rows now carry a VERIFIED `filled_on_page`** (`apply_worker` L1031).
  The Round-1/2 audits used that column as ground truth while it was self-reported; from here it
  is real, so future coverage audits can trust it.
* **The E2 "Fill what it can" button's "filled N / M left"** (`routes.py` L850) is now truthful.

### Verification

8 checks, all PASS in headless Chromium against fixtures built from the live contracts: empty
demotes, filled kept, Ashby button-group read via class divergence, unreadable kept, react-select
value read, reformatted phone agrees, and the required gate both fires and clears.

---

## Round 3c (2026-07-31) — the `browser` test tier — SHIPPED

The scratchpad harnesses are now a permanent, opt-in tier: `tests/test_fill_mechanics_browser.py`,
marker `browser`, run with **`pytest -m browser`** (8 tests, ~8s). Deselected by default alongside
`smoke`/`eval`/`integration`.

**Why it had to exist.** The five Round-3 defects all shipped with green unit tests, because the
fake pages stub `page.evaluate` — they assert that Python *called* the JS, never that the JS does
the right thing. Live `smoke` tests would catch these but need the network and a live posting, so
they can't gate a commit. This tier is the middle: **the real JS, a real engine, zero network**,
with fixtures mirroring DOM contracts captured read-only from live forms.

**It has teeth — verified by mutation.** Reverting `scope = container` to the original
`container || document` makes `test_option_click_bails_when_the_container_cannot_be_identified`
fail immediately (`assert True is False`), then the file was restored from git. Note the
*other* scoping test still passes under that mutation: the label-matching rung finds the right
container on its own, so the ghost-question test is the one specifically guarding the fail-open.

**Coverage:** F1 scoping + fail-closed bail, F2 checkbox never typed into, F3 conservative
dropdown commit, F4 select snapping, plus read-back across all five widget contracts, the
demote-only asymmetry, and the required-fill gate firing *and* clearing.

Suite after Round 3a+3b+3c: **1554 passed / 21 deselected** (was 1521 / 13 at session start).

---

## Dead boards — "keep trying, mark it `failure - 404`" (2026-07-31) — SHIPPED

**Owner decision.** Presented as a fork (auto-disable after N consecutive 404s vs. surface it
and leave it alone); the owner chose: *"I would not remove it from targeting, we should try and
if it fails just mark it as a 'failure - 404'."* So a dead board is **never dropped** — it is
swept every run and self-heals the day the company's board comes back. What changes is the
BOOKKEEPING.

**Why the bookkeeping mattered.** The owner's spine carried 13 identical
`GreenhouseError: board token 'dbtlabsinc' not found (404)` rows — one per daily run for weeks —
in the same bucket as real failures. Recurring known noise is exactly what hides a new problem.

**What shipped**

* `sources/errors.py` → **`BoardNotFound(ats, token)`**, a cross-ATS signal distinct from a
  generic source error (404 = permanent and specific; a timeout or 5xx = transient).
* All three sources raise it on 404. This also closed a real hole: **Lever and Ashby returned
  `[]` on ANY non-200**, so a dead token there was indistinguishable from "this company has no
  open roles" — completely invisible. Greenhouse was the only ATS that reported one at all.
  Other non-200s stay tolerant (unchanged).
* `discover_worker` catches it → `StageSkip("failure - 404: board token '<token>' not found")`,
  so the event spine records a **skip with the reason** rather than an error. New
  `summary.boards_missing`, counted separately from `board_errors`. `BOARD_404_REASON` is the
  one marker constant.
* `doctor.check_boards` is the other half of the bargain — keeping 404s out of `av3 errors`
  only works if something still surfaces them. WARN (never FAIL) naming each board, with a fix
  hint that says they're *still swept* so the behaviour isn't mistaken for a silent drop. It
  reads **both** the new skip rows and **legacy error rows**, so it works against an existing
  `events.db` immediately.

**Verified against the owner's real data** (read-only): `check_boards` reports
`WARN: 1 board token(s) returning 404: greenhouse:dbtlabsinc` at both 14d and 60d.
14 tests in `tests/test_board_404.py`; 1568 green.

Two pre-existing tests encoded the old behaviour and were updated to the new contract
(`test_greenhouse.py::test_discover_bad_token_raises`,
`test_lever_ashby.py::test_lever_discover_bad_site_empty` → `..._raises_board_not_found`).

---

## Lever / Ashby live smoke — the layer-3 gap (2026-07-31) — SHIPPED

Outstanding since Round 1. The smoke suite already had two layers: **discovery smoke** (public
APIs still return jobs) and **form-load smoke** (the ~4 STANDARD selectors still resolve in live
HTML). Neither touched **custom questions** — which is where every field-coverage round did its
work, and where ATS markup actually churns. So the Lever `urls[*]` family, the container-anchored
labels, widget typing, and the selectors the Round-3 read-back depends on had **no live guard**.

**Layer 3** (`tests/test_live_smoke.py`, marker `smoke`): each test runs the driver's REAL
`discover_custom_questions` against a live posting and diffs it against a ground-truth DOM probe
(`_GROUND_TRUTH_JS`) — the apples-to-apples shape the Round-1 audit recommended keeping. Targets
are resolved from the public API at run time (`_first_live_listing`), so nothing is pinned to a
posting that will close. Read-only; discovery is a `page.evaluate` that only reads.

| Test | Guards |
|---|---|
| Lever | labels come from `.application-question > .application-label` (never an option label); every `urls[*]` on the page is discovered; a page with questions yields questions |
| Ashby | `.ashby-application-form-field-entry` + `.ashby-application-form-question-title` both still exist (**discovery AND read-back key off this pair**); no `_systemfield_*` leaks into custom questions; Yes/No widgets type as `radio` WITH options |
| Greenhouse | react-select comboboxes type as `kind='combobox'` (mistyped ⇒ typed into instead of opened, nothing commits) |

Shared `_assert_labels_are_real` is the highest-value assertion and has no false-alarm mode: an
empty label, or an OPTION label ("Yes"/"No") standing in for the question, is the exact Round-1
regression that sank work-auth, sponsorship and every essay card.

**Non-vacuity is explicit.** Label assertions pass trivially on an empty discovery — the very
failure mode (silently finding nothing) they exist to catch — so both Lever and Ashby also assert
that a page with question containers yields questions.

**Verified live, and mutation-tested.** All 10 smoke tests pass against real postings (44s).
Renaming the Ashby title selector and the Lever label selector made the new tests fail with
precise, actionable messages naming the live form:

```
Lever  …/matchgroup/…/apply: 2 discovered question(s) have NO label
       (['cards[…][field1]', 'cards[…][field0]']) -> the container/label selector drifted
Ashby  …/Linear/…/application: page has 1 Yes/No widget(s) but discovery typed none as
       kind='radio' -> they'd route to the text path and never land
```

Both drivers were then restored from git. Default suite unaffected: 1568 green, 24 deselected.

### Test tiers, end to end

| Tier | Runs | Catches | Cost |
|---|---|---|---|
| unit (fake pages) | always | Python-side logic, wiring, contracts | ms |
| **`-m browser`** | opt-in | the fill/read-back **JS** against fixed DOM contracts | ~8s, needs Chromium |
| **`-m smoke`** | cron | **live selector drift** — the #1 v2 bug source | ~44s, needs network |
