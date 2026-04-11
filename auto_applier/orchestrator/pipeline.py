"""Pipeline stages: discover -> score -> decide -> apply."""
import logging

from auto_applier.browser.anti_detect import random_delay, reading_pause
from auto_applier.browser.form_filler import FormFiller
from auto_applier.config import MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
from auto_applier.storage.models import Job, Application
from auto_applier.storage import repository

logger = logging.getLogger(__name__)


async def discover_jobs(
    platform, keyword: str, location: str, dry_run: bool = False,
) -> list[Job]:
    """Search for jobs and filter out duplicates.

    Filters at two levels for real runs:
    1. Per-source: already-applied ``(job_id, source)`` pairs.
    2. Canonical: cross-source duplicates matched by ``canonical_hash``
       (same company + title normalized). Catches the same listing
       cross-posted to multiple platforms or discovered in a previous
       run under a different job_id.

    Dry runs bypass BOTH dedup gates so the same jobs can be
    re-processed across sessions. This is essential for iterative
    testing — otherwise the first dry run saves every job to
    ``jobs.csv`` and every subsequent run finds zero 'new' jobs,
    making the pipeline look broken when it's just working exactly
    as designed for real runs.

    The log line at the end reports the exact fate of every job
    so the 'Found 99 jobs, processed 0' mystery is always visible.
    """
    all_jobs = await platform.search_jobs(keyword, location)
    raw = len(all_jobs)
    new_jobs: list[Job] = []
    seen_this_batch: set[str] = set()
    filtered_applied = 0
    filtered_canonical = 0
    filtered_batch_dup = 0

    for job in all_jobs:
        if not dry_run:
            if repository.job_already_applied(job.job_id, job.source):
                filtered_applied += 1
                continue
            if job.canonical_hash:
                if job.canonical_hash in seen_this_batch:
                    filtered_batch_dup += 1
                    continue
                if repository.job_seen_canonically(job.canonical_hash):
                    filtered_canonical += 1
                    continue
                seen_this_batch.add(job.canonical_hash)
        else:
            # Dry-run path: only dedup within the current batch so
            # we don't score the same listing twice in one run, but
            # do NOT consult persisted state.
            if job.canonical_hash and job.canonical_hash in seen_this_batch:
                filtered_batch_dup += 1
                continue
            if job.canonical_hash:
                seen_this_batch.add(job.canonical_hash)
        repository.save(job)
        new_jobs.append(job)

    logger.info(
        "discover_jobs[%s/%s]: raw=%d kept=%d filtered=%d "
        "(already_applied=%d, canonical_dup=%d, batch_dup=%d) dry_run=%s",
        platform.source_id, keyword,
        raw, len(new_jobs),
        filtered_applied + filtered_canonical + filtered_batch_dup,
        filtered_applied, filtered_canonical, filtered_batch_dup, dry_run,
    )
    return new_jobs


async def fetch_description(platform, job: Job) -> Job:
    """Fetch the full job description and liveness in one navigation.

    We're already navigating to the job URL to read the description —
    check liveness from the same loaded page so dead listings can be
    skipped without a second round-trip.
    """
    if not job.description:
        job.description = await platform.get_job_description(job)
    # Liveness inspection uses the page already loaded by
    # get_job_description — no second navigation.
    try:
        await platform.check_liveness(job, navigate=False)
    except Exception:
        job.liveness = "unknown"
    return job


async def apply_to_job(
    platform,
    job: Job,
    resume_path: str,
    resume_text: str,
    resume_label: str,
    personal_info: dict,
    router,
    dry_run: bool = False,
) -> Application:
    """Apply to a single job and return the Application record."""

    # If a tailored resume PDF was generated ahead of time for this
    # job, use it for the upload instead of the original file. The
    # tailored version is strictly additive — falls back silently
    # when none exists.
    from auto_applier.resume.tailor import tailored_pdf_path
    tailored = tailored_pdf_path(job.job_id)
    if tailored.exists():
        resume_path = str(tailored)

    # Create FormFiller with context for this specific job
    form_filler = FormFiller(
        router=router,
        personal_info=personal_info,
        resume_text=resume_text,
        job_description=job.description,
        company_name=job.company,
        job_title=job.title,
        resume_label=resume_label,
        platform_display_name=getattr(platform, "display_name", ""),
    )
    platform.form_filler = form_filler

    # Simulate reading the job before applying
    page = await platform.get_page()
    await reading_pause(page)

    # Apply
    result = await platform.apply_to_job(job, str(resume_path), dry_run)

    # Record gaps
    for gap in result.gaps:
        gap.source = platform.source_id
        repository.save(gap)

    # Build application record.
    #
    # The old logic was `status = "dry_run" if dry_run else ...`
    # which marked EVERY dry-run job as 'dry_run' regardless of
    # whether the apply actually succeeded — external redirects,
    # failed form walks, missing apply buttons, and honeypot-blocked
    # forms all looked identical to real dry-run applies. That's
    # why 'Sr. Marketing Measurement Analyst (Remote)' showed as an
    # apply in the dashboard when it was actually skipped as external.
    #
    # New logic: dry-run status only applies when the APPLY ACTUALLY
    # REACHED THE SUBMIT STEP. A failed apply during dry-run is
    # recorded as 'failed' with the real failure_reason intact, so
    # the dashboard counter and patterns analysis reflect what
    # actually happened.
    if result.success:
        status = "dry_run" if dry_run else "applied"
    else:
        status = "failed"
    app = Application(
        job_id=job.job_id,
        status=status,
        source=platform.source_id,
        resume_used=resume_label,
        score=0,
        cover_letter_generated=result.cover_letter_generated,
        failure_reason=result.failure_reason,
        fields_filled=result.fields_filled,
        fields_total=result.fields_total,
        used_llm=result.used_llm,
    )
    repository.save(app)

    # Schedule follow-up reminders on real submissions (not dry-run).
    if status == "applied":
        try:
            repository.schedule_followups(
                job_id=job.job_id,
                source=platform.source_id,
                applied_at_iso=app.applied_at,
            )
        except Exception:
            pass

    # Random delay between applications
    await random_delay(MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS)

    return app
