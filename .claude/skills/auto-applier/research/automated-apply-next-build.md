# Automated Apply — Next Build Handoff

> Written 2026-06-13, after the field-fill overhaul (commit **20b3731**, 1004 tests green).
> Read `automated-apply-go-live.md` first for the full backstory; this is the forward plan.
>
> **UPDATE 2026-06-13 — BUILD 1, 1.1, 2, 3 all resolved. 1029 tests green.**
> - BUILD 1 (cover-letter upload) SHIPPED + validated live (Tailscale). Merged to master + pushed.
> - BUILD 1.1 (per-job cover model) REPLACED the company-index: per-posting letters via `av3 cover`
>   → `artifacts/uploads/<job_id>/Cover Letter<ext>` (generic upload basename = anti-detection),
>   archived on confirmed APPLIED. Company-index machinery removed. See "BUILD 1.1".
> - BUILD 2 (enumerated react-selects) CLOSED as no-fix-needed: the bails are honest RESOLVER
>   decisions, not a filler miss (proven via the resolver-detail dump). See "BUILD 2 RESULT".
> - BUILD 3 (veteran_status) FIXED: a `_DECLINE_SYNONYMS` contraction gap ("don't"), not a flake.
> Left before go-live: the gated watched `--no-dry-run`; per-job `av3 cover` assignment for the jobs
> you queue; (follow-up) extend the generic-filename model to the résumé.

## Where we are

The apply driver now fills the **new `job-boards.greenhouse.io` React layout** correctly.
Verified live on Grafana via the diagnostic: **17/19 fields fill**, all required ones commit.
Discovery walks every control (not just `question_*`), the resolver handles essays (bank-only,
no LLM invention), profile/contact fields, work-auth/sponsorship, EEO, attestation + consent
bails, and a `fill_combobox` commits react-selects (Yes/No, country, city autocomplete, EEO
prefer-not). Phone is correct (inline iti `+1 …`; country residence field shows the "+1" badge
but submits United States — both confirmed correct, NOT a bug).

**No real `--no-dry-run` submission has happened yet.** That remains the gated, watched decision.

## The validation path (USE THIS — not screenshots)

`JobSearch/diagnose_apply.py <greenhouse_apply_url>` runs the REAL driver discovery + REAL
resolver against a live form and prints, per field: kind, discovered?, resolution source,
fills, and the value that LANDED on the page (read-back, incl. react-select `.select__single-value`).
No submit. This is how every fix above was verified. Also: a real `av3 apply --dry-run` now writes
a local `resolution` event per field to `events.db` (`stage='resolution'`, metadata only, never the
answer value, NOT mirrored) — query it to audit any run:
```
SELECT context_json FROM events WHERE run_id=? AND stage='resolution'
```
Set `AV3_DATA_DIR=C:\Users\jar85\JobSearch\av3data` for the personal search.

## BUILD 1 — Cover-letter upload — DONE (2026-06-13)

Shipped. The bot now attaches a cover letter on Greenhouse. What landed:

**Shared helper** — `apply_base.attach_cover_letter(page, selector, path) -> bool`: defensive
native-file-input upload. Empty path → short-circuit False (no DOM query); absent input or upload
error → False (observable, never fatal — a cover letter is supplementary).

**Greenhouse driver** (`greenhouse_apply.py`) — `_COVER_LETTER_SELECTOR = "#cover_letter"`, a new
`cover_letter_path: str = ""` kwarg, and an `attach_cover_letter` call right after the résumé
attach (records `outcome.filled["cover_letter"]`). DOM confirmed live on Hightouch: hidden
`#cover_letter` (`class="visually-hidden"`, `accept=".pdf,.doc,.docx,.txt,.rtf"`) parallel to
`#resume`; `set_input_files` works directly, no Attach-click. `.docx` uploads as-is (no PDF render).

**Lever + Ashby** — accept the `cover_letter_path` kwarg (so the worker calls every driver
uniformly) but DO NOT wire the upload yet: Lever commonly uses a single combined upload; Ashby's
cover field is often a UUID-named custom question. Scope each on a live form before wiring (their
docstrings say so). Greenhouse was the whole batch, so it was wired first.

**Manual cover-letter library — ⚠️ SUPERSEDED by BUILD 1.1 (below).** The first cut resolved a
letter by `job.company` (a slug-keyed `index.json` in `cover_letters_dir`). That model couldn't
disambiguate companies with multiple per-posting letters, so it was REPLACED by the per-job model
(`av3 cover` + `artifacts/uploads/<job_id>/Cover Letter<ext>`). `cover_letters_dir`,
`manual_cover_letter_path`, `_cover_slug`, and the JobSearch `index.json` were all removed. The
worker (`_artifacts_for`) now reads `existing_job_cover(settings, job.id)` (manual, generic-named) →
optimize `.txt` → `""`; **no per-company fallback**. See BUILD 1.1 for the full design.

**Tests** — `tests/test_cover_library.py` (per-job assign/lookup/archive + the attach helper),
`tests/test_cli_cover.py` (`av3 cover`), driver-attach tests in `test_apply_driver.py`, worker
per-job + archive-on-APPLIED tests in `test_apply_worker.py`. All driver stubs (incl.
`test_session_expiry`) accept the `cover_letter_path` kwarg.

**Validate live (still a user-watched step):** `diagnose_apply.py` attaches a throwaway file and
asserts the hidden `#cover_letter` input receives it (the input vanishes post-attach — it reads the
visible filename instead; see the post-attach DOM swap below). Run watched on a real Greenhouse form
to confirm the on-page "Cover Letter" section shows the attached filename (the user's Image #2 showed
it empty):
```
$env:AV3_DATA_DIR='C:\Users\jar85\JobSearch\av3data'; $env:PYTHONIOENCODING='utf-8'
python C:\Users\jar85\JobSearch\diagnose_apply.py https://job-boards.greenhouse.io/<token>/jobs/<id> <Company>
```

**VALIDATED LIVE 2026-06-13 on Tailscale** (`/tailscale/jobs/4704631005`): the upload works —
`filled['cover_letter']=True` and the on-page "Cover Letter" section shows the attached filename.
Three findings to know:

1. **Post-attach DOM swap (important).** After `set_input_files('#cover_letter', …)` fires react's
   onChange, Greenhouse **removes the bare `#cover_letter` input** and renders the attached filename in
   the Cover Letter section. So you CANNOT verify by re-reading `#cover_letter.files` (the element is
   gone — reads as -1). Verify by the **visible filename in the section** instead (the diagnostic's
   read-back was fixed to do this). The attach itself is unaffected — `filled['cover_letter']` is the
   true signal, captured at attach time.
2. **Cover Letter can be REQUIRED** (`Cover Letter*` on Tailscale). So a queued job at a company whose
   letter ISN'T in the index → no attach → on a real `--no-dry-run` the required-field validation
   correctly routes it to assisted/REVIEW (never submits blank). Another reason to map the multi-variant
   companies before going live on them.
3. **`#cover_letter` presence varies per company** (live survey of 10 boards): Postman, Adyen,
   SingleStore, Tailscale, Celonis HAVE it; Monzo, Algolia do NOT (form renders, no cover field — the
   defensive helper no-ops). And several companies **redirect `job-boards.greenhouse.io/<token>/jobs/<id>`
   to their OWN careers host** (SumUp→sumup.com, Fivetran→fivetran.com, Databricks→databricks.com) — no
   Greenhouse form at that URL at all (0 controls). Those jobs can't auto-apply via the greenhouse driver;
   a discovery/routing follow-up (detect the redirect, mark for assisted) is worth a future ticket.

## BUILD 1.1 — Per-job cover-letter architecture (SUPERSEDES the BUILD 1 company index)

**Why this replaces the first cut.** BUILD 1's first commit resolved a cover letter by
`job.company` (a slug-keyed `index.json` in `cover_letters_dir`). That's the WRONG model: the
letters are hand-authored **per posting** (role/region/tier vary), so a per-company default can't
disambiguate Databricks ×5, Grafana ×3, etc. The user flagged it: *"the method was write one per job,
not pick a global company one."* Correct — go pure per-job, no global defaults.

**The anti-detection insight (the real reason it matters).** Playwright `set_input_files(path)` sends
the file under its **basename**. A file literally named `CoverLetter_Tailscale_SE_Commercial.docx` is a
fingerprint — it reveals templated, per-company mass-applying. A normal applicant uploads
`Cover Letter.docx`. So the upload filename must be **generic**, and the per-posting identity must live
in the folder path / archive, never in the uploaded basename.

**Storage contract (file-existence is the contract, like the optimize artifacts):**
- Live, ready-to-upload: `artifacts/uploads/<job_id>/Cover Letter<ext>` — folder keyed by job id,
  **generic basename**. Content is the user's hand-written letter; only the filename is normalized.
- Archived after confirmed use: `artifacts/uploads/_archive/Cover Letter - <job_id><ext>` — job id
  appended so the archive is identifiable; the *live* upload never carries it.

**Commands:**
- `av3 cover <job_id> <source_letter>` — copies the chosen letter into the job folder as
  `Cover Letter<ext>` (order-independent with `av3 queue`; this is the per-job "write one per job"
  step). Overwrites an existing assignment for that job. (Auto-pipeline analog: the optimize stage
  already writes a per-job cover after scoring — same per-job contract, different producer.)

**Worker (`ApplyWorker._artifacts_for` + `_process_one`):**
- Cover source order: per-job `artifacts/uploads/<job_id>/Cover Letter.*` (manual, generic) →
  optimize `{job_id}_cover.txt` → `""`. **No company fallback** (removed `cover_letters_dir`,
  `manual_cover_letter_path`, the index). Pass the resolved path to `driver.prepare(cover_letter_path=…)`.
- On a **confirmed `APPLIED`** (real run only): move the live file to
  `artifacts/uploads/_archive/Cover Letter - <job_id><ext>` and record THAT path on the `Application`
  row (the row points at the kept file). Non-APPLIED (assisted/review) leaves the file in the job folder
  — it isn't "confirmed used" yet, and assisted still needs it available.

**Follow-up (not in this build):** the RÉSUMÉ has the identical filename tell (`{job_id}.pdf` /
`Joseph_Lira_Resume_Solutions_Engineer.pdf`). The same per-job-folder + generic-basename pattern should
extend to it (e.g. `artifacts/uploads/<job_id>/Resume<ext>` or a real-name basename). Scoped out here to
keep the résumé path stable; do it next with the same mechanism.

## BUILD 2 — Verify the Tailscale enumerated react-selects

The `fill_combobox` committer was validated on Grafana (Yes/No, country, city, EEO). The
Tailscale form (Image #6) has ranged/enumerated dropdowns the user flagged: **years-of-experience**,
**SaaS yes/no**, **"have you used Tailscale before?"**. These resolve via the copilot (inferred);
the answer must match an option to commit. Run:
```
python JobSearch/diagnose_apply.py https://job-boards.greenhouse.io/tailscale/jobs/4663656005
```
Check each enumerated field's `ON_PAGE` read-back. If a copilot prose answer ("No, I haven't…")
doesn't match a Yes/No/range option → it bails blank (SAFE: required+blank → assisted). To make
them commit, have the copilot/resolver return option-canonical short values for combobox questions
(e.g. "No" not "No, I haven't…"), or have `fill_combobox` extract a yes/no/leading-number from the
value. "Why are you interested in joining Tailscale?" correctly bails (company-specific) and the
privacy consent correctly bails — leave those.

**CONFIRMED LIVE 2026-06-13** (`/tailscale/jobs/4704631005`, 14 custom Qs, 13/19 filled). The job ID
in the command above (`4663656005`) is stale — use `4704631005` or any live Tailscale posting. What
committed vs bailed:
- ✅ commit: sponsorship `[committed] No`, "hands-on experience designing/deploying…" `[committed] No`,
  LinkedIn + Website (GitHub) typed, Gender/Hispanic `[committed] Decline To Self Identify`,
  Disability `[committed] I do not want to answer`, Country `[committed] +1` (cosmetic dial-code badge,
  submits US — known, not a bug).
- ⛔ bail-blank (the BUILD 2 targets): "located in / willing to relocate to Singapore?" (Y/N),
  "How many years of experience…" (`question_…[]`, a RANGE select), "have you used Tailscale before?"
  — all `[ctrl-text] Select...`. These are the enumerated react-selects whose inferred prose answer
  doesn't match an option. **This is the work:** option-canonical short values (or a yes/no/number
  extractor in `fill_combobox`).
- ✅ correct bail (leave): "Why are you interested in joining Tailscale?" (textarea, company-specific),
  the "I have read and understand…Candidate Privacy…" consent (CONSENT class).
- ⚠️ `veteran_status` bailed blank again here — turned out NOT to be a flake; see BUILD 3 (fixed).

### BUILD 2 RESULT (2026-06-13): no safe "make them commit" fix — closing it.

The `diagnose_apply.py` resolver-detail dump (`src=/fills=/review=/val=` per question) showed the
enumerated bails are **the RESOLVER honestly bailing, not a `fill_combobox` miss**:
- relocate-to-Singapore (`src=review`), used-Tailscale-before (`src=review`), privacy-consent
  (`src=review`/CONSENT) → `needs_review=True`, no value ever reaches the filler. → assisted is
  CORRECT. Nothing to fix.
- "How many years of experience…" → `src=bank conf=0.84 fills=True`, but `val='Honest answer: zero
  years in that specific role…'` — a PROSE bank answer routed into a range react-select. It didn't
  commit because prose ≠ an option. **Auto-committing it is the WRONG move:** it would push a
  disqualifying-but-honest "0 years" onto an auto-submit screener (exactly Joseph's weakest axis,
  DBA→SE). Bailing to assisted so the human decides is the safer, honest behavior — the invariant
  ("a blank the human fills beats a confident wrong answer") says leave it. So a yes/no/number
  extractor in `fill_combobox` was deliberately NOT added: it would only fire on this prose case,
  where firing is undesirable. Clean standalone numeric answers are rare here and can be revisited
  if data shows them.

Net: the enumerated react-selects behave correctly. No filler change for screeners.

## BUILD 3 — `veteran_status` — DONE (2026-06-13): contraction regex gap, not a flake

Root-caused via the live EEO option-text dump. The resolver yields `"Prefer not to answer"`; the
decline-only branch in `_click_combobox_option` must click the **decline option**, whose wording
varies per form. Tailscale's veteran decline option is **"I don't wish to answer"** — and the old
`_DECLINE_SYNONYMS` `\b(?:not|n't|never)\b` alternative **never matched the contraction "don't"**
(no word boundary between "do" and "n't"), so no option matched → deterministic bail. It only LOOKED
intermittent because gender/hispanic use "Decline To Self Identify" (caught by `decline`) and
disability uses "I do not want to answer" (caught by `not…answer`) — only the "don't"/"won't"
phrasing slipped. Fix: `n['’]t` (no leading `\b`) in the negation alternative + added `share`.
VERIFIED LIVE: veteran_status now `[committed] I don't wish to answer`; all 4 EEO comboboxes commit.
Tests: `test_apply_driver.py::test_decline_*`. (The earlier scroll/menu-wait/retry hardening stays —
it was a separate below-the-fold concern.) 1026 green.

## THEN — the gated go-live (unchanged)

Once field-fill is trusted: a **watched `av3 apply --once --limit 1 --no-dry-run`** on ONE clean
Greenhouse Solutions job, browser visible, confirm `APPLIED` fires only on a positive confirmation.
Repeat per clean job. Most Solutions roles will still **assist-pend** on an honesty-sensitive
required screener or the attestation/consent bails — that is correct, not a failure.

## Invariants (never compromise)
Manual login only; never retry through CAPTCHA → assisted; mid-form break → REVIEW; `APPLIED` only
on positive confirmation; the bot never attests to being human, never auto-consents, never invents
an essay or overclaims a screener. A blank the human fills beats a confident wrong answer.
