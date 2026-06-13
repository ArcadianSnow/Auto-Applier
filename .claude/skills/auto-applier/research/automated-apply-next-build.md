# Automated Apply — Next Build Handoff

> Written 2026-06-13, after the field-fill overhaul (commit **20b3731**, 1004 tests green).
> Read `automated-apply-go-live.md` first for the full backstory; this is the forward plan.

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

## BUILD 1 — Cover-letter upload (biggest remaining; user-requested)

**DOM (confirmed live on Hightouch):** there's a hidden file input **`#cover_letter`**
(`class="visually-hidden"`, `accept=".pdf,.doc,.docx,.txt,.rtf"`) parallel to `#resume`. It's
already in the DOM (behind the "Attach" button visually) — **`set_input_files("#cover_letter", path)`
works directly**, no need to click Attach. Same pattern as the existing résumé upload.

**Driver change (`greenhouse_apply.prepare_application`, mirror in lever/ashby):**
- Add a `cover_letter_path: str = ""` parameter.
- After the résumé attach, if `cover_letter_path` and `#cover_letter` exists: `set_input_files`
  it; record `outcome.filled["cover_letter"]`. Defensive (a failure is observable, not fatal).

**Worker change (`ApplyWorker._process_one`):**
- `_artifacts_for(job)` already returns `cover_used` (the per-job optimize-generated `.txt`), but
  the manual-queue path (`av3 queue`) has none → `cover_used=""`. So add a **job→letter map** for
  the manual path: Joseph's 20 Solutions letters live in `JobSearch/cover-letters/*.docx`
  (`build_solutions_cover_letters.py`), one per company. Map by `job.company` (slug/normalize) →
  the matching `.docx`; fall back to `""` (no letter) if unmatched. Pass it to `driver.prepare`.
- Greenhouse accepts `.docx`, so upload the `.docx` directly — no PDF render needed. (Optimize-
  generated `.txt` cover letters: render or skip; the manual `.docx` is the real content.)
- Decide where the map lives: simplest is a small `cover-letters/index.json` in JobSearch
  (`{company: filename}`) that the worker reads, OR a settings-level `cover_letters_dir` + fuzzy
  company match. Keep it in JobSearch (PII-adjacent), not the repo.

**Validate:** extend `diagnose_apply.py` to pass a `cover_letter_path` and assert `#cover_letter`
has a file after; run on a Greenhouse form. Confirm the on-page "Cover Letter" section shows the
attached filename (the user's Image #2 showed it empty).

**Note:** Lever/Ashby cover-letter inputs differ — scope each before wiring (Lever often has a
single combined upload; Ashby varies). Greenhouse first (the user's whole batch is Greenhouse).

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

## BUILD 3 — `veteran_status` intermittent EEO commit

One non-required EEO react-select flakes ~1/run (the others commit). Already added scroll-into-view
+ menu-wait + pre-clear + 3 retries. If it matters, instrument `fill_combobox` to log which option
it clicked and re-run the diagnostic a few times to see the failure mode (menu not rendering vs
option-click intercepted). Benign for now — EEO is optional, blank is acceptable.

## THEN — the gated go-live (unchanged)

Once field-fill is trusted: a **watched `av3 apply --once --limit 1 --no-dry-run`** on ONE clean
Greenhouse Solutions job, browser visible, confirm `APPLIED` fires only on a positive confirmation.
Repeat per clean job. Most Solutions roles will still **assist-pend** on an honesty-sensitive
required screener or the attestation/consent bails — that is correct, not a failure.

## Invariants (never compromise)
Manual login only; never retry through CAPTCHA → assisted; mid-form break → REVIEW; `APPLIED` only
on positive confirmation; the bot never attests to being human, never auto-consents, never invents
an essay or overclaims a screener. A blank the human fills beats a confident wrong answer.
