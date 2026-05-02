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

    # Build dedup sets ONCE per batch instead of re-reading the CSVs on
    # every iteration. Naive per-job calls gave us O(jobs × rows) reads
    # that would noticeably degrade after a few hundred runs.
    #
    # Both sets deliberately key off PROCESSED state (any Application
    # row exists), not Jobs-as-scraped. That keeps continuous-run mode
    # honest: cycle 1 may only score 3 of 99 scraped jobs because the
    # per-platform budget ran out, and cycle 2 has to be free to pick
    # up the other 96. Dedupping on the raw Job set would bury them.
    processed_pairs: set[tuple[str, str]] = set()
    processed_hashes: set[str] = set()
    if not dry_run:
        processed_pairs = repository.processed_pairs()
        processed_hashes = repository.processed_canonical_hashes()

    for job in all_jobs:
        if not dry_run:
            if (job.job_id, job.source) in processed_pairs:
                filtered_applied += 1
                continue
            if job.canonical_hash:
                if job.canonical_hash in seen_this_batch:
                    filtered_batch_dup += 1
                    continue
                if job.canonical_hash in processed_hashes:
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
    """Fetch description + liveness + external check in one navigation.

    Stacking three page inspections on the single get_job_description
    navigation means external jobs are skipped before any LLM cycles
    are spent on them. Previously external-only jobs burned 20-70
    seconds on ghost check + archetype classify + multi-dim scoring
    before the apply step discovered they were external and bailed.

    Order:
    1. Load the page via get_job_description (required for
       everything else)
    2. Liveness check — is this listing alive?
    3. External check — can we even apply to this via the platform's
       flow, or is the only apply route a third-party ATS?

    Both liveness and external are stored on the Job's ``liveness``
    field so the engine's existing skip gate picks them up with
    no downstream changes. Values: "live" | "dead" | "external" | "unknown".
    """
    if not job.description:
        job.description = await platform.get_job_description(job)
        # Persist the JD onto the Job row so post-hoc commands like
        # `cli cover`, `cli tailor`, `cli research` see real text
        # instead of the empty stub that discovery wrote.
        if job.description:
            try:
                repository.update_job_description(job.job_id, job.description)
            except Exception as exc:
                logger.debug(
                    "Could not persist JD for %s: %s", job.job_id, exc,
                )
    # Liveness inspection uses the page already loaded by
    # get_job_description — no second navigation.
    try:
        await platform.check_liveness(job, navigate=False)
    except Exception:
        job.liveness = "unknown"
    # If it's dead, don't waste time checking external.
    if job.liveness == "dead":
        return job
    # Fast-skip external-only listings (Apply on company site, etc.)
    # before any scoring happens.
    try:
        if await platform.check_is_external(job):
            job.liveness = "external"
    except Exception:
        pass
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
    score: int = 0,
) -> Application:
    """Apply to a single job and return the Application record.

    ``score`` is the resume-vs-JD score the engine already computed
    before deciding to apply. It's persisted on the Application row
    so post-hoc analyses (resume-suggestion, conversion patterns,
    refine flow's archetype-mismatch detector) have a real signal
    to read instead of zeros. Without this, every saved Application
    had score=0 and check_resume_suggestion's `score <= 0` filter
    silently dropped every row.
    """

    # Auto-personalize the resume for this job. If we've already
    # tailored for this job_id (e.g. a prior `cli tailor` run, or an
    # earlier auto-tailor in this session), reuse it. Otherwise
    # generate one now from resume + JD via the LLM, render to PDF,
    # and use that for the upload.
    #
    # On real (non-dry) runs we tailor unconditionally — that's the
    # whole point of "AI personalizes your resume for each job".
    # Dry runs skip the tailor LLM call to keep the test cycle fast;
    # the apply flow then falls back to the original resume file.
    #
    # Tailoring cost: one LLM call (~5-15s) + one Playwright PDF
    # render (~3-5s) per first-apply to a job. Cached on disk after
    # that, so re-applies / cli tailor / cli show are all free.
    from auto_applier.resume.tailor import (
        tailored_pdf_path, ResumeTailor,
        save_tailored_json, render_html, render_pdf,
    )
    tailored = tailored_pdf_path(job.job_id)
    if not tailored.exists() and not dry_run and resume_text and job.description:
        try:
            tailor_obj = ResumeTailor(router)
            tailored_resume = await tailor_obj.tailor(
                resume_text=resume_text,
                job_description=job.description,
                company_name=job.company or "the company",
                job_title=job.title or "the position",
                job_id=job.job_id,
                resume_label=resume_label,
            )
            if tailored_resume is not None:
                # Pull display name + contact from personal_info so
                # the rendered PDF shows the candidate's actual info,
                # not "Jane Doe" placeholders.
                disp_name = (
                    personal_info.get("name")
                    or " ".join(
                        x for x in (
                            personal_info.get("first_name", ""),
                            personal_info.get("last_name", ""),
                        ) if x
                    )
                    or "Resume"
                )
                contact_bits = [
                    personal_info.get("email", ""),
                    personal_info.get("phone", ""),
                    personal_info.get("city", ""),
                ]
                disp_contact = " | ".join(b for b in contact_bits if b)
                html = render_html(tailored_resume, disp_name, disp_contact)
                ok = await render_pdf(html, tailored)
                if ok:
                    save_tailored_json(tailored_resume)
                    logger.info(
                        "Auto-tailored resume for %s — using tailored PDF for upload",
                        job.job_id,
                    )
        except Exception as exc:
            logger.warning(
                "Auto-tailor failed for %s (%s) — falling back to original resume",
                job.job_id, exc,
            )
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
    elif getattr(result, "requires_manual_apply", False):
        # External-application jobs: site has no in-platform apply
        # button (Indeed external link, Dice no Easy Apply, etc.).
        # Route to "skipped" so the GUI manual-apply panel surfaces
        # them, not the failed bucket — the user CAN apply, just
        # has to do it manually on the company's website.
        status = "skipped"
    else:
        status = "failed"
    app = Application(
        job_id=job.job_id,
        status=status,
        source=platform.source_id,
        resume_used=resume_label,
        score=score,
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
