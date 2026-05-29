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
    declares its expected schema inline. The Ollama/Gemini backends in
    :mod:`av3.llm.complete` already pass ``format=json`` / ``responseMimeType``,
    but the schema-in-prompt keeps weaker local models honest.
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


#: All Phase-3 templates exported here so the eval harness ((7/M)) can iterate them.
ALL_TEMPLATES: tuple[PromptTemplate, ...] = (SCORE_JD,)


__all__ = ["ALL_TEMPLATES", "SCORE_JD", "PromptTemplate"]
