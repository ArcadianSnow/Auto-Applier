"""Repositories + engine (spec §4): persistence, validated state changes, dedup queries."""

from __future__ import annotations

import pytest

from auto_applier.db import tx
from auto_applier.db.repositories import (
    AnswerRepo,
    ApplicationRepo,
    JobRepo,
    ScoreRepo,
    SkillGapRepo,
)
from auto_applier.domain.models import Answer, Application, Job, JobScore
from auto_applier.domain.state import (
    ApplicationStatus,
    ApplyMode,
    InvalidTransition,
    JobState,
)


def _job(source="greenhouse", sid="1", title="Data Analyst", company="Acme", **kw) -> Job:
    return Job(source=source, source_job_id=sid, title=title, company=company, **kw)


def test_add_and_get_job(conn):
    repo = JobRepo(conn)
    job = repo.add(_job(canonical_hash="h1"))
    fetched = repo.get(job.id)
    assert fetched is not None
    assert fetched.title == "Data Analyst"
    assert fetched.state is JobState.DISCOVERED


def test_unique_source_constraint(conn):
    repo = JobRepo(conn)
    repo.add(_job(sid="dup"))
    with pytest.raises(Exception):  # sqlite IntegrityError on UNIQUE(source, source_job_id)
        repo.add(_job(sid="dup"))


def test_upsert_discovered_is_idempotent(conn):
    repo = JobRepo(conn)
    first = repo.upsert_discovered(_job(sid="x"))
    second = repo.upsert_discovered(_job(sid="x", title="Different Title"))
    assert first.id == second.id  # same row returned, no duplicate
    assert second.title == "Data Analyst"  # original preserved


def test_set_state_valid_and_invalid(conn):
    repo = JobRepo(conn)
    job = repo.add(_job())
    repo.set_state(job.id, JobState.DESCRIBED)
    assert repo.get(job.id).state is JobState.DESCRIBED
    with pytest.raises(InvalidTransition):
        repo.set_state(job.id, JobState.APPLIED)  # DESCRIBED → APPLIED not allowed


def test_update_fields_rejects_state(conn):
    repo = JobRepo(conn)
    job = repo.add(_job())
    repo.update_fields(job.id, description="full JD text", ghost_score=2.0)
    assert repo.get(job.id).description == "full JD text"
    with pytest.raises(ValueError, match="set_state"):
        repo.update_fields(job.id, state="APPLIED")


def test_applied_canonical_hashes_only_counts_applied(conn):
    repo = JobRepo(conn)
    applied = repo.add(_job(sid="a", canonical_hash="hash-applied"))
    # walk it to APPLIED
    for s in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED,
              JobState.QUEUED_APPLY, JobState.APPLYING, JobState.APPLIED):
        repo.set_state(applied.id, s)
    repo.add(_job(sid="b", canonical_hash="hash-pending"))  # stays DISCOVERED
    hashes = repo.applied_canonical_hashes()
    assert "hash-applied" in hashes
    assert "hash-pending" not in hashes  # unconfirmed never dedups (spec §5)


def test_company_applied_count(conn):
    repo = JobRepo(conn)
    j = repo.add(_job(sid="c", company="Globex"))
    assert repo.company_applied_count("Globex") == 0
    for s in (JobState.DESCRIBED, JobState.SCORED, JobState.DECIDED,
              JobState.QUEUED_APPLY, JobState.APPLYING, JobState.APPLIED):
        repo.set_state(j.id, s)
    assert repo.company_applied_count("Globex") == 1


def test_score_upsert(conn):
    JobRepo(conn).add(_job(sid="s"))
    job = JobRepo(conn).get_by_source("greenhouse", "s")
    repo = ScoreRepo(conn)
    repo.upsert(JobScore(job_id=job.id, total=7.5, dimensions={"skills": 8.0}, model="m"))
    repo.upsert(JobScore(job_id=job.id, total=6.0, dimensions={"skills": 6.0}, model="m2"))
    got = repo.get(job.id)
    assert got.total == 6.0  # upsert overwrote
    assert got.dimensions["skills"] == 6.0


def test_score_foreign_key_cascade(conn):
    repo = JobRepo(conn)
    job = repo.add(_job(sid="fk"))
    ScoreRepo(conn).upsert(JobScore(job_id=job.id, total=5.0))
    conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    assert ScoreRepo(conn).get(job.id) is None  # cascaded (PRAGMA foreign_keys=ON)


def test_application_lifecycle(conn):
    job = JobRepo(conn).add(_job(sid="app"))
    repo = ApplicationRepo(conn)
    app = repo.add(Application(
        job_id=job.id, mode=ApplyMode.BROWSER_AUTO, status=ApplicationStatus.APPLYING
    ))
    repo.set_status(app.id, ApplicationStatus.APPLIED, submitted_at="2026-05-26T00:00:00+00:00")
    got = repo.get(app.id)
    assert got.status is ApplicationStatus.APPLIED
    assert got.submitted_at.startswith("2026-05-26")
    assert [a.id for a in repo.list_by_job(job.id)] == [app.id]


def test_skill_gap_bump(conn):
    repo = SkillGapRepo(conn)
    repo.bump("Tableau")
    g = repo.bump("Tableau")
    assert g.count == 2
    repo.bump("Spark")
    assert {g.skill for g in repo.list_open(min_count=2)} == {"Tableau"}


def test_answer_upsert(conn):
    repo = AnswerRepo(conn)
    repo.upsert(Answer(question="Authorized to work in US?", answer="Yes", source="user"))
    repo.upsert(Answer(question="Authorized to work in US?", answer="No", source="user"))
    assert repo.get("Authorized to work in US?").answer == "No"
    assert len(repo.all()) == 1


def test_tx_rolls_back_on_error(conn):
    repo = JobRepo(conn)
    with pytest.raises(RuntimeError):
        with tx(conn):
            repo.add(_job(sid="rollback"))
            raise RuntimeError("boom")
    assert repo.get_by_source("greenhouse", "rollback") is None
