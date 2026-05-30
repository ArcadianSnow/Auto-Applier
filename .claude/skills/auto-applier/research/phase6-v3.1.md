# Phase 6 — v3.1 (after core proves out)

Knowledge base for the **v3.1** workstreams (spec §11b Phase 6 + the §11 scope-split table). v3.0-core
(phases 0–5) shipped 2026-05-29 at `v3@3fab3f9`, 612 tests green. Phase 6 has **several independent
sub-phases with no mandated order**:

- per-job résumé-path rewire (a Phase-3 carry-over swept up first — see (1/M) below)
- configurable Pareto strategy profiles (§8a)
- salary intelligence + BLS OES market data (§8d)
- outcome feedback loop (§8e)
- interactive batch skill-reconciliation (§7b)
- story bank + company research + rich analytics / what-to-learn trends
- branded UI polish (frontend-design skill)

Each sub-phase is its own slice: build → tests green → record rationale here → update spec/memory.

---

## (1/M) — Per-job résumé-path rewire (2026-05-30)

**What & why.** Closes the oldest open carry-over: the apply worker shipped the single global
`artifacts/resume.pdf` for *every* job, ignoring the per-job tailored résumé the optimize+Strict gate
(`optimize_worker`, spec §7 #6) generates. That made the whole "generate a tailored résumé per job from the
fact bank" pipeline cosmetic at the apply step — the auto-apply path uploaded a generic résumé. This is a
real correctness defect, so it leads Phase 6 before any net-new v3.1 feature.

**The contract (already established in Phase 3, now honoured).** The optimize worker writes two artifacts
keyed by `job.id`, via helpers in `av3/resume/generate.py`:

- `generated_resume_path(settings, job_id)`   → `artifacts_dir/generated/{job_id}.pdf`
- `generated_cover_letter_path(settings, job_id)` → `artifacts_dir/generated/{job_id}_cover.txt`

**File existence IS the durable "this job was optimized" contract** — there is deliberately NO DB column for
the path (decided in Phase 3 (3/M)). So the apply worker derives the SAME paths from `job.id` and reads them;
no hand-off table, no migration.

**Implementation** (`av3/pipeline/apply_worker.py`):

- New `ApplyWorker._artifacts_for(job) -> (resume_path, cover_path)`:
  - résumé = the per-job PDF when it exists on disk, **else** the global `resume.pdf` the worker was
    constructed with (`self._resume_path`).
  - cover = the per-job `.txt` when it exists, else `""`.
- `_process_one` calls it once and threads `resume_used` into `driver.prepare(...)` (replacing the old
  blanket `self._resume_path`) and writes both `generated_resume_path` + `cover_letter_path` onto the
  `Application` row.
- `_recover_job_to_review` (the per-job exception → FAILED→REVIEW path) uses the same helper so a FAILED
  attempt records which résumé it *would* have used (dashboard triage parity with the success path).

**Why keep the global `resume.pdf` as a fallback (not remove it).** A job can reach `QUEUED_APPLY` without a
per-job PDF: a crash-swept APPLYING leftover, a manual re-queue, or a job queued before optimize ran. The
fallback guarantees the apply step always has *a* résumé to upload rather than crashing. The CLI pre-flight
(`av3 apply` / `av3 run`) still requires `resume.pdf` for exactly this reason; only the comments/fix-hints
were updated to call it the fallback (it is no longer the primary).

**Why this is safe / backward-compatible.** The constructor signature is unchanged (`resume_path=` stays the
fallback). Existing worker tests pass `resume_path="/tmp/resume.pdf"` and never write per-job files, so they
fall through to the fallback exactly as before — no existing assertion changed.

**Tests** (`tests_v3/test_apply_worker.py`, +4):
- `test_uses_per_job_generated_resume_when_present` — writes the per-job PDF, asserts the driver received it
  AND the Application row records it.
- `test_falls_back_to_global_resume_when_no_per_job_pdf` — no per-job file → driver + row get the global path;
  cover path is `""`.
- `test_records_per_job_cover_letter_path_when_present` — per-job `.txt` present → recorded on the row.
- `test_failed_recovery_records_per_job_resume_path` — driver crash → FAILED row carries the per-job path.

**Not in this sub-phase.** Whether the apply *drivers* paste the cover-letter `.txt` into a form textarea is
separate (drivers currently take only `resume_path`; cover-letter field-fill is a future driver concern). This
sub-phase makes the worker *resolve + record* the right artifacts; the résumé upload is wired end-to-end, the
cover-letter is recorded on the row for the dashboard.

**Validation:** full v3 suite green (612 → 616), 11 deselected by design.
