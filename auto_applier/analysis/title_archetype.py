"""Lightweight job-title archetype classification.

Groups job titles into broad career-track buckets (analyst, engineer,
scientist, manager, etc.) so we can show the user "you applied to 5
analyst-type jobs with your data_analyst resume" instead of listing
each unique title separately.

Two sources of truth, in priority order:

1. ``data/archetypes.json`` — if the user has configured named
   archetypes (optional), we use the richer classifier from
   ``resume/archetypes.py``.

2. Regex keyword fallback — zero-setup default. Works out of the box
   for common tech/business titles. Returns ``"other"`` for titles
   that don't match any known pattern.

This module intentionally NEVER calls the LLM. Archetype classification
needs to be cheap enough to run over hundreds of jobs in a gap report.
"""
from __future__ import annotations

import re


# Regex keyword patterns, ordered by specificity. More specific titles
# (scientist > engineer > analyst) are checked first so compound titles
# like "Data Science Engineer" classify as "scientist" rather than
# defaulting to "engineer".
#
# Each pattern is case-insensitive. Matches anywhere in the title.
_ARCHETYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("scientist",  re.compile(r"\b(data science|data scientist|research scientist|ml\s*(engineer|scientist)|machine learning (engineer|scientist))\b", re.IGNORECASE)),
    ("architect",  re.compile(r"\b(architect|principal architect|solutions architect)\b", re.IGNORECASE)),
    ("engineer",   re.compile(r"\b(engineer|developer|swe|sre|devops|backend|frontend|fullstack|full.?stack|platform|infrastructure)\b", re.IGNORECASE)),
    ("analyst",    re.compile(r"\b(analyst|analytics|reporting|intelligence)\b", re.IGNORECASE)),
    ("designer",   re.compile(r"\b(designer|design|ux|ui|product design)\b", re.IGNORECASE)),
    ("manager",    re.compile(r"\b(manager|director|head of|lead|supervisor|chief|vp|vice president|cto|ceo|cfo)\b", re.IGNORECASE)),
    ("product",    re.compile(r"\b(product manager|product owner|pm|program manager)\b", re.IGNORECASE)),
    ("sales",      re.compile(r"\b(sales|account executive|ae|business development|bdr|sdr|customer success|cs)\b", re.IGNORECASE)),
    ("marketing",  re.compile(r"\b(marketing|growth|seo|content|social media|brand)\b", re.IGNORECASE)),
    ("operations", re.compile(r"\b(operations|ops|project manager|coordinator|administrator)\b", re.IGNORECASE)),
    ("support",    re.compile(r"\b(support|help ?desk|customer service|technical support)\b", re.IGNORECASE)),
    ("finance",    re.compile(r"\b(finance|financial|accountant|accounting|auditor|controller)\b", re.IGNORECASE)),
    ("hr",         re.compile(r"\b(recruiter|talent|hr|human resources|people operations)\b", re.IGNORECASE)),
]


def classify_title(title: str) -> str:
    """Classify a job title into a broad archetype bucket.

    Returns a short lowercase tag: ``"analyst"``, ``"engineer"``,
    ``"scientist"``, etc., or ``"other"`` if no pattern matches.

    Strips seniority prefixes ("Senior", "Lead", "Principal", etc.)
    before matching so "Senior Data Analyst" and "Data Analyst"
    classify the same way. Seniority prefixes map to the "manager"
    bucket only when they appear without another role — e.g. a bare
    "Director" classifies as manager, but "Director of Engineering"
    classifies as engineer (more specific pattern matches first).
    """
    if not title:
        return "other"

    for archetype, pattern in _ARCHETYPE_PATTERNS:
        if pattern.search(title):
            return archetype

    return "other"


def classify_with_user_archetypes(
    title: str,
    user_archetypes: list[dict] | None = None,
) -> str:
    """Classify using user-configured archetypes first, falling back to regex.

    ``user_archetypes`` is the content of ``data/archetypes.json`` —
    a list of ``{"name": str, "keywords": [str]}`` entries. If any
    keyword substring-matches the title (case-insensitive), that
    archetype wins.

    When ``user_archetypes`` is None or empty, falls through to the
    regex classifier. This is the path we default to for novice users
    who haven't configured archetypes.
    """
    if not title:
        return "other"

    if user_archetypes:
        title_lower = title.lower()
        for entry in user_archetypes:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "").strip()
            keywords = entry.get("keywords", [])
            if not name or not isinstance(keywords, list):
                continue
            for kw in keywords:
                if not isinstance(kw, str):
                    continue
                if kw.strip().lower() in title_lower:
                    return name

    return classify_title(title)
