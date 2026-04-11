"""Prompt templates for all LLM-powered features.

Each template is a :class:`PromptTemplate` instance with a *system* prompt
and a *template* string containing ``{placeholders}``.  Call
``template.format(**kwargs)`` to produce the filled user prompt.

Prompts are tuned for instruction-following JSON-capable local models
(Gemma 4 family by default). Key conventions:

- JSON prompts always state the exact schema and demand JSON-only output
  with no thinking preamble, markdown fences, or commentary. Ollama's
  ``format: "json"`` mode enforces this at the server level, but we
  reinforce it in the system prompt so Gemini/rule-based fallbacks behave.
- System prompts are short and directive. Gemma follows short system
  prompts more reliably than long ones.
- Field names in schemas use snake_case to match Python consumers.
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
        "You fill job application form fields. Given a resume and job "
        "description, answer the question concisely and professionally "
        "in the candidate's voice. If the resume lacks relevant info, "
        "reply with an empty string. Output the answer only — no "
        "preamble, no quotes, no markdown."
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
        "You evaluate job fit. Score how well the resume matches the "
        "job on a 1-10 integer scale. Respond ONLY with a JSON object "
        "matching this schema (no other text, no code fences):\n"
        '{"score": int 1-10, "explanation": str (<=2 sentences), '
        '"matched_skills": [str], "missing_skills": [str], '
        '"deal_breakers": [str]}'
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
        "Extract structured skills from this resume. Respond ONLY with a "
        "JSON object matching this schema (no other text):\n"
        '{"technical_skills": [{"name": str, "level": '
        '"beginner"|"intermediate"|"advanced"|"expert", "years": int}], '
        '"soft_skills": [str], "certifications": [str], "tools": [str]}'
    ),
    template="{resume_text}",
)

# ------------------------------------------------------------------
# Skill extraction -- job description
# ------------------------------------------------------------------

SKILL_EXTRACT_JD = PromptTemplate(
    system=(
        "Extract required skills from this job description. Respond ONLY "
        "with a JSON object matching this schema (no other text):\n"
        '{"required": [str], "preferred": [str], "experience_level": '
        '"entry"|"mid"|"senior"|"lead"|"principal"}'
    ),
    template="{job_description}",
)

# ------------------------------------------------------------------
# Resume bullet generation
# ------------------------------------------------------------------

RESUME_BULLET = PromptTemplate(
    system=(
        "Generate 2-3 resume bullet points for a confirmed skill. Be "
        "specific, use strong action verbs, and quantify impact where "
        "possible. Respond ONLY with a JSON array of strings (no other "
        "text): [\"bullet 1\", \"bullet 2\", ...]"
    ),
    template=(
        "Skill: {skill_name}\n"
        "Level: {skill_level}\n"
        "Context from user: {user_context}\n"
        "Existing resume context:\n{resume_excerpt}"
    ),
)

# ------------------------------------------------------------------
# Multi-dimensional resume scoring
# ------------------------------------------------------------------

SCORE_DIMENSIONS = PromptTemplate(
    system=(
        "You evaluate how well a resume matches a job along seven axes. "
        "Return a 0-10 float for each axis PLUS a one-sentence reason. "
        "Judge by skills and experience, NOT by job title. If the job "
        "description omits information for an axis (e.g. no compensation "
        "mentioned), score that axis 5.0 and say 'not specified'.\n\n"
        "Axes:\n"
        "- skills: do the required technical skills match the resume?\n"
        "- experience: does the candidate's years and relevance match?\n"
        "- seniority: does the candidate's level match the role level?\n"
        "- location: is remote / geography compatible?\n"
        "- compensation: if stated, does the salary range match expectations?\n"
        "- culture: does the company culture / values fit the candidate?\n"
        "- growth: does the role offer career trajectory / learning?\n\n"
        "Respond ONLY with this JSON schema (no other text, no code fences):\n"
        '{"skills": {"score": float, "reason": str}, '
        '"experience": {"score": float, "reason": str}, '
        '"seniority": {"score": float, "reason": str}, '
        '"location": {"score": float, "reason": str}, '
        '"compensation": {"score": float, "reason": str}, '
        '"culture": {"score": float, "reason": str}, '
        '"growth": {"score": float, "reason": str}, '
        '"matched_skills": [str], '
        '"missing_skills": [str], '
        '"summary": str}'
    ),
    template=(
        "Resume ({resume_label}):\n{resume_text}\n\n"
        "Job Description:\n{job_description}"
    ),
)


# ------------------------------------------------------------------
# Resume selection / scoring (legacy single-dimension)
# ------------------------------------------------------------------

RESUME_SELECT = PromptTemplate(
    system=(
        "Score how well this resume matches the job description on a 1-10 "
        "integer scale. Judge by skills and experience, NOT by job title. "
        "Respond ONLY with a JSON object matching this schema (no other "
        "text, no code fences):\n"
        '{"score": int 1-10, "explanation": str (<=2 sentences), '
        '"matched_skills": [str], "missing_skills": [str]}'
    ),
    template=(
        "Resume ({resume_label}):\n{resume_text}\n\n"
        "Job Description:\n{job_description}"
    ),
)

# ------------------------------------------------------------------
# Job archetype classification
# ------------------------------------------------------------------

CLASSIFY_JOB_ARCHETYPE = PromptTemplate(
    system=(
        "You classify job descriptions into one of a fixed set of "
        "archetype categories. Pick the single best match and give a "
        "confidence score 0.0-1.0 (1.0 = certain, 0.5 = could go "
        "either way, 0.0 = none of the listed archetypes apply). If "
        "no archetype fits well, return an empty string for archetype "
        "and a low confidence. Respond ONLY with this JSON schema "
        "(no other text, no code fences):\n"
        '{"archetype": str, "confidence": float, "reason": str}'
    ),
    template=(
        "Available archetypes:\n{archetype_menu}\n\n"
        "Job Description:\n{job_description}"
    ),
)


# ------------------------------------------------------------------
# STAR+Reflection interview story generation
# ------------------------------------------------------------------

STAR_STORIES = PromptTemplate(
    system=(
        "Generate 3 STAR+Reflection interview stories tailored to "
        "this specific job. Each story must draw from the candidate's "
        "actual resume experience — do not invent employers, projects, "
        "or outcomes. If the resume lacks material for a story, return "
        "fewer stories rather than fabricating.\n\n"
        "STAR+R structure: Situation, Task, Action, Result, Reflection "
        "(what the candidate learned and how they'd apply it here).\n\n"
        "Return ONLY this JSON (no preamble, no code fences):\n"
        '{"stories": [{"title": str, "question_prompt": str (the '
        'behavioral question this story answers, e.g. "Tell me about a '
        'time you disagreed with a teammate"), "situation": str, '
        '"task": str, "action": str, "result": str, "reflection": str}]}'
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}\n\n"
        "Company: {company_name}\n"
        "Job Title: {job_title}"
    ),
)


# ------------------------------------------------------------------
# Cover letter generation
# ------------------------------------------------------------------

COVER_LETTER = PromptTemplate(
    system=(
        "Write a concise, professional cover letter under 300 words. Map "
        "the candidate's specific strengths to the job's requirements — "
        "reference concrete skills and responsibilities. Avoid generic "
        "filler and clichés. Output plain prose only, no markdown."
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}\n\n"
        "Company: {company_name}\n\n"
        "Job Title: {job_title}"
    ),
)
