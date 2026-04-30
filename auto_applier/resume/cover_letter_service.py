"""On-demand cover letter generation for any job in history.

This sits on top of ``resume/cover_letter.py`` (which only knows how
to call the LLM) and adds the pieces needed for CLI use:

- Loading the Job from storage by job_id
- Picking the right resume (either explicit label or best-scored)
- Writing the letter to ``data/cover_letters/`` as Markdown
- A plain-English filename that a human can browse
- Strong hallucination guards in the prompt (inherited from
  ``llm/prompts.py:COVER_LETTER``)

Used by ``cli cover <job_id>`` and the interactive flow in
``cli almost`` where the user wants a letter for a manually-applied
external job.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from auto_applier.config import COVER_LETTERS_DIR
from auto_applier.llm.router import LLMRouter
from auto_applier.resume.cover_letter import CoverLetterWriter
from auto_applier.resume.manager import ResumeManager
from auto_applier.storage.models import Application, Job
from auto_applier.storage.repository import load_all

logger = logging.getLogger(__name__)


@dataclass
class CoverLetterResult:
    """What `generate_cover_letter` returns."""
    job_id: str
    job_title: str
    company: str
    resume_label: str
    letter: str  # the full generated text
    file_path: Path | None  # where we saved it, or None on failure


def _slugify(text: str, max_len: int = 40) -> str:
    """Filesystem-safe slug. Preserves rough readability."""
    text = text.strip().lower()
    # Replace anything non-alphanumeric with a hyphen
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len] or "job"


def _pick_resume_for_job(
    job: Job,
    resume_manager: ResumeManager,
    preferred_label: str = "",
) -> tuple[str, str]:
    """Decide which resume to use for this job's cover letter.

    Priority:
    1. ``preferred_label`` if given AND it exists
    2. The resume recorded on the Application (if the job was already
       processed — this matches what scoring picked)
    3. The only resume the user has (if just one exists)
    4. First resume alphabetically (fallback — user only has one setup run)

    Returns ``(resume_label, resume_text)``. ``resume_text`` is the
    enriched text via ResumeManager.get_resume_text so confirmed skills
    added by Evolution are included.
    """
    # 1. Explicit preference
    if preferred_label:
        info = resume_manager.get_resume(preferred_label)
        if info is not None:
            return preferred_label, resume_manager.get_resume_text(preferred_label)
        logger.warning(
            "Preferred resume '%s' not found, falling back to best match",
            preferred_label,
        )

    # 2. Resume recorded on the Application
    for app in load_all(Application):
        if app.job_id == job.job_id and app.resume_used:
            info = resume_manager.get_resume(app.resume_used)
            if info is not None:
                return app.resume_used, resume_manager.get_resume_text(app.resume_used)

    # 3. Only one resume loaded
    resumes = resume_manager.list_resumes()
    if len(resumes) == 1:
        label = resumes[0].label
        return label, resume_manager.get_resume_text(label)

    # 4. Fallback to first alphabetically — user will rarely hit this
    # because most users have applied to at least one job already
    if resumes:
        label = resumes[0].label
        return label, resume_manager.get_resume_text(label)

    return "", ""


async def generate_cover_letter(
    job_id: str,
    router: LLMRouter,
    resume_manager: ResumeManager,
    preferred_resume: str = "",
    save_to_disk: bool = True,
) -> CoverLetterResult | None:
    """Generate a cover letter for a job already in storage.

    Returns None if the job can't be found or no resume is loaded.
    On LLM failure, returns a result with empty ``letter`` so the
    caller can surface the error to the user.

    If ``save_to_disk`` is True (default), writes the letter to
    ``data/cover_letters/<company>-<title>-<short_id>.md``. Set
    False to just get the text back without persisting.
    """
    # Multiple rows can exist for the same job_id (discovery appends
    # a stub before the description fetch runs). Pick the row with
    # the longest description so post-hoc commands work even before
    # update_job_description has collapsed the duplicates.
    jobs: dict[str, Job] = {}
    for j in load_all(Job):
        existing = jobs.get(j.job_id)
        if existing is None or len(j.description or "") > len(existing.description or ""):
            jobs[j.job_id] = j
    job = jobs.get(job_id)
    if job is None:
        logger.warning("No job found with id %s", job_id)
        return None

    resume_label, resume_text = _pick_resume_for_job(
        job, resume_manager, preferred_label=preferred_resume,
    )
    if not resume_text:
        logger.warning("No resume available to generate cover letter for %s", job_id)
        return None

    writer = CoverLetterWriter(router)
    letter = await writer.generate(
        resume_text=resume_text,
        job_description=job.description,
        company_name=job.company or "the company",
        job_title=job.title or "the position",
    )

    file_path: Path | None = None
    if save_to_disk and letter:
        company_slug = _slugify(job.company or "company")
        title_slug = _slugify(job.title or "role")
        short_id = job_id[-8:] if len(job_id) > 8 else job_id
        filename = f"{company_slug}-{title_slug}-{short_id}.md"
        file_path = COVER_LETTERS_DIR / filename
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                f"# Cover Letter — {job.title or 'Role'}\n\n"
                f"**Company:** {job.company or '(unknown)'}\n"
                f"**Resume used:** {resume_label}\n"
                f"**Job ID:** {job.job_id}\n\n"
                f"---\n\n"
            )
            file_path.write_text(header + letter, encoding="utf-8")
            logger.info("Saved cover letter to %s", file_path)
        except OSError as exc:
            logger.warning("Could not save cover letter: %s", exc)
            file_path = None

    return CoverLetterResult(
        job_id=job_id,
        job_title=job.title,
        company=job.company,
        resume_label=resume_label,
        letter=letter,
        file_path=file_path,
    )
