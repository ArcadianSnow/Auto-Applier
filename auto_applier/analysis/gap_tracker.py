"""Skill gap recording and analysis."""
from dataclasses import dataclass

from auto_applier.analysis.title_archetype import classify_with_user_archetypes
from auto_applier.storage.models import Job, SkillGap
from auto_applier.storage.repository import save, load_all
from auto_applier.resume.skills import find_missing_skills


@dataclass
class GapContext:
    """A SkillGap enriched with its job's title, company, and archetype.

    Used by analytics views (`cli gaps`, `cli trends`) to group gaps
    by resume + title archetype without re-doing the join each time.
    """
    gap: SkillGap
    job_title: str
    company: str
    archetype: str  # "analyst", "engineer", "other", etc.


def gaps_with_context(
    user_archetypes: list[dict] | None = None,
) -> list[GapContext]:
    """Load all SkillGaps and enrich each with its job's title + archetype.

    Joins SkillGap (by job_id) to Job at query time. Single source of
    truth — the Job CSV holds the canonical title, and gaps inherit
    whatever Job says right now. No denormalization / drift risk.

    ``user_archetypes``: optional content of data/archetypes.json.
    When None, falls back to the regex-based classifier, which is
    zero-setup and works out of the box.
    """
    gaps = load_all(SkillGap)
    jobs = {j.job_id: j for j in load_all(Job)}

    enriched: list[GapContext] = []
    for g in gaps:
        job = jobs.get(g.job_id)
        if job is None:
            enriched.append(GapContext(
                gap=g,
                job_title="",
                company="",
                archetype="other",
            ))
            continue
        enriched.append(GapContext(
            gap=g,
            job_title=job.title,
            company=job.company,
            archetype=classify_with_user_archetypes(
                job.title, user_archetypes=user_archetypes,
            ),
        ))
    return enriched


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
