"""Role-family classifier for job titles (read-side, deterministic).

The LLM scorer rates JD-vs-profile *fit* but does not bucket a posting into a role
family. This module is the deterministic layer that maps a title to one of a small set
of families — so the ``av3 shortlist`` view can target one family at a time, and each
family lines up 1:1 with a résumé variant the user maintains (Solutions / Data-Platform /
Database / AI-Application / …).

Like :mod:`auto_applier.domain.location`, matching is intentionally simple and
substring-based on the *normalized* title (ATS titles are short and messy). Order matters:
families are checked most-specific first, first match wins, so e.g. "Analytics Engineer"
lands in DATA_PLATFORM (an engineering role) rather than ANALYTICS (a BI/analyst role),
and "Senior Software Engineer, Data Platform" lands in DATA_PLATFORM rather than BACKEND.

Keyword sets are tuned against the live scored corpus (the personal search). Edit the
``_FAMILY_KEYWORDS`` table to retune. Each family maps to a résumé variant in
``JobSearch/resume-variants.md``.
"""

from __future__ import annotations

from enum import Enum

from auto_applier.domain.dedup import normalize

__all__ = ["JobFamily", "classify_family", "FAMILY_LABELS"]


class JobFamily(str, Enum):
    """Role family of a posting. Each maps 1:1 to a résumé variant."""

    SOLUTIONS = "solutions"            # Solutions / Forward-Deployed / Customer / Implementation Eng
    DATA_PLATFORM = "data_platform"    # Data Eng + Analytics Eng + Data Platform + Platform Eng
    DATABASE = "database"              # DBA / Database / SQL Engineer
    AI_APPLICATION = "ai_application"  # Applied-AI / LLM-application engineering
    BACKEND = "backend"                # Backend / full-stack / general SWE
    ANALYTICS = "analytics"            # BI / Data Analyst / Data Science (non-engineering)
    OTHER = "other"                    # unmatched


#: Human-readable labels for the digest/shortlist header.
FAMILY_LABELS: dict[JobFamily, str] = {
    JobFamily.SOLUTIONS: "Solutions / Forward-Deployed / Customer Eng",
    JobFamily.DATA_PLATFORM: "Data / Platform Engineering",
    JobFamily.DATABASE: "Database / DBA / SQL Engineering",
    JobFamily.AI_APPLICATION: "AI-Application / LLM Engineering",
    JobFamily.BACKEND: "Backend / Software Engineering",
    JobFamily.ANALYTICS: "Analytics / BI",
    JobFamily.OTHER: "Other / unmatched",
}

# Ordered, most-specific first. Keywords are already normalized (lowercase, space-delimited);
# they are matched as whole phrases against a space-padded normalized title.
_FAMILY_KEYWORDS: tuple[tuple[JobFamily, tuple[str, ...]], ...] = (
    (JobFamily.SOLUTIONS, (
        "solutions engineer", "solution engineer", "solutions engineering",
        "solutions consultant", "solutions architect", "forward deployed",
        "customer engineer", "sales engineer", "field engineer", "value engineer",
        "implementation engineer", "implementation specialist", "implementation consultant",
        "professional services", "deployment engineer", "onboarding engineer",
        "presales", "pre sales",
    )),
    (JobFamily.DATA_PLATFORM, (
        "data platform", "data engineer", "data engineering", "analytics engineer",
        "analytics engineering", "platform engineer", "data infrastructure", "data infra",
        "etl", "big data", "data pipeline", "streaming engineer", "lakehouse",
    )),
    (JobFamily.DATABASE, (
        "database", "databases", "dba", "sql", "postgres", "postgresql", "mysql",
        "sql server", "t sql",
    )),
    (JobFamily.AI_APPLICATION, (
        "ai engineer", "ai engineering", "applied ai", "ai developer", "machine learning",
        "ml engineer", "ml platform", "llm", "genai", "generative ai", "applied scientist",
        "ai", "ml",
    )),
    (JobFamily.BACKEND, (
        "backend", "back end", "software engineer", "full stack", "fullstack", "swe",
        "developer", "application engineer",
    )),
    (JobFamily.ANALYTICS, (
        "analyst", "analytics", "business intelligence", "bi", "reporting", "insights",
        "data scientist", "data science",
    )),
)


def classify_family(title: str | None) -> JobFamily:
    """Map a job title to its :class:`JobFamily` (first match wins, most-specific first)."""
    norm = f" {normalize(title or '')} "
    if norm.strip():
        for family, keywords in _FAMILY_KEYWORDS:
            if any(f" {kw} " in norm for kw in keywords):
                return family
    return JobFamily.OTHER
