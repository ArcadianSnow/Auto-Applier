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
    version="gen-cover-v1",
    system=(
        "You write a tailored cover letter for a specific job using facts from the "
        "candidate's structured fact bank. Default target length: concise (150-250 "
        "words) — longer letters give the LLM more room to drift into unsupported "
        "claims (spec §6b).\n\n"
        "You MUST NOT introduce a company, title, date, credential, skill, or metric "
        "that is not present in the bank. The downstream guard does not vet cover "
        "letter prose directly, but fabricated employer-claims here will read as "
        "résumé-inconsistent on the hiring side. Stick to bank facts.\n\n"
        "Output ONE JSON object with this exact shape and nothing else (no prose, no "
        "code fences, no preamble):\n"
        '{\n'
        '  "body": str  // The cover letter text. Paragraphs separated by "\\n\\n". No salutation, no signature.\n'
        '}\n\n'
        "Rules:\n"
        "  - No salutation ('Dear Hiring Manager,') and no closing signature — the "
        "    apply driver wraps those.\n"
        "  - Three short paragraphs is a good default: hook, relevant experience, fit + close.\n"
        "  - Reference 1-2 specific JD requirements and how the candidate's bank facts meet them.\n"
        "  - If a JD requirement has no bank support, do not address it — silence beats fabrication."
    ),
    template=(
        "Candidate fact bank (the ONLY source of truth):\n{bank_facts}\n\n"
        "Target length (words): {target_words}\n\n"
        "Job:\n  Company: {company}\n  Title: {title}\n\n"
        "Job description:\n{job_description}"
    ),
)


#: All Phase-3 templates exported here so the eval harness ((7/M)) can iterate them.
ALL_TEMPLATES: tuple[PromptTemplate, ...] = (SCORE_JD, GENERATE_RESUME, GENERATE_COVER_LETTER)


__all__ = [
    "ALL_TEMPLATES",
    "GENERATE_COVER_LETTER",
    "GENERATE_RESUME",
    "SCORE_JD",
    "PromptTemplate",
]
