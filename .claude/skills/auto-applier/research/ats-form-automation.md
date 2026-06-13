# ATS Form Automation — Greenhouse, Lever, Ashby

**Research question:** How automatable are the hosted application forms of Greenhouse, Lever, and Ashby
via browser automation (Playwright/patchright), and how is a successful submission confirmed?

**Why it matters:** APIs cannot submit applications for us (employer-credential-gated), so 100% of
submits go through browser automation on the ATS-hosted forms. The v3 reliability invariant is that we
mark a job `APPLIED` **only on a positive submit confirmation** — so we need to know exactly what that
signal looks like per ATS, and how brittle the whole path is.

**Method:** Live DOM inspection (Playwright) of real, currently-open application forms on each ATS in
**May 2026**, cross-checked against vendor docs and practitioner write-ups. Forms inspected:
- Greenhouse: `job-boards.greenhouse.io/anthropic/jobs/<id>` (Anthropic Fellows Program)
- Lever: `jobs.lever.co/voltus/<uuid>/apply` (Energy Markets Intern)
- Ashby: `jobs.ashbyhq.com/ramp/<uuid>/application` (AI Partnerships Manager)

> **Confidence note:** Field names/IDs below are observed live and are the *platform-level* primitives
> (same across companies). Custom-question identifiers are per-company and are **not** stable — that is
> the whole point of the custom-question section. Treat selectors as durable for the standard fields and
> as "must-discover-at-runtime" for custom questions.

---

## Greenhouse

**URL pattern.** Standard hosted board: `https://job-boards.greenhouse.io/<token>/jobs/<numeric-id>`.
The job-detail page **is** the application form (no separate `/apply` route) — fields render inline below
the description. Legacy `boards.greenhouse.io` 301-redirects to `job-boards.greenhouse.io`. Note: many
large companies embed Greenhouse via `<iframe>` on their own careers domain or proxy it behind a custom
site (e.g. Stripe redirects `job-boards.greenhouse.io/stripe` → `stripe.com/jobs`). **We should always
drive the canonical `job-boards.greenhouse.io/<token>/jobs/<id>` URL, not the company's wrapper page**, to
get the predictable DOM and avoid iframe/SPA wrappers.

**Form structure.** Server-rendered HTML, single page (not a multi-step wizard, not a SPA). A real
`<form>` exists. Standard fields use **stable element IDs**:

| Field | Selector (observed) | Notes |
|---|---|---|
| First name | `#first_name` | required |
| Last name | `#last_name` | required |
| Email | `#email` | required |
| Phone | `#phone` | optional, `type=tel`, intl-tel-input widget |
| Country | `#country` | optional, custom dropdown |
| Résumé | `#resume` (`input[type=file]`) | `accept=".pdf,.doc,.docx,.txt,.rtf"` |
| Submit | `button[type=submit]` text **"Submit application"** | |

These IDs are consistent across Greenhouse companies (verified pattern; matches what autofill extensions
like JobWizard key on via the `greenhouse.io` / `grnh.se` domain).

**File upload.** Native `<input type=file id="resume">` is present → `page.set_input_files('#resume', path)`
works directly. The visible UI also offers **"Attach" / "Dropbox" / "Google Drive" / "Enter manually"**
buttons; ignore those and target the underlying native input. Greenhouse parses the résumé but does **not**
force a "correct the parsed fields" step on the candidate — parsing populates the recruiter's view, not a
blocking candidate flow.

**Custom / screening questions.** Employer-added questions render inline as additional inputs with IDs of
the form **`#question_<numeric-id>`** (e.g. `#question_14364081008`). The numeric ID is **per-posting and
not stable across companies or even across roles**. Some required custom inputs render with **no id/name
at all**. Knockout questions are ordinary required selects/inputs — there is no special "knockout" markup;
a wrong answer just gets the candidate auto-rejected downstream. **Implication: custom questions must be
discovered at runtime by walking the form and reading the associated `<label>` text, not by a fixed
selector map.**

**CAPTCHA likelihood.** **High / effectively default.** Greenhouse ships **invisible reCAPTCHA** built
into careers-page integration options 1–4 (Core/Plus/Pro tiers). The Anthropic form we inspected had the
`g-recaptcha-response` textarea present and `gstatic.com/recaptcha/...js` loaded **on page load**. It's
per-board configurable (Configure → Job boards & posts → Spam protection sensitivity), and stricter
settings escalate to a **6-digit email-verification code** when the behavioral score looks bot-like.
Practitioners (e.g. the Zapply write-up) report Greenhouse running **reCAPTCHA Enterprise** — AI-scored,
not a clickable checkbox, and "extremely difficult to bypass." It scores mouse/typing behavior silently.

**Submission confirmation.** Redirect to a dedicated confirmation page:
`https://job-boards.greenhouse.io/<token>/jobs/<id>/confirmation`. Heading/body text is **employer-editable**
(Greenhouse "Edit application confirmation page") but defaults to a "Thank you for applying" message. So the
**most reliable signal is the URL ending in `/confirmation`**, secondarily a visible "thank"/"received"
string — never rely on exact body text. A confirmation email is also sent but arrives async (not usable as
an in-session signal).

---

## Lever

**URL pattern.** Job: `https://jobs.lever.co/<company>/<uuid>`. Apply form: append **`/apply`** →
`https://jobs.lever.co/<company>/<uuid>/apply`.

**Form structure.** Server-rendered HTML, single page, real `<form method="post">` whose `action` is the
same `/apply` URL. Standard fields use **stable `name` attributes** (mostly no IDs):

| Field | Selector (observed) | Notes |
|---|---|---|
| Full name | `input[name="name"]` | required (Lever's only hard-required text field besides email) |
| Email | `input[name="email"]` | required, `type=email` |
| Phone | `input[name="phone"]` | required on this posting |
| Current company | `input[name="org"]` | |
| Location | `#location-input` / `input[name="location"]` | autocomplete; writes hidden `#selected-location` |
| LinkedIn | `input[name="urls[LinkedIn]"]` | also `urls[Twitter]`, `urls[GitHub]`, `urls[Portfolio]` |
| Résumé | `#resume-upload-input` (`input[type=file] name="resume"`) | native input |
| Submit | `#btn-submit` (button, text **"Submit application"**) | |

The `name`-based selectors (`name`, `email`, `phone`, `org`, `urls[...]`, `eeo[...]`) are **consistent
across all Lever companies** — this is the most selector-stable of the three for standard fields.

**File upload.** Native `<input type=file name="resume" id="resume-upload-input">` → `set_input_files`
works. After upload Lever does an async **résumé-parse-then-prefill**: it can auto-populate name/company
from the parsed file. The parse result is written to a hidden `resumeStorageId` field; the form is not
fully ready until parsing settles, so **wait for `resumeStorageId` to be populated (or the parse spinner to
clear) before reading/overwriting the prefilled fields**.

**Custom / screening questions.** Rendered as **"cards"** keyed by UUID:
`textarea[name="cards[<uuid>][field0]"]` with a sibling hidden `cards[<uuid>][baseTemplate]`. The UUID is
**per-posting, not stable**. EEO questions are standard selects: `eeo[gender]`, `eeo[race]`, `eeo[veteran]`.
Pronoun questions render as a cluster of `name="pronouns"` checkboxes plus a custom text field. As with
Greenhouse, custom questions must be discovered by walking `cards[...]` inputs and reading their labels.

**CAPTCHA likelihood.** **High / present by default.** The Voltus form carried a hidden
`input[name="h-captcha-response"]` (id `hcaptchaResponseInput`) and an `#hcaptchaSubmitBtn` — i.e. Lever
uses **hCaptcha (invisible mode)** wired into the submit path. It fires on submit and can escalate to a
visible challenge if the behavioral score is poor.

**Submission confirmation.** On success Lever POSTs the form and shows a **"Thank you for applying"**
confirmation (URL typically becomes `.../<uuid>/thanks` or an on-page thank-you state). A confirmation email
("`<name>, thanks for applying!`") is sent but is async. **Detect success via the post-submit thank-you
state / `thanks` URL plus absence of the still-present `#btn-submit` and validation errors.**

---

## Ashby

**URL pattern.** Job: `https://jobs.ashbyhq.com/<company>/<uuid>`. Apply form:
`https://jobs.ashbyhq.com/<company>/<uuid>/application`.

**Form structure.** **React SPA** — this is the key difference. **There is NO `<form>` element**
(`document.querySelector('form')` → null). The page renders client-side and submission happens via an
**XHR/fetch to a backend endpoint** (`api.ashbyhq.com/applicationForm.submit`, `multipart/form-data` or
JSON with file handles), not a native form POST. The board can also be embedded as an iframe in a SPA
careers site (`_Ashby.iFrame().load`), but the canonical hosted route renders the form directly.

Standard fields use **stable `_systemfield_*` name+id**:

| Field | Selector (observed) | Notes |
|---|---|---|
| Legal name | `#_systemfield_name` | required |
| Email | `#_systemfield_email` | required, `type=email` |
| Résumé | `#_systemfield_resume` (`input[type=file]`) | required; very broad `accept` (pdf/doc/docx/odt/rtf + image/video/audio) |
| Submit | button text **"Submit Application"** (`<button type=submit>`, no native form) | |

**File upload.** Native `<input type=file id="_systemfield_resume">` → `set_input_files` works. There are
**two file inputs** in the DOM (a generic one and the system résumé field) — target `#_systemfield_resume`
specifically. Ashby uploads the file via its own handler (`file.createFileUploadHandle`) and references it
by handle in the submit payload.

**Custom / screening questions.** Rendered with field name = **raw UUID** (e.g.
`eeea6952-8ba0-47ac-b1ec-5598969bd3e1`) — no human-readable prefix at all, and **per-form, not stable**.
The phone field on the inspected form was itself a UUID-named input, i.e. even "semi-standard" fields can be
UUIDs depending on form config. Knockout questions (e.g. "Are you currently based in the SF Bay Area?")
render as **Yes/No buttons**, not radios. **Custom questions must be discovered at runtime by pairing each
UUID input with its visible `<label>` text.** No `data-testid` attributes are exposed to lean on.

**CAPTCHA likelihood.** **Present.** The Ramp form had `g-recaptcha-response` + reCAPTCHA loaded — Ashby
uses **invisible reCAPTCHA** (its "Verified Applicants" / spam-protection feature) on hosted forms. Like the
others it's behavioral-score based and per-org configurable.

**Submission confirmation.** Because it's a SPA, **there is no page redirect to rely on**. Ashby's own
guidance to integrators is explicit: *"check the response's `success` field … not doing so will result in
applications not being recorded, without any notification."* The hosted board renders an in-place
**"Application submitted"** success state (React swaps the form for a confirmation panel) and sends a
confirmation email. **Detect success via the appearance of the success/confirmation panel (and/or a 200 with
`success:true` on the `applicationForm.submit` XHR), NOT a URL change.** This is the trickiest of the three
to confirm.

---

## Automation difficulty assessment

**Is `BROWSER_AUTO` realistic on these three? Partially — with an honest caveat: CAPTCHA is the gating
risk, not the form mechanics.**

The form mechanics are genuinely friendly:
- All three expose **native `<input type=file>`** for résumé → `set_input_files` works; no Flash/custom
  drag-drop-only widgets, no résumé-correction wall that blocks submit.
- Standard fields have **stable, documented selectors** (`#first_name`/`#email` on Greenhouse;
  `name="..."`/`eeo[...]` on Lever; `_systemfield_*` on Ashby). A small per-ATS adapter covers the standard
  fields for *every* company on that ATS.
- Greenhouse and Lever are **server-rendered single-page forms with redirect/thank-you confirmation** — the
  easy case. Ashby is a **React SPA with no `<form>` and no redirect** — workable but needs DOM-state /
  XHR-based confirmation detection.

The hard parts:
1. **CAPTCHA is effectively default-on across all three** (Greenhouse reCAPTCHA Enterprise, Lever invisible
   hCaptcha, Ashby invisible reCAPTCHA). These are **behavioral-score** systems — they silently watch mouse
   and typing cadence and, on a bad score, escalate to a visible challenge or an email-verification code.
   They cannot be "solved" headlessly and **we must never retry through one** (project invariant). Good
   anti-detect (headed real Chrome, Bézier mouse paths, typing jitter, human pacing) keeps the *invisible*
   score passing in many cases, but any escalation to a visible challenge → **downgrade to assisted**.
   Practitioner reports (Zapply) confirm reCAPTCHA Enterprise is the wall that stops fully-automated submit.
2. **Custom / screening questions are per-posting and not selector-stable** (`#question_<id>`,
   `cards[<uuid>]`, raw-UUID names). They must be discovered at runtime (walk inputs → read labels → answer
   via the resolver/LLM). Any question we can't answer confidently → REVIEW.

**Verdict:** Reliable, fully-hands-off auto-apply is **not** guaranteed on any of the three because of the
behavioral CAPTCHA layer — but a **high-yield "auto-fill + auto-submit when the invisible CAPTCHA passes,
else hand off to assisted"** model is realistic, and is the right v3 posture. Expect a meaningful minority of
attempts (especially Greenhouse Enterprise and any posting with strict spam settings) to fall to **assisted**
(human clicks the final submit / solves the challenge). Lever and Ashby tend to be lighter-touch than
Greenhouse Enterprise. This matches the v2 lesson with LinkedIn: don't pretend a defended submit path is
fully automatable — **fail fast to REVIEW/assisted rather than retry through detection.**

---

## Confirmation-detection strategy

Goal: mark `APPLIED` **only on positive confirmation**, with one strategy that degrades gracefully across all
three. Use a **layered detector**, accepting success if *any* strong signal fires and no error signal is
present:

1. **URL transition (Greenhouse, Lever).**
   - Greenhouse: URL becomes `.../jobs/<id>/confirmation`. Strong signal — match `/confirmation` suffix.
   - Lever: URL becomes `.../<uuid>/thanks` (or a thank-you state). Match `/thanks`.
2. **Success panel / text (all three; required for Ashby).** After submit, wait (e.g. up to 15–30 s) for a
   visible element whose text matches a case-insensitive regex like
   `thank(s| you)|application (was )?(submitted|received)|we('| ha)ve received`. Ashby renders an in-place
   "Application submitted" panel — this is the primary signal there since there is no redirect.
3. **Submit-button disappearance + no validation errors.** Confirm the submit control
   (`button[type=submit]` / `#btn-submit`) is gone or disabled **and** no inline error/`aria-invalid` /
   "required" / "Please complete the reCAPTCHA" messages are present. (Greenhouse can show
   *"Please complete the reCAPTCHA and resubmit your application"* — treat as **failure**, not success.)
4. **XHR response (Ashby, optional hardening).** If we can observe network, a `200` on
   `applicationForm.submit` with `success: true` is authoritative. Treat `success: false` or a 4xx/5xx as
   failure even if the UI looks idle.

**Decision rule:**
- `APPLIED` only if (1) **or** (2) fires, **and** no error signal from (3).
- If a **visible CAPTCHA/challenge or email-code** prompt appears at submit → **do not retry**; set status
  `ASSISTED`/`REVIEW` and surface to the user (project invariant: never retry through CAPTCHA).
- If submit click produced **no** URL change, **no** success panel within timeout, and **no** error → set
  `UNCONFIRMED` / REVIEW (never optimistically mark APPLIED). Mid-form breakage → fail fast to REVIEW.
- **Never** mark APPLIED off the confirmation *email* alone (async, out-of-band, easily spoofed by
  unrelated mail) — email can be a secondary corroboration only.

**Per-ATS quick reference:**

| ATS | Form tech | Std-field selectors | Résumé input | Custom-Q identifier | CAPTCHA | Primary success signal |
|---|---|---|---|---|---|---|
| Greenhouse | Server HTML, single page (sometimes iframed) | `#first_name` `#last_name` `#email` `#phone` | `#resume` native | `#question_<numeric>` (unstable) | Invisible reCAPTCHA (often Enterprise), default-on | URL `/confirmation` + "thank you" |
| Lever | Server HTML, single page, real `<form post>` | `name="name"` `name="email"` `name="phone"` `urls[...]` `eeo[...]` | `#resume-upload-input` native | `cards[<uuid>][field0]` (unstable) | Invisible hCaptcha, default-on | URL `/thanks` + thank-you panel |
| Ashby | **React SPA, no `<form>`, XHR submit** | `#_systemfield_name` `#_systemfield_email` | `#_systemfield_resume` native | raw `<uuid>` name (unstable) | Invisible reCAPTCHA, present | In-place "Application submitted" panel / `success:true` XHR (no redirect) |

---

## Phase 1 implementation findings (2026-05-26)

Built the Greenhouse apply path in code (`av3/sources/browser/`): `detect.py` (pure
`classify_captcha` + `detect_confirmation`), `greenhouse_apply.py` (the Playwright driver),
`survey.py` (CAPTCHA-presence survey). Two findings worth carrying forward:

1. **A dry-run CANNOT measure the invisible-CAPTCHA auto-pass rate — only its *presence*.**
   The behavioral score is evaluated when the form POSTs, so "did it pass" only resolves on
   a real submit. We therefore split the measurement:
   - **CAPTCHA-presence survey** (`av3 survey`): loads N real forms dry-run (fill, never
     submit), classifies each challenge (none / invisible reCAPTCHA / Enterprise / visible /
     hCaptcha). Safe + autonomous + public-pages-only. Measures the problem *ceiling*.
   - **Auto-pass rate**: requires real submits → a **gated user decision** (it sends real
     applications, colliding with the "never submit during dev" rule). This is the headline
     number; it cannot be produced without consenting submits to real or throwaway postings.
2. **reCAPTCHA markers are JS-injected, so an httpx GET of the HTML won't reveal them** —
   `classify_captcha` must run against the *post-load* DOM from a real (headed) browser, not
   a static fetch. This is why the survey needs Chromium, not just `httpx`. The classifier
   logic itself is unit-tested against synthetic DOM (`tests_v3/test_detect.py`); the live
   survey is what confirms the real Greenhouse DOM carries the markers we key on.

Confirmation detector (`detect_confirmation`) encodes risk ④ as tested code: GH
`/confirmation` & Lever `/thanks` URL signals, generic success-text (Ashby in-place panel),
error-before-success ordering so a CAPTCHA/validation error never reads as APPLIED, and
UNCONFIRMED (retry-safe) as the default when no positive signal fires.

### First live CAPTCHA-presence survey (2026-05-26, `av3 survey`, n small — directional)

Ran the dry-run survey over a 10-token curated seed list. Breakdown:

| Outcome | Tokens | n |
|---|---|---|
| **Real GH form — reCAPTCHA Enterprise** | anthropic, cloudflare, tripadvisor | **3/3 = 100%** |
| Valid API but canonical URL redirects to a company wrapper (no GH form) | stripe, databricks, cribl, cargurus | 4 |
| Stale/dead token (confirm-probe = invalid) | coinbase, doordash, ecobee | 3 |

**Three findings:**
1. **Every real Greenhouse form in the sample shipped reCAPTCHA *Enterprise*** — the
   AI-scored "extremely difficult to bypass" variant, not the lighter invisible tier. This
   is the *leading indicator* that the Greenhouse **auto-pass rate will be LOW**, which per
   the Phase -1 conclusions means v3's GH value prop tilts toward "discovery + generation +
   **assisted** apply." **Caveat: n=3 real forms is too small to generalize** — a larger
   confirm-probed survey is needed to firm this up. But it matches the Zapply practitioner
   report and is directionally important. (The *actual* pass rate still requires real
   submits — presence ≠ pass.)
2. **~40% of valid Greenhouse API tokens don't serve a GH form at the canonical URL** —
   they redirect to a company careers wrapper (stripe → stripe.com, etc.). Discovery via API
   still works, but the apply path must **detect the wrapper/redirect and skip** (our driver
   already surfaces this as `form_present=False` when `#first_name` is absent). Seed-list
   quality matters: filter to tokens that actually serve the hosted form.
3. **Seed tokens decay** — 3/10 were dead. Confirm-probe before every use (the seeding doc's
   "incremental refresh + decay" is not optional).

**Custom-question load is heavy**: anthropic 29, cloudflare 27, tripadvisor 8 (avg ~21 per
form). Confirms custom questions are the norm and numerous → lots of answer-resolver work and
real REVIEW potential on novel questions.

**Implication for build order:** strongly consider pulling **Lever (invisible hCaptcha) and
Ashby (invisible reCAPTCHA)** forward — the research suggests both are lighter-touch than GH
Enterprise, so the auto-pass thesis may hold better there even if Greenhouse leans assisted.

### Cross-ATS CAPTCHA-presence survey (2026-05-26, n=16 real forms) — DECISIVE

Built Lever + Ashby discovery adapters (`av3/sources/lever.py`, `ashby.py`) and a unified
multi-source presence probe (`av3/sources/browser/survey.py`). Surveyed 16 live apply forms:

| ATS | Forms | CAPTCHA distribution | % Enterprise |
|---|---|---|---|
| **Greenhouse** | 8 | reCAPTCHA **Enterprise ×8** | **100%** |
| **Ashby** | 6 | Enterprise ×3, invisible reCAPTCHA ×3 | 50% |
| **Lever** | 2 | invisible **hCaptcha ×2** | **0%** |
| **Overall** | 16 | 100% had *some* invisible challenge | 68.8% |

**The decisive read — auto-apply viability is NOT uniform across ATSes:**
- **Greenhouse = uniformly reCAPTCHA Enterprise** (8/8, now corroborated at larger n). The
  hardest wall; the auto-pass rate here is likely low → **GH leans assisted.**
- **Lever = invisible hCaptcha, zero Enterprise**, AND server-rendered with a real `<form>`
  and the most selector-stable standard fields → **the most promising auto-apply target.**
  Caveat: sparsest live inventory (only ~2/10 candidate sites had open roles).
- **Ashby = split 50/50** Enterprise vs lighter invisible reCAPTCHA → auto may work on the
  lighter half, but it's a React SPA / XHR submit (hardest to *drive* and confirm).

So the ranking by **auto-apply viability** (CAPTCHA difficulty × form-drivability):
**Lever > Ashby(lighter half) > Ashby(Enterprise)/Greenhouse(Enterprise).** This flips the
Phase-2 source priority: **lead the auto path with Lever**, keep Greenhouse but expect
assisted, treat Ashby as medium with extra SPA driver work. (All still pending the actual
pass-rate measurement, which needs gated real submits — presence ≠ pass.)

Secondary: this larger run showed **16/16 `form_present`** — the earlier 4/7 wrapper-redirect
rate was token-specific (stripe/databricks proxy GH behind their own sites), not systemic.

## Phase 2 implementation findings (2026-05-28)

Built the **Lever apply driver** + the **universal assisted-apply branch** that both
Greenhouse and Lever now share. Two structural additions:

1. **`av3/sources/browser/apply_base.py`** — extracted the cross-ATS primitives
   (`Applicant`, `CustomQuestion`, `ApplyOutcome`, `human_type`) and a single source for
   the `(dry_run, mode)` dispatch contract. This keeps per-ATS modules to just selectors +
   quirks; adding the next ATS doesn't duplicate dataclasses or the mode logic.

2. **`av3/sources/browser/lever_apply.py`** — drives the canonical
   `jobs.lever.co/<company>/<uuid>/apply` URL. Highlights from the research baked into
   code: name-keyed selectors (`input[name='name']`, etc.), single full-name field, async
   résumé-parse wait (poll `resumeStorageId` for up to 8s before reading custom-Q state so
   we don't race Lever's prefill), `cards[<uuid>]` + `eeo[...]` + pronouns discovery,
   `#btn-submit`, `/thanks` URL signal (already handled by `detect_confirmation`).

**Mode dispatch contract** (now identical in `greenhouse_apply.py` and `lever_apply.py`):

| `(dry_run, mode)` | Behavior | Status set |
|---|---|---|
| `dry_run=True` (default, dev-safe) | Fill + discover, never submit | `None` |
| `dry_run=False, mode=BROWSER_ASSISTED` | Fill + discover, never submit, pre-filled browser handed to human | `ASSISTED_PENDING` |
| `dry_run=False, mode=BROWSER_AUTO` + visible challenge | No submit (project invariant: never retry through CAPTCHA) | `ASSISTED_PENDING` |
| `dry_run=False, mode=BROWSER_AUTO` + no submit button | Fail fast | `FAILED` |
| `dry_run=False, mode=BROWSER_AUTO` + clean form | Submit + run `detect_confirmation` | `APPLIED` / `UNCONFIRMED` / `FAILED` / `ASSISTED_PENDING` per confirmation outcome |

Validation: **86/86 unit tests pass** (`tests_v3/test_lever_apply.py` adds 7 cases covering
dry-run, missing-phone, BROWSER_ASSISTED, visible-challenge downgrade, full auto path with
`/thanks` confirmation, missing-submit fail-fast, and UNCONFIRMED-no-positive-signal;
`test_apply_driver.py` adds the Greenhouse BROWSER_ASSISTED case). No live submits.

**What's still pending in Phase 2** (build order, decreasing priority):
- ~~**Resolver / answer engine for custom questions.**~~ — landed 2026-05-28; see the
  Phase-2 resolver section below.
- **Ashby SPA driver.** Same dispatch shape but no `<form>` and no URL transition — must
  detect submit via the in-place success panel (or hook the `applicationForm.submit` XHR).
  Lower priority than the resolver because Ashby is 50% Enterprise anyway.
- **JobSpy discovery integration** (Indeed/ZipRecruiter) wrapped behind the source-
  capability model — verified working in S3 but not yet wired into `av3/sources/`.
- **Pipeline integration** — none of the drivers actually transition jobs through
  `APPLYING → APPLIED/FAILED/etc.` in the SQLite state machine yet; they return outcomes
  the (not-yet-built) apply worker would translate to state changes.

### Answer resolver landed (2026-05-28, commit pending)

The two-tier resolver from spec §8b/§8d is now wired into both apply drivers. Architecture:

- **`av3/llm/embed.py`** — Ollama `nomic-embed-text` client + cosine helper +
  `float32` BLOB codec for `answers.embedding`. No numpy dependency — bank is bounded
  (dozens-low-hundreds), a Python loop is fine and one less wheel to ship.
- **`av3/llm/complete.py`** — Ollama-JSON → Gemini-JSON → raises. Tier-3 backup only.
- **`av3/resume/answer_resolver.py`** — the resolver itself:
  - **Tier 0 (sensitive, §8d).** Regex-classifies the label → EEO / work-auth /
    sponsorship / salary. EEO: user self-ID or "Prefer not to answer" (still a valid
    submission, not a REVIEW). Work-auth + sponsorship: explicit fact-bank field, REVIEW
    if blank (no silent default — explicitly retires v2's "authorized=Yes" from
    [[project_us_default_assumption]]). Salary: v3.0 fills user-configured expectation,
    intelligence-layer deferred to v3.1.
  - **Tier 1 (bank).** Exact question-text match first (skips embedding round-trip —
    cheap path for users who seeded v2's flat `answers.json`). Then semantic match:
    embed the question, cosine vs stored vectors, hit on ≥ 0.78.
  - **Tier 2 (LLM).** Bank misses → fact-bank + question goes to the LLM, which returns
    `{answer, confidence}`. Above 0.7 → submit, flagged as `inferred` (feeds the §8e
    promotion loop). Below → REVIEW.
- **`av3/sources/browser/apply_base.py`** — added `fill_resolutions()` and
  `any_required_unresolved()` as cross-ATS primitives. Both drivers now accept an
  optional `resolver` param; when supplied they resolve each discovered question, type
  text answers via `human_type` (preserves the behavioral signal), select-option for
  selects, and **downgrade `BROWSER_AUTO` to `ASSISTED_PENDING` if any *required*
  question came back as REVIEW** (never auto-submit a form with missing required
  answers).
- **`av3/resume/seed_answers.py`** — one-shot importer for v2's `data/answers.json`.
  Idempotent (UPSERT keyed by question text); computes embeddings if an embedding
  client is available.

Tuning operating points (conservative, fail-closed):

| Knob | Value | Why |
|---|---|---|
| `semantic_match_threshold` | 0.78 | High enough that "what's your favorite color?" doesn't get answered with a SQL-years string; low enough that genuine paraphrases ("Years of SQL experience" ≡ "How many years of SQL?") hit. Tunable per §10. |
| `llm_confidence_threshold` | 0.70 | Mirrors the v2 score-band lessons — borderline LLM proposals route to REVIEW rather than risk a confidently-wrong answer that submits on auto. |

**Validation: 124/124 unit tests pass** (38 new): full resolver coverage (sensitive
classification matrix; work-auth bail-to-review when bank blank; sponsorship
true/false/None; EEO with and without self-ID; salary user-config and missing-config;
exact + semantic + below-threshold bank paths; LLM high/low/unavailable/malformed
replies; batch order preservation; cosine + codec sanity; v2 seeder idempotency +
missing-file no-op). Driver wiring tests in both `test_apply_driver.py` and
`test_lever_apply.py` verify that resolved answers reach the right DOM element (typed
text for input/textarea, `select_option` for select), and that a required-Q REVIEW
downgrades `BROWSER_AUTO` to `ASSISTED_PENDING` *before* CAPTCHA / submit-button
checks. No live submits.

**What this unblocks.** The auto path can now submit forms with custom questions
when the bank covers them (most common ones — work-auth, years-of-X — are seeded from
v2's `answers.json` on Day-1 of onboarding). The assisted path becomes legitimately
*assisted*: the human reviews pre-filled answers rather than typing them from scratch.

**What still needs wiring.** A real apply worker that constructs the resolver
(needs the fact bank + AnswerRepo + EmbeddingClient + CompletionClient) and threads
it into `prepare_application(..., resolver=...)`. That worker also owns the
`APPLYING → APPLIED/FAILED/...` state transitions, which the drivers still don't
emit themselves (they just return `ApplyOutcome`). Both fall under the next
Phase-2 piece: the apply worker.

## Apply worker landed (2026-05-28, commit `e17f92f`)

Phase 2 (3/N) — `av3/pipeline/apply_worker.py`. The worker is the drain side of the
`QUEUED_APPLY` queue and the spec §7 #7 implementation. It is the smallest distance
to a real end-to-end pipeline and the prerequisite for the first live Lever smoke test.

What it does (per spec §7 + the per-ATS findings above):

- **Constructs the resolver once per run** from injected `FactBank` + `AnswerRepo`
  (+ optional `EmbeddingClient` / `CompletionClient`). Both LLM clients are optional —
  exact-text bank matches + sensitive-field policy still work with neither installed,
  so Ollama is never a hard dependency of the worker.
- **Drains `QUEUED_APPLY` jobs** via `JobRepo.list_by_state(..., limit=)`, dispatching
  by `Job.source` through a `DriverEntry` registry (`lever` → `lever_apply`, `greenhouse`
  → `greenhouse_apply`). Unknown sources are silently skipped (no state change). The
  registry is injectable so worker tests stub the driver entirely instead of dragging
  in `FakePage` — keeps the worker's contract tests focused on what the worker actually
  owns (state, isolation, telemetry).
- **Translates `ApplyOutcome.status` → `JobState`** via the strict state machine:
  - `APPLIED` → `APPLYING → APPLIED` (terminal; dedup source of truth).
  - `ASSISTED_PENDING` → `APPLYING → REVIEW` (a deliberate human handoff, NOT a
    failure; the §5 state-machine docstring already noted that going via FAILED
    "would muddy the event spine"; the edge was added in this commit).
  - `UNCONFIRMED` / `FAILED` → `APPLYING → FAILED → REVIEW` (spec §5 wording —
    "no confirmation / mid-form break → FAILED → REVIEW"). Dedup keys off APPLIED,
    so an UNCONFIRMED attempt is safely retryable.
  - `dry_run=True` skips the APPLYING transition entirely — no state ping-pong in
    the event log for dev/test runs.
- **Per-job error isolation** (matches v2's hard-won pattern from
  `orchestrator/engine.py`): driver exceptions are caught at the `run_once` level;
  the job's state is recovered (`APPLYING → FAILED → REVIEW`) and a `FAILED`
  application row is written so the dashboard's "what happened to this job?" view has
  the attempt recorded.
- **Per-company/day rate limit** (spec §7 re-apply policy): silent skip when
  `JobRepo.company_applied_count(company) >= settings.pacing.max_per_company_per_day`.
  The job stays in `QUEUED_APPLY` so the next cycle picks it up tomorrow.
- **Pacing** (spec §8a v3.0 fixed): random `uniform(min_delay_s, max_delay_s)`
  between *successful* applies. Skips (rate-limit, unknown source) don't burn a
  delay slot.
- **Inferred-resolution telemetry mirror** (spec §8b iteration loop / §9): one
  `resolver_inferred` event per `ResolutionSource.INFERRED` answer, carrying
  `{question, category, confidence, outcome}` only. The answer value never enters
  the mirrored payload. **EEO resolutions never mirror at all**, even when inferred
  (§8d — EEO self-ID stays 100% local).
- Writes an `Application` row for every real (non-dry-run) attempt with `mode`,
  `status`, `generated_resume_path`, and `submitted_at` populated on positive submit.

### State machine addition (`av3/domain/state.py`)

Added `JobState.REVIEW` to `APPLYING`'s allowed-targets set. Rationale in the
docstring: ASSISTED_PENDING is a deliberate handoff, and going through `FAILED`
muddies the event spine. The crash-sweep (`APPLYING → QUEUED_APPLY`),
positive-confirm (`APPLYING → APPLIED`), and mid-form-break (`APPLYING → FAILED →
REVIEW`) paths are unchanged.

### Operating defaults

| Knob | Default | Why |
|---|---|---|
| `mode` | `BROWSER_AUTO` | Spec §7a — bias toward auto where safe; the drivers' own §8b downgrade catches required-Q REVIEW + visible challenges. |
| `dry_run` | `True` | Dev-safe default. CLI flips it to `False` for real apply runs. |
| `pacing.min_delay_s` / `max_delay_s` | 60 / 180 (settings default) | v3.0 fixed pacing; strategy profiles land in v3.1. |
| `pacing.max_per_company_per_day` | 2 (settings default) | Re-apply rate limit (spec §7 — never look spammy to one employer). |

### Validation: 147/147 v3 tests pass (+23)

Worker tests (22 cases) cover all four `ApplyOutcome.status` outcomes, dry-run
state preservation, per-company rate-limit silent skip, unknown-source skip,
single-driver exception isolation across multiple jobs, pacing on success/skip
boundaries, `--limit` honoring, resolver construction with + without LLM/embed
clients, applicant build from fact-bank contact, and the §8b/§8d/§9 telemetry
policy (INFERRED-only mirror, EEO drop, bank-hit silence). Plus one
state-machine test asserting the new `APPLYING → REVIEW` edge.

### What still needs wiring

- **First live Lever smoketest.** The worker's first real submit is a gated user
  decision (sends a real application). The CLI entry now exists (see "CLI apply
  wired" below); the smoketest itself is still on the user. Lever was chosen as
  the field-validated auto-viable target per the n=16 survey above — start there,
  not Greenhouse.
- **Ashby SPA driver.** Same dispatch shape as Lever/GH but no `<form>`, XHR
  submit, in-place success panel. `detect_confirmation` already has the design
  notes (§Ashby above). Once that lands the worker only needs the registry entry.
- **Phase-3 staged worker scheduler.** This worker handles the apply *step*; the
  surrounding pipeline (discover/score/optimize as background workers) is still
  Phase 3. For now, a CLI invocation drives `worker.run_once()` once per cycle.
- **Crash-sweep on startup.** Spec §5 mandates re-queueing jobs left in `APPLYING`
  from a crashed prior run — owed at worker-service boot, not implemented yet
  (the edge `APPLYING → QUEUED_APPLY` exists for it; just no caller).

## CLI apply wired (2026-05-29, Phase 2 (4/N))

### What landed

`av3 apply` (in `av3/cli/main.py`) is the smallest reachable wrapper around
`ApplyWorker.run_once()`. It is the entry point for the gated first live Lever
smoketest that closes Phase 2's auto-apply thesis.

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--once` | `True` | Run one cycle and exit. Only mode v3.0 ships; placeholder for the Phase-3 staged scheduler. |
| `--limit N` | unbounded | Max QUEUED_APPLY jobs per run. Threaded directly into `worker.run_once(limit=...)`. |
| `--source lever\|greenhouse` | both | Subsets the driver registry (cleaner than per-job filtering — the worker already short-circuits unknown sources). |
| `--dry-run / --no-dry-run` | `--dry-run` | Dev-safe default. `--no-dry-run` SENDS REAL APPLICATIONS; CLI prints a loud confirmation line first. |
| `--mode auto\|assisted` | `auto` | `BROWSER_AUTO` vs `BROWSER_ASSISTED`. Only consulted when `dry_run=False`. |
| `--no-llm` | `False` | Skip Ollama/Gemini construction. Resolver still works for exact-bank + sensitive policy. |

### Pre-flight (doctor-style)

Two fail-fast checks with `fix ->` hints:

- `data_dir / "profile" / "master.json"` MUST exist (fact bank).
- `data_dir / "artifacts" / "resume.pdf"` MUST exist (resume PDF placeholder
  until §6b résumé generation lands).

Failure exits 2 with the doctor's `x FAIL` / `fix ->` format, never crashes
mid-run.

### Wire-up shape

```
load_settings -> pre-flight (bank + resume)
              -> FactBank.load(master.json)
              -> init_app_db(app_db_path)
              -> configure_sink(EventSink(events_db_path))
              -> drivers = default_drivers()  [or {source: ...}]
              -> embed = OllamaEmbeddings(...) | None
              -> llm   = build_default(settings) | None
              -> asyncio.run(_run()):
                   BrowserSession.start() -> ApplyWorker(...) -> worker.run_once(limit) -> BrowserSession.stop()
              -> ASCII summary line + Notes block
              -> exit 1 if summary.errors else 0
```

`BrowserSession`'s `new_page` coroutine is the only callable the worker holds
on to from the browser layer — the worker stays Playwright-free.

### Summary line + exit code

ASCII-only one-liner (Windows cp1252 safety):
`run_id=<id> attempted=N applied=N review=N skipped=N errors=N dry_run=N elapsed=N.Ns`,
optionally followed by a `Notes:` block of any human-readable lines the worker
recorded (rate-limit skips, unknown sources). Exit code is `1` when any per-job
exception fired (`summary.errors > 0`) so cron/CI can gate; `0` otherwise; `2`
on pre-flight failure.

### Validation: 162/162 v3 tests pass (+15)

`tests_v3/test_cli_apply.py` (15 cases) covers: both pre-flight failure modes
with `fix ->` hint visible; summary-line shape on clean runs; exit code `1` on
errors; `Notes:` block printing; `--dry-run` default and `--no-dry-run` flip
(plus its loud confirmation line); `--mode auto|assisted` → `ApplyMode` enum
mapping; `--source` subsetting the driver registry; default registry exposes
both sources; `--limit` threading into `run_once`; `--no-llm` zeroing both LLM
clients; default wiring both LLM clients; session `start`/`stop` lifecycle;
fact-bank threading. Stubs `BrowserSession` + `ApplyWorker` so no live browser
runs (worker behavior is owned by `test_apply_worker.py`).

### What still needs the user

The first live Lever smoketest is the gated decision the spec's Phase-1
"open" item names. CLI is ready:

```
av3 apply --once --source lever --limit 1 --dry-run    # confirm form fills cleanly
av3 apply --once --source lever --limit 1 --no-dry-run --mode auto    # REAL SUBMIT
```

Junk-target Lever posting required. Record the outcome (confirmation detection,
`resolver_inferred` rows in `events.db`, post-submit `JobState`) in this doc
as the live-pass data point we've been chasing.

## Crash-sweep landed (2026-05-29, Phase 2 (5/N))

Spec §5 mandate satisfied. `ApplyWorker.recover_crashed()` walks
`JobRepo.list_by_state(APPLYING)` and routes each leftover via
`APPLYING → QUEUED_APPLY` (the edge already existed; just had no caller).
`run_once()` calls it BEFORE pulling the queue so the recovered jobs flow
through the same cycle — otherwise they'd sit out another full one. The count
lands in `summary.recovered` and produces one human-readable Notes line.

`recover_crashed()` is sync and never touches the driver path, so an
operational tool ("doctor" check, future `av3 recover` command) can call it
without booting a `BrowserSession`. CLI summary line now reads
`... errors=N recovered=N dry_run=N elapsed=N.Ns`.

Validation: 5 new tests in `tests_v3/test_apply_worker.py` cover the bare
recovery method, idempotency (zero APPLYING → zero work), auto-run inside
`run_once`, clean-queue stays at recovered=0 with no spurious notes, and the
no-browser-required guarantee (proven with a bombing driver stub).

## Ashby SPA driver landed (2026-05-29, Phase 2 (6/N))

`av3/sources/browser/ashby_apply.py` mirrors the Lever/GH dispatch shape with
React-SPA-specific quirks:

- **Form-ready wait.** Until `#_systemfield_name` is in the DOM the SPA hasn't
  hydrated; reading custom-Q state too early returns nothing. `_wait_for_form_ready`
  polls via `page.wait_for_selector` (8 s budget); the FakePage stub omits it so
  unit tests stay cheap.
- **Three system fields only.** Name (full name → `#_systemfield_name`), email
  (`#_systemfield_email`), résumé (`#_systemfield_resume`). Phone is **not** a
  standard field — per-form it's UUID-named — so it flows through custom-Q
  discovery + the resolver like every other question.
- **Custom-Q discovery by exclusion.** Walk every input/textarea/select whose
  id/name does NOT start with `_systemfield_` (and isn't a `g-recaptcha-response` /
  `-response` carrier). Pair each with its `<label>` or
  `.ashby-application-form-question-title` heading. UUID-named fields land in
  `CustomQuestion.field_id` and the shared `_selector_for` picks the id-keyed
  selector `#<uuid>` for filling.
- **No URL transition on success.** The XHR submit (`api.ashbyhq.com/applicationForm.submit`)
  doesn't redirect; the React app swaps the form for an in-place "Application
  submitted" panel. `detect_confirmation`'s existing `_SUCCESS_TEXT_RE` matches
  it — no per-ATS branch needed there.
- **Submit selector** `button[type=submit]` (the button is outside the (non-existent)
  `<form>` but Ashby still sets `type=submit`). Click → React's onClick handler
  fires the XHR.

Registered in `ApplyWorker.default_drivers()` as `"ashby"`; CLI `--source` now
accepts `ashby` alongside lever/greenhouse. Validation: 10 new tests in
`tests_v3/test_ashby_apply.py` cover dry-run + assisted + auto-submit + visible
challenge downgrade + UNCONFIRMED + UUID resolver wiring + required-Q downgrade +
the discover-by-exclusion filter.

**Known limitation deferred to Phase 3:** knockout questions render as Yes/No
buttons (not radios) — the current discover walk skips them (no native input
element). Required knockouts will trigger the §8b downgrade to ASSISTED_PENDING
through the unresolved-required path. Resolver + driver extensions to handle
button-enums are tracked as Phase-3 work, not Phase-2-blocking.

## JobSpy discovery wired (2026-05-29, Phase 2 (7/N))

`av3/sources/jobspy.py` wraps `python-jobspy` behind the v3 source-capability
shape (mirrors `LeverSource` / `AshbySource`). `JobSpyQuery` dataclass +
`JobSpySource.discover(query)` returns a list of `JobSpyListing` (same field
shape as the other listings). Discovery-only — no apply path; browser-board
applying is a Phase-3 capability.

Design decisions:

- **Per-row `source` = JobSpy's `site` column** (`indeed` / `zip_recruiter` /
  ...), NOT the wrapper's organizational `source_name = "jobspy"`. This way
  cross-source dedup works correctly and the apply worker's unknown-source
  skip keeps browser-board jobs out of any auto-apply path until a Phase-3
  driver lands.
- **Default sites = Indeed + ZipRecruiter only.** LinkedIn is in JobSpy but is
  intentionally excluded — TLS fingerprinting defeats stealth even through
  scrapers ([[project_linkedin_research_2026-04-14]]).
- **Stable source_job_id.** Newer JobSpy emits an `id`; older versions don't.
  Falls back to `sha256(job_url)[:16]` so re-running the same query dedups
  correctly against `(source, source_job_id)`.
- **NaN-tolerant.** Pandas uses NaN for missing cells; `_row_value` treats NaN
  as missing (the `val != val` trick avoids depending on numpy).
- **Best-effort.** Scraper exceptions → `[]` (never crashes the discovery
  pipeline); a row with neither id nor job_url is dropped (nothing stable to
  dedup on).
- **Optional install.** `pip install -e ".[v3,jobspy]"` — pulls pandas as a
  transitive dep, hence opt-in. Lazy-imported; absence raises a clear
  `ImportError` with the install command.

Validation: 13 new tests in `tests_v3/test_jobspy_source.py` cover the happy
path, kwargs threading, default-site exclusion, hours-old omission, stable-ID
fallback, NaN tolerance, empty results, scraper exceptions, lazy-import error,
multi-site per-row source assignment, and DataFrame-to-records conversion.
Pandas is NOT required for the tests (fakes return plain dicts).

## Sources

- [Greenhouse — Invisible reCAPTCHA](https://support.greenhouse.io/hc/en-us/articles/115005448066-Invisible-reCAPTCHA)
- [Greenhouse — Edit application confirmation page](https://support.greenhouse.io/hc/en-us/articles/115005516483-Edit-application-confirmation-page)
- [Greenhouse — Embed a job board (iframe)](https://support.greenhouse.io/hc/en-us/articles/46365908766875-Embed-a-Greenhouse-job-board-on-your-career-site)
- [Greenhouse — Choose a careers page integration option](https://support.greenhouse.io/hc/en-us/articles/200721644-Choose-a-careers-page-integration-option)
- [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html)
- [JobWizard — How to autofill Greenhouse applications](https://jobwizard.ai/blog/how-to-autofill-greenhouse-job-applications-with-jobwizard)
- [Lever — Configuring your application form](https://help.lever.co/hc/en-us/articles/20087243347741-Configuring-your-Lever-application-form)
- [Lever — Adding custom application questions](https://help.lever.co/hc/en-us/articles/20087327834397-Adding-custom-application-questions-to-job-postings)
- [Lever — Automation workflow recipes (confirmation email)](https://help.lever.co/hc/en-us/articles/20087246036893-Automation-workflow-recipes)
- [Ashby — applicationForm.submit (developer reference)](https://developers.ashbyhq.com/reference/applicationformsubmit)
- [Ashby — Application Forms (knowledge base)](https://docs.ashbyhq.com/application-forms)
- [Ashby — Creating a custom careers page (SPA / iframe)](https://developers.ashbyhq.com/docs/creating-a-custom-careers-page)
- [Ashby — Job board embed example (application form only)](https://www.ashbyhq.com/job-board-embed-examples/application-form-only)
- [Zapply — Hacking Greenhouse and Lever (reCAPTCHA Enterprise, proxies, email-code)](https://vanja.io/zapply-hacking-greenhouse-and-lever/)
- [scale.jobs — Why iCIMS applications break most automation tools (ATS comparison)](https://scale.jobs/blog/icims-applications-break-most-automation-tools)
- [simonfong6/auto-apply — Selenium bot for Greenhouse/Lever/Workday/Jobvite](https://github.com/simonfong6/auto-apply)
- [LifeShack — Auto-apply on Ashby](https://www.lifeshack.com/job-board/ashbyhq/)
- [LoopCV — Ashby application status explained](https://www.loopcv.pro/guides/ashby-application-status/)

Live DOM inspected May 2026: `job-boards.greenhouse.io/anthropic`, `jobs.lever.co/voltus`,
`jobs.ashbyhq.com/ramp`.


---

## First full E2E dry-run validation (2026-06-11) — findings + fixes

First end-to-end live exercise of the whole apply chain (discover -> filter -> score ->
optimize -> apply --dry-run) against real boards (gitlab/cloudflare GH, highspot Lever,
Vanta/Linear Ashby) with the real fact bank + qwen3:8b in an isolated AV3_DATA_DIR.
All 10 live smoke tests passed first (zero selector drift since 2026-05-26).

**Stage results:** discover 468 seen -> 3 matched; filter ran with REAL embeddings for the
first time (2 passed / 1 filtered, no fail-open) after pulling nomic-embed-text; score
2 DECIDED; optimize generated a guard-passed resume + cover (one full pass); apply
dry-run filled the live GitLab form end-to-end with zero errors on the final run.

**Bugs found live -> fixed (each has a regression test):**

1. **Embed client 10s timeout** (llm/embed.py): the FIRST call after Ollama swaps
   models pays a >10s cold load -> ReadTimeout -> the filter failed open on every run
   (the long-standing "no embed/bank" known-issue was never just a missing model —
   the timeout guaranteed failure even with the model present). Now 60s, and {exc!r}
   so ReadTimeout doesn't render as an empty reason.
2. **Completion 60s timeout** (llm/complete.py): resume/cover generation on an 8B
   model can exceed 60s -> spurious fail-closed to REVIEW. Now 180s.
3. **UNGUARDED COVER LETTER (the big one):** qwen3:8b wrote a Kubernetes/Terraform/
   ArgoCD cover letter for a SQL Server DBA and it reached QUEUED_APPLY — guard_l1
   vets only the structured resume. New vet_cover_letter (resume/guard.py):
   symmetric deterministic extraction (the reconcile vocabulary) over the letter vs
   the bank corpus; unsupported tech claims -> REVIEW. Wired as gate 3b in the
   optimize worker. Caught the live fabrication on re-run.
4. **react-select menu blocks later fields:** the NEW job-boards.greenhouse.io layout
   renders dropdown questions as comboboxes; typing opened a "No options" menu that
   intercepted pointer events over every later field (30s timeout each, job-level
   error). Fixes in apply_base: human_type click bounded to 8s + returns False on
   intercept; new settle_open_dropdown commits the matching option or presses
   Escape after each custom-question fill.
5. **Question double-discovery on the new GH layout:** each combo question = a visible
   combobox input + a hidden value carrier with distinct ids but the same label ->
   every screener was LLM-resolved and typed TWICE. Label-level dedup added to the
   GH discoverer (empty labels never deduped).
6. **Tier-3 resolver overclaim trap (the most important):** the resolver gated on the
   model's SELF-reported confidence — qwen3:8b reported 0.95 on "production
   Kubernetes/Go experience?" judgment calls whose honest answer is No. Tier-3 now
   routes through the section-8f copilot: the deterministic evidence audit must pass
   (yes/partial cites bank facts that check out, else bail to REVIEW), confidence is
   structural (0.9 clean / 0.75 self-flagged-low), and honest "No" fills legitimately.
   Verified live: the Kubernetes screener now resolves to an honest No.

**Still untaken (deliberately):** a real --no-dry-run submission. That remains the
gated user decision; this validation makes it a much safer one.

**Operational note:** nomic-embed-text is now installed in Ollama — the personal
search's daily refresh will start ACTUALLY pre-filtering by embedding relevance
(cosine >= 0.6 vs the profile) instead of failing open. Removing that model restores
the old fail-open behavior if ever preferred.

## Dress-rehearsal #2 quirks (2026-06-12) — phone widget + react-select attestation

Reading the resolver events from the first 5-job Solutions dry-run surfaced two more
ATS-form realities (fixes in `apply_base.py` / `answer_resolver.py`, tests green):

7. **Phone is intl-tel-input with a GLOBE default (no country selected).** On the current
   Greenhouse layout `#phone` is wrapped in `.iti--allow-dropdown` and the default flag is
   `iti__flag iti__globe` — NOT US. Typing a national number (`1-682-718-8130`) leaves it
   with no dial code → invalid. Fix: `normalize_phone()` types `+E.164` (`+16827188130`);
   intl-tel-input auto-selects the country from the `+` prefix (verified live: flag → US,
   value → `+1 682-718-8130`). Never drive its country dropdown (it's a search dialog).
   Wired into Greenhouse + Lever drivers.
8. **The human-attestation gate is a react-select with a non-descriptive label.** Grafana's
   `"Which of the following best describes you?*"` (options "I am an AI…" / "I am a human
   being") renders as react-select — options are NOT in the DOM until the menu opens, so
   option-pair detection can't see them, and the bare label matches no attestation pattern.
   The copilot answered it (conf 0.75). Fix: a `"…best describes you"` label rule (EEO-guarded)
   + a value-side `affirms_human()` backstop in `fill_resolutions` that refuses to TYPE any
   human affirmation. See research/automated-apply-go-live.md (blocker A) for the full story.
