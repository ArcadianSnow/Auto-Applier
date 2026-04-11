"""Pipeline stages: discover -> score -> decide -> apply."""
import asyncio

from auto_applier.browser.anti_detect import random_delay, reading_pause, simulate_organic_behavior
from auto_applier.browser.form_filler import FormFiller
from auto_applier.config import MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
from auto_applier.scoring.models import ScoreDecision
from auto_applier.storage.models import Job, Application, SkillGap
from auto_applier.storage import repository


async def discover_jobs(platform, keyword: str, location: str) -> list[Job]:
    """Search for jobs and filter out duplicates.

    Filters at two levels:
    1. Per-source: already-applied ``(job_id, source)`` pairs.
    2. Canonical: cross-source duplicates matched by ``canonical_hash``
       (same company + title normalized). Catches the same listing
       cross-posted to multiple platforms or discovered in a previous
       run under a different job_id.
    """
    all_jobs = await platform.search_jobs(keyword, location)
    new_jobs: list[Job] = []
    seen_this_batch: set[str] = set()
    for job in all_jobs:
        if repository.job_already_applied(job.job_id, job.source):
            continue
        if job.canonical_hash:
            if job.canonical_hash in seen_this_batch:
                continue
            if repository.job_seen_canonically(job.canonical_hash):
                continue
            seen_this_batch.add(job.canonical_hash)
        repository.save(job)
        new_jobs.append(job)
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

    # Build application record
    status = "dry_run" if dry_run else ("applied" if result.success else "failed")
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
