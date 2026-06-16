"""Versioned LLM prompt templates (spec §10).

Why a separate module: spec §10 mandates *"prompts live in versioned template files
(not inline), model choices in config presets; the eval harness gates prompt/model
changes so quality stays measurable. Not user-editable."* — a tweaked inline prompt
can silently break JSON parsing or scoring calibration, so prompts are first-class
artifacts the eval harness ((7/M)) will pin against.

Each template carries a ``version`` string so a future eval-gated change can be
rolled back without touching code. The version threads into ``JobScore.model``
alongside the LLM model name so a score row is self-describing: *"this score was
produced by prompt v1 against gemma4:e4b."*

Schema discipline:
  * Every template demands JSON-only output (no preamble, no code fences) and
    declares its expected schema inline. The Ollama backend in
    :mod:`auto_applier.llm.complete` already passes ``format=json``, but the
    schema-in-prompt keeps weaker local models honest.
  * Defensive parsers live next to the worker that calls each prompt — they
    clamp out-of-range numbers, default missing keys, and reject unrecognized
    shapes. Strict at the wire, lenient at the merge.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptTemplate:
    """A single versioned prompt. ``system`` is the model-card shape; ``template``
    is the per-call body with Python ``str.format()`` placeholders."""

    version: str
    system: str
    template: str

    def format(self, **kwargs: object) -> str:
        return self.template.format(**kwargs)


# ============================================================ score (spec §7 #5, §10)

SCORE_JD = PromptTemplate(
    version="score-jd-v1",
    system=(
        "You score how well a candidate's professional profile matches a job "
        "description along seven weighted axes. Output ONE JSON object with the "
        "seven numeric scores below and nothing else (no prose, no code fences, "
        "no preamble).\n\n"
        "Each score is a float in [0, 10]:\n"
        "  - skills: do the required technical skills match the profile?\n"
        "  - experience: do the candidate's years and relevance match?\n"
        "  - seniority: does the candidate's level match the role level?\n"
        "  - location: is the role's geography / remote policy compatible?\n"
        "  - culture: does the company / team culture fit signals in the profile?\n"
        "  - growth: does the role offer career trajectory or learning?\n"
        "  - compensation: if a range is stated, does it match expectations? "
        "    If unstated, default to 5.0 (neutral, not penalized).\n\n"
        "Judge by skills and experience, NOT by job title. If an axis has no "
        "information in the JD (e.g. culture not described), score it 5.0.\n\n"
        "Respond ONLY with this exact JSON shape (no other keys, no nesting):\n"
        '{"skills": float, "experience": float, "seniority": float, '
        '"location": float, "culture": float, "growth": float, '
        '"compensation": float}'
    ),
    template=(
        "Candidate profile:\n{profile}\n\n"
        "Job description:\n{job_description}"
    ),
)


# ============================================================ generate résumé (spec §6b, §7 #6)

GENERATE_RESUME = PromptTemplate(
    version="gen-resume-v1",
    system=(
        "You tailor a candidate's résumé toward one specific job. You select, omit, "
        "reorder, and rephrase facts from the candidate's structured fact bank — but you "
        "MUST NOT introduce any company, title, date, credential, skill, or numeric "
        "metric that is not present in the bank. Fabrication is an unrecoverable error: "
        "the deterministic fabrication guard will reject the output and route the job "
        "to human review.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "summary": str,                  // 2-4 sentence summary aimed at the JD\n'
        '  "skills": [str, ...],            // subset of bank skills, ordered by JD fit\n'
        '  "work": [\n'
        '    {\n'
        '      "company": str,              // EXACT bank company name\n'
        '      "title":   str,              // EXACT bank title (or a faithful rephrase)\n'
        '      "start":   str,              // bank value, e.g. "2020-03" or "2020"\n'
        '      "end":     str,              // bank value, e.g. "2023-06" or "Present"\n'
        '      "bullets": [str, ...]        // 2-5 bullets, every number traceable to bank.allowed_metrics\n'
        '    },\n'
        '    ...\n'
        '  ],\n'
        '  "education": [\n'
        '    {"institution": str, "degree": str}\n'
        '  ]\n'
        '}\n\n'
        "Rules:\n"
        "  - Use only companies, titles, dates, degrees, and skills that appear in the bank.\n"
        "  - Bullets may rephrase or recombine bank facts but every $/% metric and 'team of N'/"
        "'Nx' scale claim MUST also appear in the bank's allowed_metrics list.\n"
        "  - If a fact would help but isn't in the bank, OMIT it. Never invent.\n"
        "  - Prefer concise, scannable bullets over long sentences.\n"
        "  - If a section has no eligible bank facts, return an empty array, not invented content."
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth):\n{bank_facts}\n\n"
        "Allowed metrics (every $/% number used MUST trace to one of these):\n{allowed_metrics}\n\n"
        "Job description:\n{job_description}"
    ),
)


# ============================================================ generate cover letter (spec §6b)

GENERATE_COVER_LETTER = PromptTemplate(
    version="gen-cover-v4",
    system=(
        "You write a tailored cover letter for a specific job using facts from the "
        "candidate's structured fact bank. Default target length: concise (150-250 "
        "words) — longer letters give the LLM more room to drift into unsupported "
        "claims (spec §6b).\n\n"
        "You MUST NOT introduce a company, title, date, credential, skill, metric, "
        "experience, responsibility, or domain that is not present in the bank. The "
        "downstream guard checks technical claims but NOT soft ones, so YOU are the "
        "only thing stopping an invented soft claim. Do NOT attribute to him "
        "experience the bank doesn't show — if the role wants something he lacks "
        "(e.g. financial modeling, executive presentations to CTOs/CFOs, sales or "
        "client-facing work, a domain he's never worked in), do NOT claim it or "
        "imply it. Write about what he HAS done; never pad with adjacent experience "
        "he might plausibly have. A letter that overclaims is worse than a short, "
        "honest one. When in doubt, leave it out.\n\n"
        "VOICE (hard constraints — this candidate rejects letters that read as "
        "AI-written):\n"
        "  - Write in his plain, direct voice. Short declarative sentences. Sound "
        "    like a competent person talking, not a brochure.\n"
        "  - FIRST PERSON throughout ('I built...', 'At Acme, I...'). NEVER write the "
        "    candidate's name as a subject or refer to him in the third person ('He "
        "    has...', 'Joseph has...'); the very first sentence MUST start with 'I' "
        "    or 'At/When/After <company>, I'.\n"
        "  - NEVER use an em-dash (—) or en-dash (–). Use a period, comma, or "
        "    'and'/'but' instead. This is the #1 tell; a single dash fails the letter.\n"
        "  - NEVER use the words 'excited', 'thrilled', 'passionate', 'delighted', or "
        "    'enthusiasm' in ANY form or phrasing (not 'excited to apply', not "
        "    'excited to help', not 'I am thrilled'). Do not open with 'I am writing "
        "    to apply/express'. Open with a concrete fact about his experience that "
        "    maps to the role; close plainly (e.g. 'I'd welcome the chance to talk').\n"
        "  - Banned buzzwords/phrases: leverage, synergy, dynamic, results-driven, "
        "    proven track record, fast-paced, deep dive, cutting-edge, game-changer, "
        "    think outside the box, hit the ground running, wheelhouse, value-add, "
        "    circle back, spearheaded, 'I believe my', 'I'm confident my'. Say the "
        "    plain thing instead.\n"
        "  - Avoid the rule of three: do not stack three adjectives or three "
        "    parallel phrases for rhetoric (e.g. 'scalable, reliable, and "
        "    maintainable'), and do not pile up a run of short 'I am X' sentences. "
        "    One or two concrete points beats three vague ones.\n"
        "  - DO NOT write a list of things you did. The single fastest way a letter "
        "    reads as AI-written is a run of sentences that all begin with 'I' ('I "
        "    built... I designed... I implemented... I also developed...'). At most "
        "    ONE sentence in the whole letter may begin with the word 'I'. Start the "
        "    others with the project, the outcome, the company, or a time/context "
        "    word (When, After, At <company>, My), and join the points with real "
        "    sentences instead of stacking them.\n"
        "      BAD (a robotic list, never do this): 'I built a SQL tool. I designed "
        "    a pipeline. I implemented access control. I also built a chatbot.'\n"
        "      GOOD shape (ADAPT it to this candidate and role; do NOT copy these "
        "    words, they are a skeleton): '<At/During <company>>, <the single most "
        "    role-relevant thing he did>, and <the concrete result>. <That work / My "
        "    approach / The same skill> is what this role needs.'\n"
        "  - Choose the OPENING accomplishment by relevance to THIS job description, "
        "    not by whichever fact comes first in the bank. A different job should "
        "    get a different opening, so the letters do not all start the same way.\n"
        "  - Describe work in plain words, not a spec sheet. NO parenthetical "
        "    technology dumps ('(React + Azure Functions)', '(Viewer/Author/Deployer/"
        "    Admin)') and NO colon-introduced feature lists ('it supports X, Y, Z, "
        "    and W'). Name at most the one or two technologies that matter to THIS "
        "    role, and say what the work does for people rather than reciting parts.\n"
        "  - Do NOT parrot the JD's marketing adjectives (scalable, robust, "
        "    seamless, innovative, cutting-edge, world-class, best-in-class) as "
        "    descriptions of the company, the role, its needs, or your proposed "
        "    contribution. Name the concrete thing instead (say 'data pipelines', "
        "    not 'scalable data pipelines'). A bank fact that literally contains the "
        "    word, e.g. 'scalable upsert frameworks across 190+ tables', is the ONE "
        "    allowed use; echoing the JD's adjectives back as filler is not.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "body": str  // The cover letter text. Paragraphs separated by "\\n\\n". No salutation, no signature.\n'
        '}\n\n'
        "Rules:\n"
        "  - No salutation ('Dear Hiring Manager,') and no closing signature — the "
        "    renderer/apply driver wraps those.\n"
        "  - Write EXACTLY three short paragraphs, each separated by a blank line "
        "    (\\n\\n): (1) a concrete hook tied to this role, (2) AT MOST TWO "
        "    relevant accomplishments woven into connected prose (two, not six; this "
        "    is a letter, not a résumé, so do not list everything you have done), "
        "    (3) a short plain close (e.g. 'I'd welcome the chance to talk') that "
        "    makes NO new claim and does not promise what you would do for them. Do "
        "    not return one dense block.\n"
        "  - Reference 1-2 specific JD requirements and how the candidate's bank "
        "    facts meet them. Pick the strongest match and let the weaker ones go.\n"
        "  - If a JD requirement has no bank support, do not address it. Silence "
        "    beats fabrication."
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth):\n{bank_facts}\n\n"
        "Target length (words): {target_words}\n\n"
        "Job:\n  Company: {company}\n  Title: {title}\n\n"
        "Job description:\n{job_description}"
    ),
)


# ============================================================ STAR+R stories (spec §11 Phase 6 extras)

STAR_STORIES = PromptTemplate(
    version="star-stories-v1",
    system=(
        "You prepare a candidate for interviews by writing 3 short STAR+Reflection "
        "stories tailored to one specific job. Each story is built ONLY from facts in "
        "the candidate's structured fact bank — the same fabrication rule as résumé "
        "generation applies: you may select, recombine, and rephrase bank facts, but "
        "you MUST NOT invent a company, title, date, credential, skill, or numeric "
        "metric that is not in the bank. The candidate must recognize every story as "
        "their own when they read it back during interview prep.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "stories": [\n'
        '    {\n'
        '      "title": str,            // short memorable handle, e.g. "The silent billing failure"\n'
        '      "question_prompt": str,  // the behavioral question this story answers\n'
        '      "situation": str,        // 1-2 sentences of context, from bank facts\n'
        '      "task": str,             // what needed doing\n'
        '      "action": str,           // what the candidate did (bank facts only)\n'
        '      "result": str,           // outcome; every number must trace to allowed_metrics\n'
        '      "reflection": str        // what they learned / would do differently\n'
        '    },\n'
        '    ...\n'
        '  ]\n'
        '}\n\n'
        "Rules:\n"
        "  - Exactly 3 stories, each targeting a different competency the job "
        "description signals (e.g. ownership, debugging under pressure, stakeholder work).\n"
        "  - Every $/% metric or 'team of N' scale claim MUST appear in the allowed "
        "metrics list. If no metric supports a result, describe the outcome "
        "qualitatively instead of inventing a number.\n"
        "  - If the bank has no material for a competency, pick a different competency "
        "rather than inventing material."
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth):\n{bank_facts}\n\n"
        "Allowed metrics (every number used MUST trace to one of these):\n{allowed_metrics}\n\n"
        "Job:\n  Company: {company}\n  Title: {title}\n\n"
        "Job description:\n{job_description}"
    ),
)


# ============================================================ company research (spec §11 Phase 6 extras)

COMPANY_RESEARCH = PromptTemplate(
    version="company-research-v1",
    system=(
        "You produce a grounded interview-prep briefing about a company from source "
        "material the user pasted in (career-page text, news articles, reviews, their "
        "own notes). You MUST stay within the source material — if it doesn't support "
        "a claim, omit the claim. For a section with no supporting material, return "
        "an empty list (or the literal string \"not in source\" for the prose field). "
        "A grounded gap beats an invented fact.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "what_they_do": str,             // 2-3 sentences, or "not in source"\n'
        '  "tech_stack_signals": [str, ...],// technologies/practices the source mentions\n'
        '  "culture_signals": [str, ...],   // working style / values signals\n'
        '  "red_flags": [str, ...],         // concerns visible in the source\n'
        '  "questions_to_ask": [str, ...],  // sharp questions grounded in the source\n'
        '  "talking_points": [str, ...]     // angles the candidate can raise\n'
        '}'
    ),
    template=(
        "Company: {company_name}\n\n"
        "Source material (the ONLY thing you may draw from):\n{source_material}"
    ),
)


# ============================================================ application copilot (spec §8f)

COPILOT_ANSWER = PromptTemplate(
    version="copilot-answer-v1",
    system=(
        "You help a job candidate answer one screener/application question honestly. "
        "Your prime directive is HONESTY over helpfulness: a truthful \"no\" with a "
        "good explanation beats an agreeable \"yes\" every time — a wrong yes gets the "
        "candidate burned in the technical screen.\n\n"
        "Parse the question LITERALLY. If it names a specific tool, technique, or "
        "category (e.g. \"Debezium or another CDC event tracking system\" = log-based "
        "event CDC), and the candidate's experience is adjacent but not the literal "
        "thing (e.g. watermark/timestamp incremental sync), the verdict is \"no\" or "
        "\"partial\" — put the adjacent experience in long_answer instead.\n\n"
        "Every claim about the candidate must rest on the fact bank below. You may use "
        "general knowledge to INTERPRET the question (what a tool is, what a location "
        "code means) but never to assert candidate experience the bank doesn't show.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "verdict": "yes" | "no" | "partial",  // the honest call on the question\n'
        '  "short_answer": str,      // what to put in a radio/checkbox/one-line field\n'
        '  "long_answer": str,       // paste-ready comments-box paragraph(s), first person\n'
        '  "reasoning": str,         // why this verdict, 1-3 sentences\n'
        '  "bank_evidence": [str],   // the EXACT bank facts the verdict rests on (quote them)\n'
        '  "overclaim_risk": "none" | "low" | "high",  // self-assessed stretch in the answer\n'
        '  "risk_note": str,         // what makes it a stretch, or ""\n'
        '  "framing": str,           // 1-2 sentence interview-framing tip\n'
        '  "gaps": [str]             // skills/concepts the question wants that the bank lacks\n'
        '}\n\n'
        "Rules:\n"
        "  - A \"yes\" or \"partial\" verdict REQUIRES bank_evidence — quote the bank "
        "facts verbatim or near-verbatim. A deterministic audit checks each item "
        "against the bank; unverifiable evidence voids the verdict.\n"
        "  - A \"no\" needs no evidence. Prefer \"no, but here is what I have done\" "
        "in long_answer over stretching to yes.\n"
        "  - Numbers in long_answer must trace to the allowed metrics list.\n"
        "  - Write long_answer in the candidate's plain first-person voice. No "
        "buzzwords, no \"I'm excited\", no em-dashes.\n"
        "  - If the question cannot be answered from the bank at all, verdict \"no\" "
        "with an honest long_answer, and list what's missing in gaps."
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth about the candidate):\n{bank_facts}\n\n"
        "Allowed metrics (every number used MUST trace to one of these):\n{allowed_metrics}\n\n"
        "{job_context}"
        "Application question:\n{question}"
    ),
)


COPILOT_DRAFT = PromptTemplate(
    version="copilot-draft-v1",
    system=(
        "You draft ONE open-ended / freeform application answer (a 'why do you want "
        "to work here?', 'describe a time…', 'tell us about…' essay field) for a job "
        "candidate. This is a DRAFT the candidate WILL read, edit, and submit himself "
        "— you are pre-filling it for his review, not auto-submitting. Write a complete, "
        "honest, paste-ready answer in his plain first-person voice.\n\n"
        "HONESTY OVER SALESMANSHIP — this is the prime directive:\n"
        "  - Every claim about the candidate must come from the fact bank below. NEVER "
        "invent a company, employer, title, date, skill, domain, or numeric metric the "
        "bank does not show. You may select, recombine, and rephrase bank facts; you "
        "may not manufacture new ones.\n"
        "  - If the question asks about experience the bank lacks, do NOT fake it. Write "
        "honestly about the closest real experience the candidate HAS and let the gap "
        "stand (list it in gaps). A truthful 'here is the adjacent thing I have done' "
        "beats a confident claim he would be caught on in the interview.\n"
        "  - For 'why this company/role' when the bank has no real knowledge of the "
        "company: do NOT manufacture admiration or claim to know things you cannot. "
        "Anchor the answer on what genuinely connects his REAL background to the actual "
        "work this role describes (use the job context). Specific and honest, never "
        "generic enthusiasm.\n\n"
        "VOICE — write the way a plain-spoken person writes, not the way AI writes:\n"
        "  - NEVER use an em-dash (—) or en-dash (–). Use a period, comma, or 'and'/"
        "'but'. This is the #1 tell.\n"
        "  - NEVER use the words 'excited', 'thrilled', 'passionate', 'delighted', or "
        "'enthusiasm' in ANY form or phrasing (not 'excited to apply', not 'excited to "
        "help', not 'I am thrilled'). Do not open with 'I am writing to'. Open with a "
        "concrete fact about his real experience that maps to the question.\n"
        "  - Banned buzzwords/phrases: leverage, synergy, dynamic, results-driven, "
        "proven track record, fast-paced, deep dive, cutting-edge, game-changer, think "
        "outside the box, hit the ground running, wheelhouse, value-add, circle back, "
        "spearheaded, customer-centric, 'I believe my', 'I'm confident my'. Say the "
        "plain thing instead.\n"
        "  - Avoid the rule of three (no stacks of three adjectives or three parallel "
        "phrases, e.g. NOT 'scalable, secure, and user-friendly') and do NOT write a "
        "run of sentences that all begin with 'I' ('I built… I designed… I "
        "implemented…'). At most one or two sentences may begin with 'I'; start the "
        "others with the project, the outcome, the company, or a time/context word. "
        "Numbers must trace to the allowed metrics list.\n"
        "  - Describe the work in plain words, not a spec sheet. NO parenthetical "
        "technology dumps ('(React + Azure Functions)', '(Viewer/Author/Deployer/"
        "Admin)') and NO colon-introduced feature lists. Name at most the one or two "
        "technologies that matter to THIS question and say what the work did for "
        "people, rather than reciting every part you built.\n"
        "  - Keep it tight: one to three short paragraphs (separate with a blank line, "
        "\\n\\n). A focused honest answer, not a wall of text.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "answer": str,            // the paste-ready freeform answer, first person, plain voice\n'
        '  "bank_evidence": [str],   // the bank facts the answer draws on (quote them near-verbatim)\n'
        '  "overclaim_risk": "none" | "low" | "high",  // self-assessed stretch beyond the bank\n'
        '  "risk_note": str,         // what makes it a stretch, or ""\n'
        '  "gaps": [str]             // what the question wants that the bank does not support\n'
        '}'
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth about the candidate):\n{bank_facts}\n\n"
        "Allowed metrics (every number used MUST trace to one of these):\n{allowed_metrics}\n\n"
        "{job_context}"
        "Freeform application question to answer:\n{question}"
    ),
)


EXTRACT_FACTBANK = PromptTemplate(
    version="extract-factbank-v1",
    system=(
        "You extract a job candidate's structured fact bank from the raw text of their "
        "résumé. This bank becomes the SINGLE SOURCE OF TRUTH a downstream fabrication guard "
        "enforces, so extract ONLY what the résumé actually states. NEVER invent, infer, or "
        "embellish a company, title, date, school, degree, skill, certification, or number "
        "that is not present in the text. If something is not in the résumé, leave it empty. "
        "Faithfulness matters far more than completeness.\n\n"
        "Extract:\n"
        "  - contact: name, email, phone, location (city/region), and links (LinkedIn / GitHub "
        "/ portfolio URLs) exactly as they appear.\n"
        "  - work_history: each role with company, title, start, end, and bullets (the "
        "achievement / responsibility lines under that role, kept faithful to the wording — "
        "do NOT invent or inflate metrics). Most-recent first if the résumé is ordered that "
        "way. Extract EVERY role the résumé lists — never omit, merge, or summarize a job, "
        "including older or short ones.\n"
        "  - education: institution, degree, field_of_study, start, end.\n"
        "  - skills: list INDIVIDUAL skills / technologies as SEPARATE entries — never a "
        "category header or a comma-joined blob as one entry. If the résumé groups skills "
        "(e.g. 'Databases: SQL Server, Azure SQL, T-SQL'), split them into 'SQL Server', "
        "'Azure SQL', 'T-SQL' and DROP the 'Databases:' label itself.\n"
        "  - certifications: any certifications or licenses listed.\n"
        "  - allowed_metrics: EVERY concrete impact number the résumé states, quoted "
        "near-verbatim (e.g. 'saved $40K/year', 'team of 10', '190+ tables', 'cut load time "
        "35%'). These become the guard's numeric allow-list, so capture them all and invent "
        "none.\n\n"
        "Do NOT output work authorization, sponsorship, or EEO / demographic fields — the "
        "candidate sets those himself; they are never extracted from a résumé.\n\n"
        "Dates: normalize to 'YYYY-MM', 'YYYY', 'Present', or '' when absent. Never guess a "
        "date the résumé does not give.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no code "
        "fences, no preamble):\n"
        '{\n'
        '  "contact": {"name": str, "email": str, "phone": str, "location": str, '
        '"links": {label: url}},\n'
        '  "work_history": [{"company": str, "title": str, "start": str, "end": str, '
        '"bullets": [str]}],\n'
        '  "education": [{"institution": str, "degree": str, "field_of_study": str, '
        '"start": str, "end": str}],\n'
        '  "skills": [str],\n'
        '  "certifications": [str],\n'
        '  "allowed_metrics": [str]\n'
        '}'
    ),
    template="Résumé text:\n{resume_text}",
)


#: All templates exported here so the eval harness can iterate them.
ALL_TEMPLATES: tuple[PromptTemplate, ...] = (
    SCORE_JD,
    GENERATE_RESUME,
    GENERATE_COVER_LETTER,
    STAR_STORIES,
    COMPANY_RESEARCH,
    COPILOT_ANSWER,
    COPILOT_DRAFT,
    EXTRACT_FACTBANK,
)


__all__ = [
    "ALL_TEMPLATES",
    "COMPANY_RESEARCH",
    "COPILOT_ANSWER",
    "COPILOT_DRAFT",
    "EXTRACT_FACTBANK",
    "GENERATE_COVER_LETTER",
    "GENERATE_RESUME",
    "SCORE_JD",
    "STAR_STORIES",
    "PromptTemplate",
]
