"""LLM-powered skill extraction from resumes and job descriptions."""
from auto_applier.llm.router import LLMRouter
from auto_applier.llm.prompts import SKILL_EXTRACT_RESUME, SKILL_EXTRACT_JD


async def extract_resume_skills(router: LLMRouter, resume_text: str) -> dict:
    """Extract structured skills from resume text via LLM.

    Returns::

        {
            "technical_skills": [{"name": str, "level": str, "years": int}, ...],
            "soft_skills": [str, ...],
            "certifications": [str, ...],
            "tools": [str, ...]
        }
    """
    result = await router.complete_json(
        prompt=SKILL_EXTRACT_RESUME.format(resume_text=resume_text),
        system_prompt=SKILL_EXTRACT_RESUME.system,
    )
    # Ensure expected keys exist with defaults
    return {
        "technical_skills": result.get("technical_skills", []),
        "soft_skills": result.get("soft_skills", []),
        "certifications": result.get("certifications", []),
        "tools": result.get("tools", []),
    }


async def extract_jd_requirements(router: LLMRouter, job_description: str) -> dict:
    """Extract required/preferred skills from a job description via LLM.

    Returns::

        {
            "required": [str, ...],
            "preferred": [str, ...],
            "experience_level": str
        }
    """
    result = await router.complete_json(
        prompt=SKILL_EXTRACT_JD.format(job_description=job_description),
        system_prompt=SKILL_EXTRACT_JD.system,
    )
    return {
        "required": result.get("required", []),
        "preferred": result.get("preferred", []),
        "experience_level": result.get("experience_level", ""),
    }


def find_missing_skills(resume_skills: dict, jd_requirements: dict) -> list[str]:
    """Compare resume skills against JD requirements. Returns missing skill names.

    Flattens technical_skills, tools, and certifications from the resume
    into a lowercase set and checks each required skill against it.
    """
    # Flatten all resume skill names to lowercase set
    resume_names: set[str] = set()
    for skill in resume_skills.get("technical_skills", []):
        if isinstance(skill, dict):
            resume_names.add(skill.get("name", "").lower())
        else:
            resume_names.add(str(skill).lower())
    for skill in resume_skills.get("tools", []):
        resume_names.add(str(skill).lower())
    for cert in resume_skills.get("certifications", []):
        resume_names.add(str(cert).lower())

    # Check required skills against resume
    missing = []
    for req in jd_requirements.get("required", []):
        if req.lower() not in resume_names:
            missing.append(req)
    return missing
