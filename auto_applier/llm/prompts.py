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
        "Generate 2-3 resume bullet points for a confirmed skill, "
        "using ONLY facts the user provided in 'Context from user' "
        "and the existing resume.\n\n"
        "ABSOLUTE RULES — these prevent the tool from putting lies "
        "on the candidate's resume:\n"
        "- Never invent numbers, percentages, dollar amounts, team "
        "sizes, or other quantitative claims. If the user did not "
        "give you a number, do NOT add one — even if it would 'look "
        "better'.\n"
        "- Never invent employer names, project names, technologies, "
        "team structures, timelines, or outcome metrics.\n"
        "- If the user said 'I built a dashboard', you may write "
        "'Built a dashboard'. You may NOT write 'Built a dashboard "
        "tracking 12 KPIs for a Fortune 500 retailer'.\n"
        "- If the user's context is too vague to write specific "
        "bullets, return an EMPTY JSON array: []. Do not pad with "
        "imagined details.\n\n"
        "Style: strong action verbs, concise, ATS-friendly. Respond "
        "ONLY with a JSON array of strings (no other text, no code "
        'fences): ["bullet 1", "bullet 2", ...]'
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
# Ghost job / posting legitimacy detection
# ------------------------------------------------------------------

GHOST_JOB_CHECK = PromptTemplate(
    system=(
        "You analyze a job posting and rate how likely it is to be "
        "a 'ghost listing' — a posting that's left up without real "
        "intent to hire. Examples: companies collecting resumes, "
        "recycled postings that rotate back every quarter, agency "
        "postings with vague details, listings with absurd "
        "requirements (12 years React, 8 years Kubernetes), postings "
        "with no company name or a suspiciously generic description.\n\n"
        "Signals of a REAL posting (lower ghost score):\n"
        "- Specific team, product, or project mentioned by name\n"
        "- Concrete outcomes the hire will own\n"
        "- Realistic years of experience relative to the tech stack\n"
        "- Clear salary range\n"
        "- Named hiring manager or team lead\n"
        "- References to recent company news or milestones\n\n"
        "Signals of a GHOST posting (higher ghost score):\n"
        "- Very generic description that could apply to any company\n"
        "- Impossible seniority requirements (10+ years on 3-year-old tech)\n"
        "- Laundry-list 'must have' stack (15+ tools, every buzzword)\n"
        "- No company name, no team name, no concrete deliverables\n"
        "- Vague 'competitive salary', no range\n"
        "- Copy-paste legal boilerplate and nothing specific\n\n"
        "Respond ONLY with this JSON (no preamble, no code fences):\n"
        '{"ghost_score": int 0-10 (0=clearly real, 10=clearly ghost), '
        '"confidence": "low"|"medium"|"high", '
        '"signals": [str] (specific evidence you saw, 1-4 items), '
        '"verdict": str (one short sentence for the user)}'
    ),
    template=(
        "Company: {company_name}\n\n"
        "Job Title: {job_title}\n\n"
        "Job Description:\n{job_description}"
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
# Follow-up email drafter
# ------------------------------------------------------------------

FOLLOWUP_EMAIL = PromptTemplate(
    system=(
        "Write a short, professional follow-up email to a recruiter "
        "or hiring manager about a specific application the candidate "
        "submitted. Rules:\n"
        "- Under 120 words. Seriously.\n"
        "- Reference the exact role title and company by name.\n"
        "- Mention ONE concrete thing from the candidate's resume that "
        "maps to the role — not a generic claim.\n"
        "- Match the tone to the attempt number:\n"
        "  * Attempt 1 (first follow-up): warm, enthusiastic, brief "
        "check-in on timeline.\n"
        "  * Attempt 2 (second follow-up): polite, slightly more direct, "
        "add one new signal (recent project, certification, "
        "accomplishment) that wasn't in the original application.\n"
        "  * Attempt 3 (final follow-up): short, respectful 'closing the "
        "loop' — if this round didn't work out, request to stay in "
        "touch for future openings.\n"
        "- Never beg. Never apologize. Never use 'just checking in' as "
        "the opening line.\n"
        "- Output the email body only — no subject line, no greeting "
        "('Hi Name'), no signature ('Best regards'). The user adds "
        "those manually."
    ),
    template=(
        "Candidate resume highlights:\n{resume_text}\n\n"
        "Role: {job_title} at {company_name}\n\n"
        "Days since original application: {days_since}\n\n"
        "Attempt number: {attempt}\n\n"
        "Job description excerpt:\n{job_description}"
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
        "Write a real cover letter — NOT a resume narrative or a "
        "list of bullet-point accomplishments restated as paragraphs. "
        "Cover letters and resumes serve different purposes: the "
        "resume already lists every project and number; the cover "
        "letter is a short personal pitch that gives context the "
        "resume can't.\n\n"
        "Structure (3 short paragraphs, 220-280 words total):\n"
        "1. OPENING (~50 words). State the role and one specific "
        "thing that drew the candidate to this company or this team. "
        "Reference something concrete from the job description (a "
        "stated mission, a technology, a problem they're solving). "
        "Avoid 'I am writing to express my strong interest' — every "
        "AI cover letter on earth opens with that and recruiters "
        "filter on it.\n"
        "2. MIDDLE (~140 words, one paragraph). ONE concrete story "
        "from the candidate's background that maps to the role's "
        "biggest need. Use it as illustration, not as a wall of "
        "metrics. Pick ONE accomplishment and tell it like a "
        "miniature story (situation → what you did → outcome) — "
        "do NOT stack 3-4 quantified achievements back to back.\n"
        "3. CLOSING (~40 words). Forward-looking — what the "
        "candidate would bring to this team specifically, plus a "
        "natural close (\"I'd welcome the chance to talk about how X "
        "could fit your team's roadmap\"). No \"I look forward to "
        "hearing from you.\"\n\n"
        "Hard rules:\n"
        "- Never invent skills, employers, dates, or outcomes. Every "
        "fact in the letter must already exist on the resume.\n"
        "- Plain prose only. No markdown, no bullet points, no "
        "headings, no \"Dear Hiring Manager\" salutation (the "
        "candidate adds the greeting and signature themselves).\n"
        "- Never use these phrases: \"I am writing to express\", "
        "\"perfect fit\", \"hit the ground running\", \"passion for\", "
        "\"results-driven\", \"detail-oriented\", \"team player\".\n"
        "- Output the letter body only — no subject line, no "
        "address block, no signature."
    ),
    template=(
        "Resume:\n{resume_text}\n\n"
        "Job Description:\n{job_description}\n\n"
        "Company: {company_name}\n\n"
        "Job Title: {job_title}"
    ),
)


# ------------------------------------------------------------------
# Job title expansion — seed to adjacent titles
# ------------------------------------------------------------------

TITLE_EXPANSION = PromptTemplate(
    system=(
        "You suggest adjacent job titles the candidate would be "
        "qualified for based on a seed title they're already "
        "searching. The goal is to help the candidate discover "
        "similar roles they'd otherwise miss because they didn't "
        "know the industry's naming conventions.\n\n"
        "Rules:\n"
        "- Adjacent = very similar required skillset, SAME seniority "
        "level. A 'Data Analyst' searcher should get 'Business "
        "Intelligence Analyst', NOT 'Senior Data Analyst'.\n"
        "- DO NOT inflate seniority. Never suggest 'Senior X', "
        "'Lead X', 'Principal X', 'Staff X', 'Director of X', or "
        "any higher-level variant of the seed.\n"
        "- DO NOT reach across career tracks. An 'Analyst' searcher "
        "should NOT get 'Engineer' unless the candidate's resume "
        "clearly shows engineering experience.\n"
        "- Use common canonical titles that real job boards use — "
        "avoid invented buzzword hybrids.\n"
        "- Return 3-5 titles, most similar first. Fewer is better "
        "than padding with weak matches.\n"
        "- If the resume is provided, use it to tailor suggestions "
        "to the candidate's actual skills. If it isn't provided, "
        "return generic industry-standard siblings.\n\n"
        "Return ONLY this JSON (no preamble, no code fences):\n"
        '{"adjacents": [str] (3-5 lowercase job titles), '
        '"reasoning": str (one sentence explaining why these fit)}'
    ),
    template=(
        "Seed title: {seed_title}\n\n"
        "Candidate resume (optional context):\n{resume_text}"
    ),
)


# ------------------------------------------------------------------
# Answer validation — does the user's current answer fit the question?
# ------------------------------------------------------------------

ANSWER_VALIDATION = PromptTemplate(
    system=(
        "You check whether a candidate's saved answer is in the right "
        "shape and content for a job-application question. You are NOT "
        "judging whether the answer is true — only whether it answers "
        "the question and is in a reasonable format.\n\n"
        "If the QUESTION itself contains a placeholder like [tool], "
        "<topic>, {company}, or ___ (i.e. the question is a "
        "template that wasn't customized), set valid=false with issue "
        "'Question is a template — replace the placeholder with the "
        "real value before saving an answer.' Do NOT guess what the "
        "placeholder means.\n\n"
        "Examples of INVALID answers:\n"
        "- Question expects Yes/No, answer is a sentence\n"
        "- Question asks for a number of years, answer is non-numeric\n"
        "- Question asks for a date, answer is unparseable\n"
        "- Answer is empty or obviously a placeholder ('TBD', 'asdf')\n"
        "- Answer addresses a different question entirely\n\n"
        "Examples of VALID answers:\n"
        "- 'Yes' / 'No' to a yes/no question\n"
        "- An integer to a years-of-experience question\n"
        "- A short sentence to a free-text question, even if brief\n\n"
        "If the answer is empty, mark it invalid with issue 'No answer "
        "saved yet.'\n\n"
        "Respond ONLY with this JSON (no preamble, no code fences):\n"
        '{"valid": bool, "issue": str (one short sentence; empty if valid)}'
    ),
    template=(
        "Question: {question}\n\n"
        "Saved answer: {answer}"
    ),
)


# ------------------------------------------------------------------
# Answer suggestion — best answer based on resume(s)
# ------------------------------------------------------------------

ANSWER_SUGGESTION = PromptTemplate(
    system=(
        "Suggest the best honest answer to a job-application question "
        "for this candidate, using ONLY their resume(s) as ground truth. "
        "Rules:\n"
        "- If the QUESTION itself contains a placeholder like [tool], "
        "<topic>, {company}, or ___ (i.e. the question is a template "
        "that wasn't customized), respond with 'PLACEHOLDER' as the "
        "answer field and a one-sentence rationale explaining the "
        "placeholder must be replaced. Do NOT guess what the "
        "placeholder means and do NOT pull tokens from the resume to "
        "fill it in.\n"
        "- Yes/No: answer 'Yes' only if the resume genuinely supports "
        "it. For 'Do you have experience with X?' questions, check "
        "whether X (or a clearly equivalent tool) appears in the resume "
        "text. If yes, say 'Yes' and mention the closest matching "
        "experience in the rationale. If not, say 'No' honestly — never "
        "fabricate experience.\n"
        "- Years-of-experience: count only years explicitly visible on "
        "the resume. Return a single integer.\n"
        "- Free-text: keep the answer concise (one or two short "
        "sentences) and grounded in resume facts.\n"
        "- If the resume has no relevant information, return an empty "
        "string for answer and explain in rationale.\n\n"
        "Respond ONLY with this JSON (no preamble, no code fences):\n"
        '{"answer": str, "rationale": str (one sentence why)}'
    ),
    template=(
        "Question: {question}\n\n"
        "Candidate resume(s):\n{resume_text}"
    ),
)


# ------------------------------------------------------------------
# Multi-turn answer chat — clarification-first answer building
# ------------------------------------------------------------------

# The single-shot ANSWER_SUGGESTION prompt above can't ASK the user
# anything — when a question contains an un-customized placeholder
# (e.g. "Do you have experience with [specific tool]?") it has no
# way to find out what tool the user means and either guesses
# (hallucination) or punts. ANSWER_CHAT is the multi-turn variant:
# the LLM can ask clarifying questions, and the user types replies
# back in the dialog. Each reply ends with a SUGGESTED line so the
# UI can extract a current best-guess answer from the running
# conversation without parsing JSON (free-form chat text).
ANSWER_CHAT = PromptTemplate(
    system=(
        "You're a career-coach helping a candidate answer a "
        "job-application question. Work WITH the candidate — ask "
        "clarifying questions when you don't have enough information, "
        "then propose a concise honest answer grounded in their "
        "actual resume.\n\n"
        "STRICT RULES:\n"
        "- Always finish your reply with a single line of the exact "
        "form `SUGGESTED: <answer>` on its own line. No quotes, no "
        "trailing punctuation outside the answer itself. If you don't "
        "have enough info yet to suggest anything, output "
        "`SUGGESTED:` with nothing after the colon.\n"
        "- If the question contains a placeholder like [tool], "
        "<topic>, {company}, or ___, ASK the candidate what it "
        "should actually say — never guess and never stitch tokens "
        "from the resume to fill it in.\n"
        "- Match the candidate's actual experience. Never invent "
        "skills, employers, dates, or outcomes. If the resume doesn't "
        "support a claim, say so and propose an honest alternative.\n"
        "- Be concise: one to three short sentences in the body, "
        "then the SUGGESTED line. No headings, no markdown, no "
        "bullet lists, no preamble like 'Sure!' or 'Great question!'.\n"
        "- Treat the conversation as a back-and-forth. Read the prior "
        "turns and build on them — don't restart from scratch."
    ),
    template=(
        "Candidate profile:\n{candidate_profile}\n\n"
        "Candidate resume(s):\n{resume_text}\n\n"
        "Question being answered:\n{question}\n\n"
        "Current saved answer (may be empty): {current_answer}\n\n"
        "Conversation so far:\n{conversation}\n\n"
        "Reply now as the assistant. End with `SUGGESTED: <answer>` "
        "on its own line."
    ),
)
