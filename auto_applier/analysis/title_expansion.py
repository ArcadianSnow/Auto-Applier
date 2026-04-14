"""Job title expansion — given a seed title, suggest adjacent titles.

A novice user who types "Data Analyst" and doesn't know to also
search "Business Intelligence Analyst" or "Analytics Engineer"
will miss 60-80% of relevant jobs. This module bridges that gap
by generating adjacent titles the user is likely qualified for.

Two sources, priority order:

1. **LLM-based** (primary) — resume-aware when we have a profile,
   generic otherwise. Uses the same Ollama -> Gemini -> rule-based
   router as the rest of the app.

2. **Static fallback dict** — ~20 common seeds covering data, tech,
   business, ops, marketing, sales. Kicks in when the LLM is down
   or returns garbage. Zero-latency, zero-cost.

The LLM prompt is designed to AVOID:
- Seniority inflation (don't suggest "Senior X" when user searched "X")
- Far-reach titles (don't suggest "Engineer" when searching "Analyst")
- Made-up buzzword titles that don't appear on job boards

Returned titles are guaranteed filesystem-safe, lowercased for
consistent matching, and deduplicated against the seed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from auto_applier.llm.prompts import TITLE_EXPANSION
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)


# Common job-title seeds and adjacent titles. When the LLM is
# unavailable or returns nothing useful, we fall back to these
# hardcoded mappings. Keys are lowercase for case-insensitive lookup.
#
# Keep this list SMALL and HIGH-CONFIDENCE. 20-ish common titles
# covers maybe 60-70% of first-time users. The LLM handles the rest.
STATIC_TITLE_EXPANSIONS: dict[str, list[str]] = {
    # Data track
    "data analyst": [
        "business intelligence analyst",
        "analytics engineer",
        "reporting analyst",
        "business analyst",
    ],
    "data engineer": [
        "analytics engineer",
        "data platform engineer",
        "etl developer",
        "big data engineer",
    ],
    "data scientist": [
        "machine learning engineer",
        "analytics engineer",
        "research scientist",
        "ai engineer",
    ],
    "analytics engineer": [
        "data engineer",
        "data analyst",
        "business intelligence engineer",
        "analytics developer",
    ],
    "business intelligence analyst": [
        "data analyst",
        "reporting analyst",
        "analytics engineer",
        "business analyst",
    ],
    # Software engineering track
    "software engineer": [
        "software developer",
        "backend developer",
        "full stack developer",
        "platform engineer",
    ],
    "frontend developer": [
        "ui engineer",
        "web developer",
        "javascript developer",
        "react developer",
    ],
    "backend developer": [
        "api engineer",
        "software engineer",
        "platform engineer",
        "server engineer",
    ],
    "full stack developer": [
        "software engineer",
        "web developer",
        "frontend developer",
        "backend developer",
    ],
    "devops engineer": [
        "site reliability engineer",
        "platform engineer",
        "cloud engineer",
        "infrastructure engineer",
    ],
    "mobile developer": [
        "ios developer",
        "android developer",
        "react native developer",
        "software engineer",
    ],
    # Product / design
    "product manager": [
        "product owner",
        "technical product manager",
        "program manager",
        "associate product manager",
    ],
    "ux designer": [
        "product designer",
        "ui designer",
        "interaction designer",
        "ux researcher",
    ],
    "ui designer": [
        "ux designer",
        "product designer",
        "visual designer",
        "web designer",
    ],
    # Business / ops
    "business analyst": [
        "product owner",
        "systems analyst",
        "operations analyst",
        "process analyst",
    ],
    "project manager": [
        "program manager",
        "scrum master",
        "project coordinator",
        "operations manager",
    ],
    # Sales / marketing
    "marketing manager": [
        "growth manager",
        "brand manager",
        "digital marketing manager",
        "product marketing manager",
    ],
    "sales representative": [
        "account executive",
        "business development representative",
        "sales associate",
        "customer success manager",
    ],
    # Finance / HR
    "accountant": [
        "financial analyst",
        "bookkeeper",
        "accounting associate",
        "auditor",
    ],
    "recruiter": [
        "talent acquisition specialist",
        "hr coordinator",
        "sourcer",
        "people operations specialist",
    ],
    "customer success manager": [
        "account manager",
        "customer support manager",
        "client success manager",
        "customer experience manager",
    ],
}


@dataclass
class ExpansionResult:
    """The result of expanding a seed job title.

    ``adjacents`` are the suggested related titles. ``source`` is
    ``"llm"`` or ``"static"`` so callers can surface confidence to
    the user. ``reasoning`` is a short LLM-written explanation
    (empty for static fallback).
    """
    seed: str
    adjacents: list[str] = field(default_factory=list)
    source: str = "static"
    reasoning: str = ""

    @property
    def has_suggestions(self) -> bool:
        return bool(self.adjacents)


def _normalize(title: str) -> str:
    """Lowercase + collapse whitespace. Used for dedup + lookup."""
    if not title:
        return ""
    return re.sub(r"\s+", " ", title.strip().lower())


def _static_expand(seed: str) -> list[str]:
    """Look up the seed in the hardcoded dictionary.

    Returns the adjacent list if the seed matches (case-insensitive),
    else an empty list. Does NOT include the seed itself.
    """
    key = _normalize(seed)
    adjacents = STATIC_TITLE_EXPANSIONS.get(key, [])
    # Return a copy — callers may mutate
    return list(adjacents)


async def _llm_expand(
    seed: str,
    router: LLMRouter,
    resume_text: str = "",
) -> tuple[list[str], str]:
    """Ask the LLM for adjacent titles. Returns (titles, reasoning).

    Empty list on failure. ``resume_text`` is optional context —
    when present, the LLM tailors adjacents to the candidate's
    actual skills rather than giving generic siblings.
    """
    # Truncate resume to keep the prompt fast on local models
    resume_excerpt = resume_text[:2000] if resume_text else "(not provided)"

    prompt = TITLE_EXPANSION.format(
        seed_title=seed,
        resume_text=resume_excerpt,
    )

    try:
        result = await router.complete_json(
            prompt=prompt,
            system_prompt=TITLE_EXPANSION.system,
        )
    except Exception as exc:
        logger.debug("LLM title expansion failed: %s", exc)
        return [], ""

    adjacents_raw = result.get("adjacents", [])
    reasoning = str(result.get("reasoning", "")).strip()

    if not isinstance(adjacents_raw, list):
        return [], reasoning

    # Clean, dedup, and filter out the seed itself
    seen: set[str] = {_normalize(seed)}
    cleaned: list[str] = []
    for title in adjacents_raw:
        if not isinstance(title, str):
            continue
        title = title.strip()
        if not title:
            continue
        normalized = _normalize(title)
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(title)

    # Cap at 5 — LLMs sometimes pad
    return cleaned[:5], reasoning


async def expand_title(
    seed: str,
    router: LLMRouter | None = None,
    resume_text: str = "",
    prefer_llm: bool = True,
) -> ExpansionResult:
    """Generate adjacent job titles for a seed.

    When ``prefer_llm`` is True (default) and a router is provided,
    tries the LLM first. Falls back to the static dictionary on LLM
    failure or empty response.

    When ``prefer_llm`` is False or no router is given, uses the
    static dict directly — deterministic, fast, good for tests and
    offline use.

    Returns ``ExpansionResult`` with an empty ``adjacents`` list if
    the seed isn't recognized by either source. Callers should check
    ``has_suggestions`` before using.
    """
    seed = (seed or "").strip()
    if not seed:
        return ExpansionResult(seed="")

    # LLM path
    if prefer_llm and router is not None:
        adjacents, reasoning = await _llm_expand(seed, router, resume_text)
        if adjacents:
            return ExpansionResult(
                seed=seed,
                adjacents=adjacents,
                source="llm",
                reasoning=reasoning,
            )
        logger.debug(
            "LLM expansion yielded no adjacents for '%s', falling "
            "back to static dict",
            seed,
        )

    # Static fallback
    static = _static_expand(seed)
    return ExpansionResult(
        seed=seed,
        adjacents=static,
        source="static",
        reasoning="",
    )
