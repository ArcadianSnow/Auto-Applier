"""Batch skill-reconciliation (spec §7b, Phase 6 5/M).

Covers the pure gather layer (extract / build proposals / additive apply) and the
SkillGapRepo producer wiring (record_batch_gaps + set_status) on a real tmp DB.
"""

from __future__ import annotations

import sqlite3

from auto_applier.db.repositories import JobRepo, SkillGapRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.reconcile import (
    DEFAULT_SKILL_VOCABULARY,
    SkillProposal,
    apply_proposals,
    build_proposals,
    extract_candidate_skills,
    record_batch_gaps,
)
from auto_applier.resume.factbank import FactBank


# --------------------------------------------------------------- extraction (pure)

def test_extract_matches_known_skills():
    jd = "We need strong Python and PostgreSQL skills; AWS a plus. Kubernetes nice to have."
    got = extract_candidate_skills(jd)
    assert "Python" in got
    assert "PostgreSQL" in got
    assert "AWS" in got
    assert "Kubernetes" in got


def test_extract_is_word_boundary_safe():
    # "Go" must not match inside "Google"; "R" must not match inside "ररm" words.
    jd = "Experience with Google Cloud and great communication."
    got = extract_candidate_skills(jd)
    assert "Go" not in got


def test_extract_handles_punctuation_terms():
    jd = "Strong C++ and C# and .NET and CI/CD experience required."
    got = extract_candidate_skills(jd)
    assert "C++" in got
    assert "C#" in got
    assert ".NET" in got
    assert "CI/CD" in got


def test_extract_empty_text():
    assert extract_candidate_skills("") == set()
    assert extract_candidate_skills("   ") == set()


def test_extract_custom_vocabulary():
    got = extract_candidate_skills("We use Elixir and Phoenix here.", vocabulary=("Elixir", "Phoenix"))
    assert got == {"Elixir", "Phoenix"}


# --------------------------------------------------------------- apply (additive)

def test_apply_proposals_is_additive():
    bank = FactBank(skills=["Python", "SQL"])
    apply_proposals(bank, ["AWS", "Docker"])
    assert bank.skills == ["Python", "SQL", "AWS", "Docker"]


def test_apply_proposals_dedupes_case_insensitively():
    bank = FactBank(skills=["Python"])
    apply_proposals(bank, ["python", "PYTHON", "AWS"])
    # 'python' variants collapse to the existing entry; only AWS is new.
    assert bank.skills == ["Python", "AWS"]


def test_apply_proposals_skips_blanks():
    bank = FactBank(skills=["Python"])
    apply_proposals(bank, ["", "  ", "Go"])
    assert bank.skills == ["Python", "Go"]


# --------------------------------------------------------------- producer + proposals (real DB)

def _job(conn, *, sjid, desc) -> Job:
    repo = JobRepo(conn)
    job = Job(source="lever", source_job_id=sjid, title="Engineer", company="Acme",
              url=f"https://x/{sjid}", description=desc)
    repo.add(job)
    repo.set_state(job.id, JobState.DESCRIBED)
    return repo.get(job.id)


def test_record_batch_gaps_only_records_missing(conn: sqlite3.Connection):
    """Skills already in the bank are NOT recorded as gaps; missing ones are."""
    _job(conn, sjid="g1", desc="Python and AWS and Kubernetes")
    bank = FactBank(skills=["Python"])  # has Python; missing AWS + Kubernetes
    gap_repo = SkillGapRepo(conn)

    bumps = record_batch_gaps(JobRepo(conn).list_all_with_description(), bank, gap_repo)

    assert bumps == 2  # AWS + Kubernetes
    open_gaps = {g.skill for g in gap_repo.list_open()}
    assert open_gaps == {"AWS", "Kubernetes"}


def test_record_batch_gaps_counts_recurrence(conn: sqlite3.Connection):
    """A skill demanded by 3 JDs ends at count 3 (the recurrence ranking signal)."""
    for i in range(3):
        _job(conn, sjid=f"r{i}", desc="We need Rust here")
    bank = FactBank(skills=[])
    gap_repo = SkillGapRepo(conn)

    record_batch_gaps(JobRepo(conn).list_all_with_description(), bank, gap_repo)

    assert gap_repo.get("Rust").count == 3


def test_build_proposals_ranks_and_filters(conn: sqlite3.Connection):
    _job(conn, sjid="p1", desc="Python AWS Docker")
    _job(conn, sjid="p2", desc="AWS Docker")
    _job(conn, sjid="p3", desc="AWS")
    bank = FactBank(skills=["Python"])
    gap_repo = SkillGapRepo(conn)
    record_batch_gaps(JobRepo(conn).list_all_with_description(), bank, gap_repo)

    proposals = build_proposals(bank, gap_repo)
    skills = [p.skill for p in proposals]
    # AWS demanded by 3, Docker by 2; Python is in-bank so excluded. Ranked by count desc.
    assert skills[0] == "AWS"
    assert "Docker" in skills
    assert "Python" not in skills
    assert proposals[0].count == 3


def test_build_proposals_min_count(conn: sqlite3.Connection):
    _job(conn, sjid="m1", desc="AWS Docker Docker")  # AWS 1x, Docker 1x (one job)
    _job(conn, sjid="m2", desc="AWS")
    bank = FactBank(skills=[])
    gap_repo = SkillGapRepo(conn)
    record_batch_gaps(JobRepo(conn).list_all_with_description(), bank, gap_repo)

    high = build_proposals(bank, gap_repo, min_count=2)
    assert [p.skill for p in high] == ["AWS"]  # only AWS reached 2


def test_build_proposals_excludes_in_bank_defensively(conn: sqlite3.Connection):
    """A gap recorded earlier but since added to the bank must not re-propose."""
    _job(conn, sjid="d1", desc="AWS")
    gap_repo = SkillGapRepo(conn)
    record_batch_gaps(JobRepo(conn).list_all_with_description(), FactBank(skills=[]), gap_repo)
    # Now the user has AWS in the bank.
    bank = FactBank(skills=["AWS"])
    assert build_proposals(bank, gap_repo) == []


def test_set_status_moves_gap_out_of_open(conn: sqlite3.Connection):
    gap_repo = SkillGapRepo(conn)
    gap_repo.bump("Rust")
    assert gap_repo.list_open() != []
    gap_repo.set_status("Rust", "certified")
    assert gap_repo.list_open() == []


def test_vocabulary_is_nonempty():
    assert len(DEFAULT_SKILL_VOCABULARY) > 30
    assert "Python" in DEFAULT_SKILL_VOCABULARY
