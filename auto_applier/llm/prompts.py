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
        "You fill job application form fields using facts from the "
        "candidate's resume. Rules:\n"
        "- Yes/no questions: answer with exactly 'Yes' or 'No'. Base "
        "it on what the resume actually shows. If the resume doesn't "
        "mention it at all, answer 'No'.\n"
        "- 'How many years of X?' questions: answer with a single "
        "integer. Count only years explicitly shown on the resume. "
        "If none are shown, answer '0'.\n"
        "- 'Do you have experience with X?' where X is a tool or "
        "skill: answer 'Yes' only if X (or a clearly equivalent tool) "
        "appears on the resume. Otherwise 'No'.\n"
        "- 'Have you previously worked for [company]?': check the "
        "resume's employment history. Answer 'Yes' only if that "
        "exact company appears in the work history.\n"
        "- Free-text questions: answer concisely in the candidate's "
        "voice, one or two sentences max.\n"
        "- If the resume genuinely has no information relevant to "
        "the question, reply with an empty string.\n"
        "Output the answer only — no preamble, no explanation, no "
        "quotes, no markdown."
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job description:\n{job_description}\n\n"
        "Company being applied to: {company_name}\n\n"
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
# Deep company research briefing
# ------------------------------------------------------------------

COMPANY_RESEARCH = PromptTemplate(
    system=(
        "Produce a concise interview-prep briefing about a company "
        "based on the provided context (career page text, news "
        "snippets, user notes, job descriptions). Be factual — never "
        "invent claims the source material doesn't support. If the "
        "source lacks information for a section, write 'not in "
        "source' rather than guessing.\n\n"
        "Return ONLY this JSON (no preamble, no code fences):\n"
        '{"what_they_do": str (2-3 sentences), '
        '"tech_stack_signals": [str], '
        '"culture_signals": [str], '
        '"red_flags": [str], '
        '"questions_to_ask": [str] (4-6 thoughtful questions to ask '
        'in an interview), '
        '"talking_points": [str] (things the candidate could mention '
        'to show genuine research)}'
    ),
    template=(
        "Company: {company_name}\n\n"
        "Source material:\n{source_material}"
    ),
)


# ------------------------------------------------------------------
# Per-JD tailored resume rewrite
# ------------------------------------------------------------------

TAILOR_RESUME = PromptTemplate(
    system=(
        "Rewrite a resume to emphasize the content most relevant to a "
        "specific job. Rules:\n"
        "- Never invent skills, employers, dates, certifications, or "
        "outcomes. Every bullet must exist in the original resume in "
        "some form.\n"
        "- You may reorder sections and bullets, rephrase wording, and "
        "promote relevant experience. You may NOT add anything new.\n"
        "- Inject keywords from the job description ONLY where they "
        "already apply to real experience.\n"
        "- Keep it ATS-friendly: plain text, short sentences, strong "
        "action verbs.\n\n"
        "Return ONLY this JSON (no preamble, no code fences):\n"
        '{"summary": str (2-3 sentence professional summary), '
        '"skills": [str], '
        '"experience": [{"title": str, "company": str, "dates": str, '
        '"bullets": [str]}], '
        '"education": [{"school": str, "degree": str, "year": str}]}'
    ),
    template=(
        "Original resume:\n{resume_text}\n\n"
        "Target job:\n{job_title} at {company_name}\n\n"
        "Job description:\n{job_description}"
    ),
)


# ------------------------------------------------------------------
# LinkedIn outreach / connection message
# ------------------------------------------------------------------

OUTREACH_MESSAGE = PromptTemplate(
    system=(
        "Write a short, warm LinkedIn connection-request message (under "
        "280 characters — LinkedIn's hard limit). The candidate is "
        "reaching out to a recruiter or hiring manager at a specific "
        "company about a specific job. Reference the role by title, "
        "mention one concrete skill or experience from the resume that "
        "maps to the role, and ask a genuine question or express "
        "specific interest. Do NOT use the word 'synergy', 'circle "
        "back', 'touch base', or any corporate cliché. Output the "
        "message body only — no 'Hi {{name}}' header, no signature."
    ),
    template=(
        "Resume highlights:\n{resume_text}\n\n"
        "Job:\n{job_title} at {company_name}\n\n"
        "Job description excerpt:\n{job_description}"
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
