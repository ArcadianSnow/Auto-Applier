# Automated Apply — Next Build Handoff

> Written 2026-06-13, after the field-fill overhaul (commit **20b3731**, 1004 tests green).
> Read `automated-apply-go-live.md` first for the full backstory; this is the forward plan.
>
> **UPDATE 2026-06-13 — BUILD 1, 2, 3 all resolved. 1026 tests green.**
> - BUILD 1 (cover-letter upload) SHIPPED + validated live (Tailscale). Merged to master + pushed.
> - BUILD 2 (enumerated react-selects) CLOSED as no-fix-needed: the bails are honest RESOLVER
>   decisions, not a filler miss (proven via the resolver-detail dump). See "BUILD 2 RESULT".
> - BUILD 3 (veteran_status) FIXED: a `_DECLINE_SYNONYMS` contraction gap ("don't"), not a flake.
> The only thing left before go-live is the gated, watched `--no-dry-run` itself (+ the open user
> decision on multi-variant cover-letter defaults).

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

**Manual cover-letter library** — `settings.cover_letters_dir` (= `data_dir/"cover-letters"`) +
`generate.manual_cover_letter_path(settings, company) -> Path | None`. Resolution: an optional
`index.json` (`{company-or-slug: filename|abspath}`, keys matched by SLUG) first, then a fuzzy
filename match (`*.docx/.pdf/.doc/.txt/.rtf`, .docx preferred on ties). `_cover_slug` lowercases,
strips a leading `cover-letter` prefix + company suffixes (inc/llc/…), reduces to alphanumerics —
so `"Fivetran, Inc."` finds `Fivetran`. No match / no dir / empty company → `None` (benign no-attach).

**Worker** (`ApplyWorker._artifacts_for`) — when no per-job optimize `.txt` exists (the `av3 queue`
manual path), falls back to `manual_cover_letter_path(settings, job.company)`. The resolved path is
both uploaded (passed as `cover_letter_path` to `driver.prepare`) and recorded on the `Application`
row. Optimize `.txt` still wins when present.

**JobSearch wiring** — `av3data/cover-letters/index.json` maps the **unambiguous 1:1 companies**
(Fivetran, Prefect, Render, dbt Labs) to absolute paths at `JobSearch/cover-letters/*.docx` (no file
duplication; the dir holds only `index.json`, so fuzzy never auto-matches a wrong variant). The
multi-variant companies (Databricks, Grafana Labs, Postman, Tailscale, Vanta — different role / region
/ tier per letter) are intentionally left UNMAPPED and listed under `_unmapped_multi_variant`: the
worker returns no-match (safe — human attaches the right letter in assisted mode) rather than guessing.
**Decision still open for the user:** pick the single default letter per multi-variant company and add
it to the index, OR teach the worker a company+role key (it only has `job.company` today).

**Tests** — `tests/test_cover_library.py` (library + helper, 14 tests), driver-attach tests in
`test_apply_driver.py`, worker fallback/pass-through tests in `test_apply_worker.py`. All driver
stubs (incl. `test_session_expiry`) accept the new kwarg. 1023 green.

**Validate live (still a user-watched step):** `diagnose_apply.py` was extended — it now exercises
`manual_cover_letter_path(settings, <company>)` (2nd CLI arg, default = URL board token) and asserts
the hidden `#cover_letter` input actually receives a file after the dry-run (read-back of
`#cover_letter.files.length`, prints PASS/FAIL). Run watched on a real Greenhouse form to confirm the
on-page "Cover Letter" section shows the attached filename (the user's Image #2 showed it empty):
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
