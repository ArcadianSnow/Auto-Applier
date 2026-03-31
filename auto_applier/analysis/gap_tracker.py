"""Skill gap recording and analysis."""
from auto_applier.storage.models import SkillGap
from auto_applier.storage.repository import save, load_all
from auto_applier.resume.skills import find_missing_skills


def record_gaps(job_id: str, gaps: list[SkillGap]) -> None:
    """Save a list of skill gaps from a job application."""
    for gap in gaps:
        save(gap)


async def record_skills_gaps_from_description(
    router, job_id: str, job_description: str, resume_text: str, resume_label: str, source: str
) -> list[SkillGap]:
    """Extract skill gaps by comparing job description against resume via LLM.

    Uses LLM-based skill extraction for both resume and JD, then finds the delta.
    Falls back to empty list on LLM failure.
    """
    from auto_applier.resume.skills import extract_resume_skills, extract_jd_requirements

    try:
        resume_skills = await extract_resume_skills(router, resume_text[:3000])
        jd_requirements = await extract_jd_requirements(router, job_description[:2000])
        missing = find_missing_skills(resume_skills, jd_requirements)

        gaps = []
        for skill_name in missing:
            gap = SkillGap(
                job_id=job_id,
                field_label=skill_name,
                category="skill",
                resume_label=resume_label,
                source=source,
            )
            save(gap)
            gaps.append(gap)
        return gaps
    except Exception:
        return []
