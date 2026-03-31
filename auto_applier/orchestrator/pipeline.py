"""Pipeline stages: discover -> score -> decide -> apply."""
import asyncio

from auto_applier.browser.anti_detect import random_delay, reading_pause, simulate_organic_behavior
from auto_applier.browser.form_filler import FormFiller
from auto_applier.config import MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
from auto_applier.scoring.models import ScoreDecision
from auto_applier.storage.models import Job, Application, SkillGap
from auto_applier.storage import repository


async def discover_jobs(platform, keyword: str, location: str) -> list[Job]:
    """Search for jobs and filter out already-applied ones."""
    all_jobs = await platform.search_jobs(keyword, location)
    new_jobs = []
    for job in all_jobs:
        if not repository.job_already_applied(job.job_id, job.source):
            repository.save(job)
            new_jobs.append(job)
    return new_jobs


async def fetch_description(platform, job: Job) -> Job:
    """Fetch the full job description if not already present."""
    if not job.description:
        job.description = await platform.get_job_description(job)
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

    # Random delay between applications
    await random_delay(MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS)

    return app
