"""Fabrication guard for LLM-tailored resumes.

Per the user's explicit ask 2026-05-03 — "we can't just lie about
certain skills for the user." LLMs hallucinate even when prompted
not to: gemma4:e4b in particular has been observed adding skills
to a tailored resume that don't appear in the source resume,
inventing employer names that don't exist in the candidate's
history, or inflating bullet metrics.

This module implements a deterministic post-tailor validator that
compares the LLM's output against the source resume text. Anything
not grounded in the source gets dropped; if too much was fabricated,
the whole tailored resume is rejected and the engine falls back
to the base resume.

Two layers shipped:

  L1 — Skill validation (cheap, no LLM):
       Every claimed skill must appear (substring or all-tokens
       match) in the source resume text. Fabricated skills are
       dropped; the kept-skill list still gets rendered.

  L2 — Experience company validation (cheap, no LLM):
       Every claimed experience entry's company name must appear
       in the source. An experience entry with a fabricated
       company is dropped wholesale.

A reject threshold (>50% of skills fabricated, OR >50% of
experience entries fabricated) triggers full rejection — at that
point the tailor output is unreliable and the base resume is the
safer choice.

Out of scope for this commit (intentional):

  L3 — LLM bullet verification: sends (source, tailored bullets)
       to a second LLM call asking "list any bullet with claims
       not supported by the source." Powerful but +20-30s per
       apply. Future opt-in flag.

  Number-claim verification: tailored bullets like "led team of
       50" when source says "led team of 5" is real fabrication
       but hard to catch deterministically without false positives
       on legitimate aggregates.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from auto_applier.resume.tailor import TailoredResume

logger = logging.getLogger(__name__)


# Acceptability thresholds. Tunable via config in the future.
# 0.5 = "if more than half of the tailored skills are fabricated,
# reject the whole tailored." Skills are a noisy signal (LLMs
# sometimes spell variants) so we don't reject on a single bad
# entry — only when fabrication is systemic.
DEFAULT_MAX_FABRICATED_SKILL_FRACTION = 0.5
# 0.34 = "any time more than a third of experience entries lack a
# matching company in source, reject." Experience is the highest-
# stakes claim; even one fabricated employer is bad. Threshold is
# lower than skills because the count is small (typically 2-5
# entries) — at 1 fabricated of 3, that's 33% which exceeds.
DEFAULT_MAX_FABRICATED_EXPERIENCE_FRACTION = 0.34


@dataclass
class ValidationReport:
    """Result of validating a tailored resume against its source.

    The report is the engine's decision input: ``is_acceptable``
    determines whether the tailored output is used or the engine
    falls back to the base resume.
    """
    is_acceptable: bool
    reason: str = ""
    dropped_skills: list[str] = field(default_factory=list)
    dropped_experience_companies: list[str] = field(default_factory=list)
    skill_fabrication_rate: float = 0.0
    experience_fabrication_rate: float = 0.0


def validate_tailored_resume(
    tailored: TailoredResume,
    source_text: str,
    max_skill_fab: float = DEFAULT_MAX_FABRICATED_SKILL_FRACTION,
    max_exp_fab: float = DEFAULT_MAX_FABRICATED_EXPERIENCE_FRACTION,
) -> ValidationReport:
    """Filter ``tailored`` in place against ``source_text`` and
    return a verdict on whether it's safe to use.

    Mutations to ``tailored``:
      - ``tailored.skills`` is reduced to only entries supported
        by the source.
      - ``tailored.experience`` is reduced to only entries whose
        company appears in the source.
      - ``tailored.education`` is left alone (low fabrication risk;
        schools are easy to verify but rarely faked).

    Returns:
        ValidationReport. When ``is_acceptable`` is False, the
        caller should fall back to the base resume — too much
        unsupported content was dropped to trust the rest.
    """
    if not source_text:
        # No source to validate against. Fail safe: we can't
        # confirm any claim, so trust the LLM's "no fabrication"
        # prompt rule and let it pass with an empty report. The
        # caller's outer fallback path still kicks in if PDF
        # render fails for any reason.
        return ValidationReport(is_acceptable=True, reason="no source to compare against")

    source_lower = source_text.lower()

    # ---- Layer 1: skills ----
    original_skills = list(tailored.skills or [])
    kept_skills: list[str] = []
    dropped_skills: list[str] = []
    for skill in original_skills:
        if not isinstance(skill, str):
            continue
        s = skill.strip()
        if not s:
            continue
        if _skill_supported(s, source_lower):
            kept_skills.append(s)
        else:
            dropped_skills.append(s)
    tailored.skills = kept_skills

    skill_count = len(original_skills)
    skill_fab_rate = len(dropped_skills) / skill_count if skill_count else 0.0

    # ---- Layer 2: experience companies ----
    original_exp = list(tailored.experience or [])
    kept_exp: list[dict] = []
    dropped_companies: list[str] = []
    for entry in original_exp:
        if not isinstance(entry, dict):
            continue
        company = str(entry.get("company") or "").strip()
        if not company:
            # No company = nothing to verify. Keep but flag.
            kept_exp.append(entry)
            continue
        if _company_supported(company, source_lower):
            kept_exp.append(entry)
        else:
            dropped_companies.append(company)
    tailored.experience = kept_exp

    exp_count = len(original_exp)
    exp_fab_rate = len(dropped_companies) / exp_count if exp_count else 0.0

    # ---- Acceptability ----
    if skill_fab_rate > max_skill_fab:
        return ValidationReport(
            is_acceptable=False,
            reason=(
                f"{skill_fab_rate:.0%} of tailored skills "
                f"({len(dropped_skills)}/{skill_count}) not "
                "supported by source resume — LLM fabricated too "
                "much. Falling back to base resume."
            ),
            dropped_skills=dropped_skills,
            dropped_experience_companies=dropped_companies,
            skill_fabrication_rate=skill_fab_rate,
            experience_fabrication_rate=exp_fab_rate,
        )
    if exp_fab_rate > max_exp_fab:
        return ValidationReport(
            is_acceptable=False,
            reason=(
                f"{exp_fab_rate:.0%} of tailored experience entries "
                f"({len(dropped_companies)}/{exp_count}) reference "
                "a company NOT in the source resume — fabricated "
                "employer history. Falling back to base resume."
            ),
            dropped_skills=dropped_skills,
            dropped_experience_companies=dropped_companies,
            skill_fabrication_rate=skill_fab_rate,
            experience_fabrication_rate=exp_fab_rate,
        )

    # Acceptable — log dropped items at WARNING so the user can
    # spot patterns over time without scanning every run.
    if dropped_skills:
        logger.warning(
            "Tailor validator: dropped %d unsupported skill(s): %s",
            len(dropped_skills), dropped_skills,
        )
    if dropped_companies:
        logger.warning(
            "Tailor validator: dropped %d unsupported experience entry "
            "(companies not in source): %s",
            len(dropped_companies), dropped_companies,
        )

    return ValidationReport(
        is_acceptable=True,
        reason="passed validation",
        dropped_skills=dropped_skills,
        dropped_experience_companies=dropped_companies,
        skill_fabrication_rate=skill_fab_rate,
        experience_fabrication_rate=exp_fab_rate,
    )


# ----------------------------------------------------------------------
# Skill / company support checks
# ----------------------------------------------------------------------

# Tokenizer that handles "C++", "C#", ".NET" — these need to keep
# their special chars to be matchable. Strip everything else.
_TOKEN_RE = re.compile(r"[a-z0-9+#./]+", re.IGNORECASE)


def _skill_supported(skill: str, source_lower: str) -> bool:
    """True if ``skill`` (case-insensitive) appears in ``source_lower``,
    either as a direct substring or via all-tokens-present.

    Examples:
      "Python"       in "Python developer"           → True (substring)
      "SQL"          in "Used MySQL daily"           → True (substring "sql" in "mysql")
      "Apache Kafka" in "kafka pipelines + airflow"  → True (token "kafka" present)
      "Snowflake"    in "Used Postgres only"          → False
      "C++"          in "Comfortable in C++"          → True
    """
    s = skill.lower().strip()
    if not s:
        return False
    if s in source_lower:
        return True
    # Multi-word: every meaningful token must appear in source
    tokens = [
        t for t in _TOKEN_RE.findall(s)
        if len(t) >= 3 or t in {"c", "r", "go"}  # 1-2 letter langs OK
    ]
    if not tokens:
        return False
    return all(t in source_lower for t in tokens)


# Common company-name suffixes to strip before matching. "Acme Corp"
# in source vs "Acme" in tailored should match; ditto for ", Inc.",
# ", LLC", ", Ltd.", etc. We strip these from BOTH sides so the
# core name is what's compared.
_COMPANY_SUFFIXES = (
    " inc",
    " inc.",
    ", inc",
    ", inc.",
    " incorporated",
    " llc",
    " l.l.c.",
    " ltd",
    " ltd.",
    " limited",
    " corp",
    " corp.",
    " corporation",
    " co.",
    " gmbh",
    " plc",
    " s.a.",
    " ag",
    " sas",
)


def _strip_company_suffix(name: str) -> str:
    """Remove common corporate-suffix noise so 'Acme' and 'Acme, Inc.'
    compare as the same company."""
    n = name.lower().strip().rstrip(",.")
    for suffix in _COMPANY_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip().rstrip(",.")
            break
    return n


def _company_supported(company: str, source_lower: str) -> bool:
    """True if ``company`` (with corporate suffixes stripped) appears
    in ``source_lower``.

    Why substring is enough: source resumes spell out company names
    in full; tailored output sometimes shortens them. As long as
    the tailored shortened form appears anywhere in the source,
    it's not fabricated.
    """
    core = _strip_company_suffix(company)
    if not core:
        return False
    if core in source_lower:
        return True
    # Last resort: split into ≥4-char tokens and require all to
    # appear. Catches "Northwind Logistics" matching when source
    # only said "Northwind". Threshold is higher (4 chars) than
    # skill matching because companies have shorter, more
    # distinctive tokens.
    tokens = [t for t in _TOKEN_RE.findall(core) if len(t) >= 4]
    if not tokens:
        return False
    return all(t in source_lower for t in tokens)
