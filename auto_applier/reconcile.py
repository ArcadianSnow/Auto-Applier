"""Batch skill-reconciliation (spec §7b) — Phase 6 / v3.1.

The reconciliation checkpoint sits between discovery and scoring: skills the batch's JDs
demand but the fact bank doesn't list are **surfaced**, the user reviews them, and the
approved ones are **inserted into the fact bank** before scoring/generation run against a
freshened bank (spec §7b).

This wires the previously-dead `SkillGapRepo` with a producer, and splits the work along
the Rule 2.6 gather/act line:

  * **Gather (pure / safe):** :func:`extract_candidate_skills` (deterministic phrase match
    of JD text against a vocabulary — no LLM, no network), :func:`record_batch_gaps` (bump
    the gap repo for demanded skills NOT already in the bank), and :func:`build_proposals`
    (rank open gaps for review). Re-runnable, idempotent-ish (bump just increments counts).
  * **Act (gated):** :func:`apply_proposals` inserts approved skills into the fact bank
    (additive — never wholesale-replace) and the caller persists + marks the gaps reconciled.
    Mutating the fact bank is the load-bearing "act"; the CLI keeps it behind an explicit
    ``--apply`` (preview is the default), and the bank is the fabrication guard's source of
    truth — so a bad insert widens what generation may claim. Gate it.

**Why a deterministic vocabulary, not an LLM extractor.** v3's core is local-first +
fail-closed; an LLM skill-tagger would add a model dependency and a fabrication surface to
a step whose whole job is to *propose facts about the user*. A curated phrase list is
transparent, testable, and zero-cost. It's intentionally tech-segment-biased (matches the
v3 target market per `research/ats-market-landscape.md`); it's injectable so a user/locale
can extend it, and an LLM-assisted extractor can land later as an opt-in `vocabulary=`
producer without changing this module's contract. See `research/phase6-v3.1.md` §(5/M).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auto_applier.db.repositories import SkillGapRepo
from auto_applier.domain.models import Job
from auto_applier.resume.factbank import FactBank

__all__ = [
    "SkillProposal",
    "DEFAULT_SKILL_VOCABULARY",
    "extract_candidate_skills",
    "record_batch_gaps",
    "build_proposals",
    "apply_proposals",
]


#: Curated, tech-segment skill vocabulary for deterministic JD matching. NOT exhaustive —
#: a transparent starting set the user can extend (pass your own ``vocabulary=``). Phrases
#: are matched case-insensitively on word boundaries, so "go" won't match "google". Kept
#: as canonical display casing; the matcher lowercases for comparison.
DEFAULT_SKILL_VOCABULARY: tuple[str, ...] = (
    # languages
    "Python", "JavaScript", "TypeScript", "Java", "Go", "Rust", "C++", "C#", "Ruby",
    "Scala", "Kotlin", "Swift", "PHP", "R", "SQL", "Bash",
    # data / ML
    "Pandas", "NumPy", "PyTorch", "TensorFlow", "scikit-learn", "Spark", "Airflow",
    "dbt", "Snowflake", "BigQuery", "Redshift", "Kafka", "ETL", "Tableau", "Power BI",
    "Machine Learning", "Deep Learning", "NLP", "Data Engineering",
    # web / frameworks
    "React", "Vue", "Angular", "Node.js", "Django", "Flask", "FastAPI", "Spring",
    "Rails", ".NET", "GraphQL", "REST",
    # infra / cloud / devops
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Ansible", "Jenkins",
    "CI/CD", "Linux", "Git", "Prometheus", "Grafana",
    # data stores
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "SQLite",
)

#: A word-boundary matcher per vocabulary term, built once. ``re.escape`` so "C++"/".NET"
#: match literally; ``(?<!\w)`` / ``(?!\w)`` rather than ``\b`` because ``\b`` misbehaves
#: around "+"/"#"/"." (non-word chars) — "C++" has no trailing word boundary.
def _compile(vocabulary: tuple[str, ...]) -> list[tuple[str, re.Pattern]]:
    out: list[tuple[str, re.Pattern]] = []
    for term in vocabulary:
        pat = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        out.append((term, pat))
    return out


_DEFAULT_COMPILED = _compile(DEFAULT_SKILL_VOCABULARY)


@dataclass(frozen=True)
class SkillProposal:
    """A skill the batch demands that the fact bank lacks — a reconciliation candidate.

    ``count`` is how many of the batch's JDs (across the gap repo's history) demanded it;
    higher = stronger signal it's worth adding. ``in_bank`` is always False for a proposal
    (it's filtered out otherwise) but kept explicit for the dashboard."""

    skill: str
    count: int
    in_bank: bool = False


def _bank_skill_keys(bank: FactBank) -> set[str]:
    return {s.strip().lower() for s in bank.skills if s and s.strip()}


def extract_candidate_skills(
    jd_text: str, vocabulary: tuple[str, ...] | None = None
) -> set[str]:
    """Deterministically pull known skill phrases out of a JD (no LLM, no network).

    Returns the canonical-cased vocabulary terms present in ``jd_text`` (word-boundary,
    case-insensitive). Empty/blank text → empty set. Pass ``vocabulary`` to override the
    built-in set (e.g. a locale- or domain-specific list)."""
    text = jd_text or ""
    if not text.strip():
        return set()
    compiled = _DEFAULT_COMPILED if vocabulary is None else _compile(vocabulary)
    return {term for term, pat in compiled if pat.search(text)}


def record_batch_gaps(
    jobs: list[Job],
    bank: FactBank,
    gap_repo: SkillGapRepo,
    *,
    vocabulary: tuple[str, ...] | None = None,
) -> int:
    """Scan a batch of jobs' JDs, bump the gap repo for every demanded skill the bank
    LACKS (spec §7b passive surfacing). Returns the number of (job, gap) bumps.

    Skills already in the bank are skipped — we only surface *gaps*. One bump per
    (job × missing skill), so a skill demanded by 5 of the batch's JDs ends at count 5,
    which is exactly the recurrence signal :func:`build_proposals` ranks on. Pure except
    for the gap-repo writes (the gap table is an accumulator, not the fact bank — bumping
    it is safe/gather; it only *proposes*, never changes what generation may claim)."""
    have = _bank_skill_keys(bank)
    bumps = 0
    for job in jobs:
        demanded = extract_candidate_skills(job.description or "", vocabulary)
        for skill in demanded:
            if skill.lower() in have:
                continue
            gap_repo.bump(skill)
            bumps += 1
    return bumps


def build_proposals(
    bank: FactBank, gap_repo: SkillGapRepo, *, min_count: int = 1
) -> list[SkillProposal]:
    """Rank open skill-gaps the bank still lacks into review proposals (spec §7b).

    Filters out anything already in the bank (defensive — a gap could have been added to
    the bank out-of-band since it was recorded), keeps ``count >= min_count``, and orders
    by recurrence (the repo already sorts by count desc). Pure read."""
    have = _bank_skill_keys(bank)
    proposals: list[SkillProposal] = []
    for gap in gap_repo.list_open(min_count=min_count):
        if gap.skill.lower() in have:
            continue
        proposals.append(SkillProposal(skill=gap.skill, count=gap.count))
    return proposals


def apply_proposals(bank: FactBank, approved: list[str]) -> FactBank:
    """Insert approved skills into the fact bank — **additive**, case-insensitive dedupe
    (spec §7b "inserted into the fact bank, user-approved"). THE act: mutates the bank that
    is the fabrication guard's source of truth, so callers gate it behind explicit user
    approval and persist + mark the gaps reconciled afterward.

    Unlike :func:`auto_applier.web.onboarding.merge_skills` (which replaces the list wholesale during
    onboarding), this APPENDS — reconciliation augments an existing bank, never clobbers it.
    Returns the same bank (mutated in place) for chaining."""
    have = _bank_skill_keys(bank)
    for raw in approved:
        skill = (raw or "").strip()
        if not skill or skill.lower() in have:
            continue
        bank.skills.append(skill)
        have.add(skill.lower())
    return bank
