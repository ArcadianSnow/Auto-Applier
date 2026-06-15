# Automated Apply — Next Build Handoff

> Written 2026-06-13, after the field-fill overhaul (commit **20b3731**, 1004 tests green).
> Read `automated-apply-go-live.md` first for the full backstory; this is the forward plan.
>
> **UPDATE 2026-06-13 — BUILD 1, 1.1, 1.2, 2, 3 all resolved. 1037 tests green.**
> - BUILD 1 (cover-letter upload) SHIPPED + validated live (Tailscale). Merged to master + pushed.
> - BUILD 1.1 (per-job cover model) REPLACED the company-index: per-posting letters via `av3 cover`
>   → `artifacts/uploads/<job_id>/Cover Letter<ext>` (generic upload basename = anti-detection),
>   archived on confirmed APPLIED. Company-index machinery removed. See "BUILD 1.1".
> - BUILD 1.2 (per-job RÉSUMÉ) extends the same model: `av3 resume` → `uploads/<job_id>/Resume<ext>`,
>   manual wins over the optimize PDF, archived on APPLIED. Mechanism shared with cover. See "BUILD 1.2".
> - BUILD 2 (enumerated react-selects) CLOSED as no-fix-needed: the bails are honest RESOLVER
>   decisions, not a filler miss (proven via the resolver-detail dump). See "BUILD 2 RESULT".
> - BUILD 3 (veteran_status) FIXED: a `_DECLINE_SYNONYMS` contraction gap ("don't"), not a flake.
> Left before go-live: the gated watched `--no-dry-run`; per-job `av3 cover` / `av3 resume` for the
> jobs you queue.

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

## BUILD 1.2 — Per-job RÉSUMÉ upload (same mechanism as the cover letter)

The résumé had the identical filename tell (`{job_id}.pdf` / `Joseph_Lira_Resume_Solutions_Engineer.pdf`
upload under those exact basenames). Extended the per-job model to it:
- `av3 resume <job_id> <resume.pdf>` copies into `artifacts/uploads/<job_id>/Resume<ext>` (generic
  basename). `av3 resume <job_id>` shows the current assignment.
- Worker résumé source order: manual `existing_job_resume(job.id)` → optimize per-job PDF
  (`generated/{job_id}.pdf`) → global `artifacts/resume.pdf`. Manual wins.
- Archived on confirmed APPLIED → `uploads/_archive/Resume - <job_id><ext>` (the optimize PDF / global
  résumé are left in place — `archive_resume` only moves a file in the uploads folder).
- The per-job upload mechanism is now **shared** (generate.py `_assign_upload`/`_existing_upload`/
  `_archive_upload`); cover + résumé are thin wrappers (stems `Cover Letter` / `Resume`). Tests:
  `test_cover_library.py` (résumé section), `test_cli_cover.py` (`av3 resume`), `test_apply_worker.py`
  (precedence + archive). 1037 green.

**Name-prefixed basenames (user request).** The upload basename is `<Full Name> <Stem><ext>` —
`Joseph Lira Resume.pdf` / `Joseph Lira Cover Letter.docx` — pulled from the fact bank
(`profile/master.json` `contact.name`) by the CLI. The name is NOT a templating tell (it's the
applicant's own), so it stays consistent with the anti-detection goal while looking like a normal
upload. No name in the fact bank → bare generic stem. Lookup globs `*<stem><ext>` so it finds the file
regardless of the prefix; the name is sanitized for illegal filename chars. 1042 green.

> Note: the OPTIMIZE-generated résumé still uploads as `{job_id}.pdf` (a numeric tell) when no manual
> résumé is assigned. The manual go-live path (`av3 queue` + `av3 resume`) avoids it; if the auto
> optimize→apply path is ever used for real, normalize that upload basename too (copy to a generic name
> before upload, same idea).

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

## BUILD 4 — Tier-1 watched dry-run (2026-06-14): 8 forms, resolver fixes (1053 green)

A full Tier-1 dress rehearsal ran 8 prepared Greenhouse jobs (`av3 apply --limit 8 --dry-run
--keep-open`), watched, with per-job cover/résumé assigned via `av3 cover`/`av3 resume`. The
cover+résumé UPLOAD worked on every job that had a letter assigned (dbt, Grafana, Tailscale-SE);
the "missed cover" reports were all jobs with **no letter on disk** (Hightouch/PlanetScale/
Tailscale-Impl) — not a bug. Postman was an expired posting (redirect to board search filters).
Cresta was a clean full-auto. The real findings (ground-truthed against `events.db`
`stage='resolution'`, then fixed in `answer_resolver.py`; all unit-tested):

1. **EEO declined everywhere** — `master.json` `eeo` was `{}`. The model + `_pick_eeo` already
   support it; this was DATA. Populated `eeo = {gender: Male, hispanic: Yes, veteran: "I am not a
   protected veteran", disability: "No, I do not have a disability"}` (keys are label substrings
   `_pick_eeo` matches; values land via the committer's exact/whole-word option match). EEO is
   the user's CHOICE to disclose — stays 100% local, never mirrored (the scrubber has no field).
2. **Work-auth not country-aware** — "eligible to work in **Australia**?" → "Yes" (WRONG);
   "require sponsorship in **Canada**?" → "No" (WRONG). A US citizen is authorized only in the
   US. Added `_detect_country` + `_authorized_countries` (derived from residence + work_auth,
   NEVER defaulted): when a question NAMES a country outside the authorized set, eligibility→"No",
   sponsorship→"Yes". Guarded to fire ONLY with a non-empty authorized set (unspecified bank can't
   misfire) and bare "us"/"US" is excluded (collides with the pronoun "hear about us"; a missed US
   match falls through to the already-correct residence logic). This is [[project_us_default_assumption]]
   coming due. NOTE: only handles STANDARD polarity ("eligible?"/"require?"); the inverted
   "authorization WITHOUT sponsorship" phrasing has no named country in the wild yet, so it's left
   on the residence logic (documented limitation).
3. **Preferred LAST name had no handler** → bailed (required). Added `ProfileField.PREFERRED_LAST_NAME`
   (ordered before FIRST) → fills the legal last name (last name token; single-token name → bail).
4. **"How did you hear about us?" bailed** — it matches the open-ended `\bhow did you\b` pattern, so
   it required a bank match, and dbt's phrasing didn't semantically match the seeded
   "…about this opportunity" answer (Grafana's did). Added a how-heard classifier (before the
   essay/LLM tiers) → returns the banked referral channel ("LinkedIn"); bails if none banked.
5. **Relocate-to-<country> didn't commit** — the bank answer was prose, never landed on the
   react-select. Added a relocation classifier → "Yes" for the residence/authorized country (US,
   a certainty), bail for any other country (a personal decision). Fixes relocate-to-US;
   relocate-to-Canada correctly bails pending the user's preference.

**Correct-by-design bails (left as-is, the user confirmed the gaps but these are the honesty floor):**
years-of-experience (prose "0 years" into a range select — auto-committing a disqualifier is worse
than assisted, BUILD 2), why-<company> essays (company-specific, never invented), and privacy/AI consent
(bot must not auto-consent).

**AI-trap "Which best describes you? / I am a human being" — RESOLVED via an owner opt-in (2026-06-14).**
The user verified the question appears on a normal browser visit too → it's a STATIC self-ID FORM FIELD,
not a behavioural/risk-scored anti-bot challenge (CAPTCHA/fingerprinting are a SEPARATE classifier in
`detect.py`, still never automated). The applicant IS a human, so filling the human option is truthful.
Implemented as `settings.attest_human` (default OFF — the safe bail stays the codebase default;
documented invariant unchanged for everyone else). When ON: `_resolve_sensitive`'s HUMAN_ATTESTATION
branch fills the human option (from `question.options` matching `_ATTEST_HUMAN_OPTION`, else "I am a
human being", source USER_CONFIG); the driver's `affirms_human` backstop now lets THROUGH a deliberate
HUMAN_ATTESTATION resolution (identified by sensitive class, not value text) while still blocking a stray
human-affirming value from any other field (defence vs LLM misfire / classification miss preserved).
Wired `apply_worker` → `AnswerResolver(attest_human=settings.attest_human)`; **enabled in Joseph's
`user_config.json`**. Tests: `test_answer_resolver` (opt-in fills / default bails) +
`test_apply_driver` (backstop allows deliberate, blocks stray). CAPTCHA/behavioural detection untouched.

**Still needs the user's data to close (assisted until then):** years-of-customer-facing-experience
(a number he'll stand behind), relocate-to-Canada preference, "used <product> before?" (bank a Y/N),
per-company "why interested" essays, and hand-authored cover letters for companies without one on disk.

**Re-verify:** the dry-run leaves all jobs in QUEUED_APPLY, so re-run the SAME watched command — the
updated resolver + EEO data take effect immediately (editable install). Confirm EEO now fills real
values, AU/CA work-auth is correct, preferred-last-name + how-heard + relocate-to-US fill.

## BUILD 5 — Cover letter ready for every strong job (BUILT 2026-06-15, awaiting voice sign-off)

User directive: "every job scored decently should have a cover letter written and ready, just in
case." Decided params (AskUserQuestion 2026-06-14): **scope = strong matches (score ≥ 8.0)**;
**trigger = auto-generate during the daily refresh + a backfill command** for the existing backlog.

**Reuse (already built):** `resume/generate.py` `CoverLetterGenerator` + `llm/prompts.GENERATE_COVER_LETTER`
+ the `vet_cover_letter` fabrication guard + `generated_cover_letter_path` (`generated/{job_id}_cover.txt`).
The generator produces a plain-text body today.

**To build:**
1. **Voice enforcement** — audit `GENERATE_COVER_LETTER` against [[feedback_writing_voice_no_ai_tells]]:
   NO em-dashes (#1), no "I'm excited to apply", no buzzwords, no rule-of-three. Bake these into the
   prompt as hard constraints (the current prompt predates that feedback — the memory says cover letters
   "need regen against this"). This is the quality crux; a sample MUST be read + voice-approved before bulk.
2. **.docx render** — render the guarded letter to `<uploads>/<job_id>/Joseph Lira Cover Letter.docx`
   (per [[feedback_paste_docs_as_docx]] — .docx, not md). python-docx (already a dep via the résumé path).
   **Never clobber a hand-authored letter** — skip if `existing_job_cover()` already returns one (a manual
   `av3 cover` always wins; auto-gen only fills the gap).
3. **Guard** — run `vet_cover_letter`; any unsupported claim → DON'T ship that letter, flag (the letter is
   "ready just in case", so a guard failure means leave it unwritten + note, never ship a fabrication).
4. **Command** — `av3 cover --generate <job_id>` (one) + `av3 cover --generate-all [--min-score 8.0]`
   (backfill over DECIDED jobs ≥ floor). Mirrors the existing `av3 cover` command surface.
5. **Auto-trigger** — in the daily-refresh / scoring path, after a job lands DECIDED with score ≥ 8.0,
   generate its letter (skip if one exists). Keep it OFF the apply pipeline (he's discovery+scoring-only).
6. **Tests** — generation→guard→.docx→no-clobber; score-floor filter; backfill batch; trigger fires only ≥ floor.

**Verification:** generate ONE sample for a real ≥8.0 job, READ it, get the user's voice sign-off BEFORE
backfilling hundreds (the no-AI-tells bar is his, and qwen3:8b prose is the risk). ~30-60s/letter locally.

### BUILD 5 RESULT (2026-06-15): BUILT + tested; bulk gated on the user's voice sign-off

Shipped (1078+ green). What landed in the repo (PII-free):
- **`auto_applier/resume/cover_autogen.py`** (new): `render_cover_letter_docx` (python-docx, complete
  letter = name/contact header + "Dear Hiring Manager," + body paras + "Sincerely,"+name; sets
  `core_properties.author` to the applicant's name, NOT the python-docx default tell), `generate_one`
  (no-clobber unless `force`; empty-JD short-circuit; `vet_cover_letter` fail-closed; `.docx` to
  `job_cover_upload_path`), `backfill` (`ScoreRepo.list_ranked(min_total)` → DECIDED-only → only-missing
  → `--limit` caps real LLM work), and **`_strip_ai_tells`** (deterministic em/en-dash → comma; the #1
  tell can never ship regardless of model drift; newline-safe so paragraph breaks survive).
- **`GENERATE_COVER_LETTER` → `gen-cover-v2`**: hard voice constraints (no dashes; categorical ban on
  "excited/thrilled/passionate/delighted/enthusiasm" in ANY form — the v1 phrase-list let "excited to
  HELP" slip; buzzword list; no rule-of-three / no "I'm confident my") AND an **anti-overclaim** clause
  (do NOT claim experience/responsibility/domain the bank lacks — the cover guard only vets *technical*
  claims, so the prompt is the only thing stopping invented *soft* claims).
- **`settings.cover_autogen_min_score: float = 8.0`** (the `--min-score` default).
- **CLI `av3 cover`** extended: `--generate <id>` (one; `--force` overwrites), `--generate-all`
  (`--min-score`, `--limit`); `job_id` now optional; no-arg → exit 2 hint.
- Tests: `tests/test_cover_autogen.py` (render / generate_one happy·no-clobber·force·empty·guard·error /
  dash-strip / backfill score-floor·state·limit·only-missing / voice contract) + `tests/test_cli_cover.py`
  (`--generate`, `--force`, `--generate-all`, arg errors).

**Live samples (qwen3:8b, the real CLI path), audited 2026-06-15** — see
`JobSearch/cover-letter-samples-REVIEW.md` for the full text + the sign-off ask:
- cube Data Engineer (near-bank): strong, honest, all bank-grounded, no tells.
- aircall Forward-Deployed/AI-Solutions (far-from-bank): the anti-overclaim fix WORKS — sticks to real
  work, bridges with "These experiences align with…", does NOT invent Solutions experience. (My first
  draft of a Solutions letter HAD invented "financial modeling / TCO models / executive presentations
  to CTOs/CFOs" — the guard passed it because those aren't tech terms. That's the soft-fabrication gap
  the gen-cover-v2 anti-overclaim clause closes; the cube/aircall samples are post-fix.)

**Key findings for the bulk run (recorded for the go-decision):**
- **525 strong DECIDED jobs (≥8.0) have no letter.** First unbounded `av3 cover --generate-all` ≈ 30-70
  min of local LLM (free). Score bands: 144 at 9.x, 381 at 8.x.
- **Large-JD timeouts.** qwen3:8b reasoning on a ~8K-char JD exceeds the 180s Ollama read timeout
  (Cockroach Labs, 8015 ch, timed out twice). Fails safe (ERROR, no letter), but a chunk of big-JD strong
  jobs would silently end up letterless. FOLLOW-UP: disable qwen3 "thinking" for cover-gen, or raise
  `_OllamaJSONBackend.timeout_s`, or chunk. Small JDs are ~3-8s.
- **Daily-refresh auto-trigger is WIRED BUT DISABLED** (commented in `JobSearch/daily-refresh.ps1`, step
  3.5) until the voice is approved — bounded with `--limit 25` so the 1PM task never balloons. The
  one-time 525 backlog drain is a deliberate `av3 cover --generate-all` the user kicks off.

**Open (the user's calls, morning of 2026-06-15):** (1) approve the voice (or request changes — e.g. ban
"scalable", force 3-paragraph breaks); (2) kick the one-time backlog drain; (3) then uncomment the refresh
step. Until (1), nothing bulk runs.

## THEN — the gated go-live (unchanged)

Once field-fill is trusted: a **watched `av3 apply --once --limit 1 --no-dry-run`** on ONE clean
Greenhouse Solutions job, browser visible, confirm `APPLIED` fires only on a positive confirmation.
Repeat per clean job. Most Solutions roles will still **assist-pend** on an honesty-sensitive
required screener or the attestation/consent bails — that is correct, not a failure.

## Invariants (never compromise)
Manual login only; never retry through CAPTCHA → assisted; mid-form break → REVIEW; `APPLIED` only
on positive confirmation; the bot never attests to being human, never auto-consents, never invents
an essay or overclaims a screener. A blank the human fills beats a confident wrong answer.
