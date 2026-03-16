"""Track and aggregate skills gaps found during applications."""

from collections import Counter

from auto_applier.resume.skills import find_missing_skills
from auto_applier.storage.models import SkillGap
from auto_applier.storage import repository


def record_gaps(job_id: str, gaps: list[SkillGap]) -> None:
    """Save skill gaps from a single application to storage."""
    for gap in gaps:
        gap.job_id = job_id
        repository.save(gap)


def record_skills_gaps_from_description(
    job_id: str,
    job_description: str,
    resume_skills: set[str],
) -> None:
    """Compare job description to resume and save any missing skills."""
    missing = find_missing_skills(resume_skills, job_description)
    for skill in missing:
        gap = SkillGap(
            job_id=job_id,
            field_label=skill,
            category="skill",
        )
        repository.save(gap)


def get_gap_summary() -> list[tuple[str, int, str]]:
    """Get all gaps sorted by frequency.

    Returns list of (field_label, count, category).
    """
    all_gaps = repository.load_all(SkillGap)

    # Count how often each gap appears
    counter = Counter()
    categories = {}
    for gap in all_gaps:
        key = gap.field_label.lower().strip()
        counter[key] += 1
        categories[key] = gap.category

    # Sort by frequency (most common first)
    return [
        (label, count, categories.get(label, "other"))
        for label, count in counter.most_common()
    ]
