"""Prompt templates for all LLM-powered features.

Each template is a :class:`PromptTemplate` instance with a *system* prompt
and a *template* string containing ``{placeholders}``.  Call
``template.format(**kwargs)`` to produce the filled user prompt.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptTemplate:
    """A pair of system prompt and user-prompt template."""

    system: str
    template: str

    def format(self, **kwargs: str) -> str:
        """Return the user prompt with placeholders filled."""
        return self.template.format(**kwargs)


# ------------------------------------------------------------------
# Form filling
# ------------------------------------------------------------------

FORM_FILL = PromptTemplate(
    system=(
        "You help fill job application forms. Given a resume and job "
        "description, answer the question concisely and professionally. "
        "If the resume doesn't contain relevant info, respond with an "
        "empty string."
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}\n\n"
        "Question: {question}\n\n"
        "Answer:"
    ),
)

# ------------------------------------------------------------------
# Job scoring
# ------------------------------------------------------------------

JOB_SCORE = PromptTemplate(
    system=(
        "You evaluate job fit. Score 1-10 how well the resume matches "
        "the job. Respond with JSON: {score, explanation, matched_skills, "
        "missing_skills, deal_breakers}"
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}"
    ),
)

# ------------------------------------------------------------------
# Skill extraction -- resume
# ------------------------------------------------------------------

SKILL_EXTRACT_RESUME = PromptTemplate(
    system=(
        "Extract skills from this resume. Respond with JSON: "
        "{technical_skills: [{name, level, years}], soft_skills: [str], "
        "certifications: [str], tools: [str]}"
    ),
    template="{resume_text}",
)

# ------------------------------------------------------------------
# Skill extraction -- job description
# ------------------------------------------------------------------

SKILL_EXTRACT_JD = PromptTemplate(
    system=(
        "Extract required skills from this job description. Respond with "
        "JSON: {required: [str], preferred: [str], experience_level: str}"
    ),
    template="{job_description}",
)

# ------------------------------------------------------------------
# Resume bullet generation
# ------------------------------------------------------------------

RESUME_BULLET = PromptTemplate(
    system=(
        "Generate 2-3 resume bullet points for a confirmed skill. Be "
        "specific and use action verbs. Format as a JSON array of strings."
    ),
    template=(
        "Skill: {skill_name}\n"
        "Level: {skill_level}\n"
        "Context from user: {user_context}\n"
        "Existing resume context:\n{resume_excerpt}"
    ),
)

# ------------------------------------------------------------------
# Resume selection / scoring
# ------------------------------------------------------------------

RESUME_SELECT = PromptTemplate(
    system=(
        "Score how well this resume matches the job description. Respond "
        "with JSON: {score: 1-10, explanation: str, matched_skills: [str], "
        "missing_skills: [str]}"
    ),
    template=(
        "Resume ({resume_label}):\n{resume_text}\n\n"
        "Job Description:\n{job_description}"
    ),
)

# ------------------------------------------------------------------
# Cover letter generation
# ------------------------------------------------------------------

COVER_LETTER = PromptTemplate(
    system=(
        "Write a concise, professional cover letter (under 300 words). "
        "Focus on mapping the candidate's specific strengths to the job "
        "requirements. Do not be generic -- reference specific skills and "
        "requirements."
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}\n\n"
        "Company: {company_name}\n\n"
        "Job Title: {job_title}"
    ),
)
