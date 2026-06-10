# Manual / human-apply tracking mode

**Shipped 2026-06-10.** A first-class operating mode for "discover + score only; a human applies
externally and records it." Built for the personal job search but lives in the product (`auto_applier/`).

## The workflow

1. `av3 shortlist --family <f> --location <mode> [--limit N] [--name NAME]` → writes a persistent,
   apply-ready shortlist to `<data_dir>/shortlist/<name>.md` (clickable, with job ids) + `<name>.json`
   (machine-readable batch). Ranked **location-fit first, then score**.
2. Human applies to the jobs externally, using the matching résumé variant.
3. `av3 applied --shortlist <name> --all` (or `av3 applied <job-id>...`) → marks them `APPLIED`
   (`mode = MANUAL`). They leave the DECIDED pool.
4. `av3 pass <job-id>...` → DECIDED → SKIPPED for "looked, not interested."
5. `av3 outcome <job-id> <kind>` (pre-existing) → log response/interview/etc. — already accepts
   manually-applied jobs since it only checks `state == APPLIED`.

## Design (the load-bearing decisions)

- **Reuse `APPLIED`, no new state.** Dedup (`applied_canonical_hashes`), retention (never prunes APPLIED),
  analytics, the web history panel, and `av3 outcome` all key off `jobs.state == APPLIED` and never inspect
  the apply *mode*. A new `MANUALLY_APPLIED` state would force all of them to learn about it. Provenance
  (bot vs human) is captured in `applications.mode` (`BROWSER_AUTO`/`BROWSER_ASSISTED`/**`MANUAL`**), not a
  second job state.
- **New edges:** `DECIDED → APPLIED` and `REVIEW → APPLIED` in `domain/state.py`. A human attestation is a
  positive confirmation, not a click inference — so the "APPLIED only on positive confirmation" invariant
  (§5/§8) still holds. APPLIED stays terminal (no new outbound edges).
- **No schema change.** `applications.mode`/`status` have no CHECK constraints (enums validated only in
  Python), so adding `ApplyMode.MANUAL` needed no DDL.
- **`mark_manually_applied` is idempotent + batch-safe** (`pipeline/manual_apply.py`): per-job `tx()`;
  already-APPLIED → "already"; non-{DECIDED,REVIEW} or unknown id → "error" (returns, never raises) so one
  bad id can't abort a batch. Writes the `Application` row FIRST, then `set_state` (mirrors apply_worker).

## ⚠️ Gotchas (the bugs we almost shipped)

- **`ScoreRepo.list_ranked()` is NOT state-filtered.** It returns every scored job regardless of state.
  The shortlist command MUST filter to `state == DECIDED` in Python, or marked-APPLIED jobs re-surface —
  defeating the whole feature. This is the #1 regression test (`test_cli_shortlist.py`).
- **Coarse-hash siblings are NOT auto-skipped.** `canonical_hash` is deliberately coarse (title+company),
  so two distinct DECIDED rows can share one. Marking one APPLIED leaves the sibling DECIDED. "Apply to one
  collapses the rest" would be a separate, explicit feature. Documented, not a bug.
- **`pass` → SKIPPED is ephemeral.** A passed job is prune-eligible after `ephemeral_days` and (since dedup
  only keys off APPLIED) can be re-discovered later. Intended.
- **Mixed-mode pacing:** a manual apply bumps `company_applied_count`/`applied_count_on_day`. Harmless in
  discovery+scoring-only mode (the apply worker isn't running); intentional if both run.

## Role-family classifier (`domain/job_family.py`)

New pure module mirroring `domain/location.py`. `classify_family(title) -> JobFamily` with ordered,
first-match keyword regex on the normalized title (reuses `dedup.normalize`). Families map **1:1 to the
résumé variants**: SOLUTIONS, DATA_PLATFORM (= data eng + analytics eng + data platform + platform eng),
DATABASE, AI_APPLICATION, BACKEND, ANALYTICS, OTHER. Order matters: "Analytics Engineer" → DATA_PLATFORM
(engineering), not ANALYTICS (BI); "ML Platform Engineer" → DATA_PLATFORM ("platform engineer" wins over
"machine learning"); "Software Engineer, Data Platform" → DATA_PLATFORM, not BACKEND.

## Files

- `domain/state.py` (edges + `ApplyMode.MANUAL`), `domain/job_family.py` (new), `pipeline/manual_apply.py`
  (new), `config/settings.py` (`shortlist_dir`), `cli/main.py` (`shortlist`/`applied`/`pass`).
- Tests: `test_state.py` (extended), `test_job_family.py`, `test_manual_apply.py`, `test_cli_shortlist.py`,
  `test_cli_applied.py`. Full suite 834 green.
- Verified end-to-end against the live search DB: shortlist → applied → job leaves the shortlist.
