"""Manual-apply service (pipeline/manual_apply.py) — the `av3 applied` write path."""

from __future__ import annotations

import pytest

from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.pipeline.manual_apply import mark_manually_applied


def _seed(conn, *, state=JobState.DECIDED, sid="s1", title="Data Engineer",
          company="Acme", canonical_hash="") -> str:
    job = Job(source="greenhouse", source_job_id=sid, title=title, company=company,
              state=state, canonical_hash=canonical_hash)
    JobRepo(conn).add(job)
    return job.id


def test_decided_to_applied_writes_manual_application(conn):
    jid = _seed(conn, state=JobState.DECIDED)
    res = mark_manually_applied(conn, jid)
    assert res.status == "applied"
    assert JobRepo(conn).get(jid).state is JobState.APPLIED
    apps = ApplicationRepo(conn).list_by_job(jid)
    assert len(apps) == 1
    assert apps[0].mode is ApplyMode.MANUAL
    assert apps[0].status is ApplicationStatus.APPLIED
    assert apps[0].submitted_at  # non-empty timestamp


def test_review_to_applied(conn):
    jid = _seed(conn, state=JobState.REVIEW)
    assert mark_manually_applied(conn, jid).status == "applied"
    assert JobRepo(conn).get(jid).state is JobState.APPLIED


def test_idempotent_already_applied(conn):
    jid = _seed(conn, state=JobState.DECIDED)
    assert mark_manually_applied(conn, jid).status == "applied"
    second = mark_manually_applied(conn, jid)
    assert second.status == "already"
    # No duplicate Application row, state unchanged.
    assert len(ApplicationRepo(conn).list_by_job(jid)) == 1
    assert JobRepo(conn).get(jid).state is JobState.APPLIED


@pytest.mark.parametrize("bad_state", [
    JobState.DISCOVERED, JobState.DESCRIBED, JobState.SCORED,
    JobState.QUEUED_APPLY, JobState.APPLYING, JobState.SKIPPED,
])
def test_guard_rejects_non_decided_review_states(conn, bad_state):
    jid = _seed(conn, state=bad_state, sid=f"s-{bad_state.value}")
    res = mark_manually_applied(conn, jid)
    assert res.status == "error"
    assert JobRepo(conn).get(jid).state is bad_state          # unchanged
    assert ApplicationRepo(conn).list_by_job(jid) == []       # no row written


def test_unknown_id_is_error_not_raise(conn):
    res = mark_manually_applied(conn, "does-not-exist")
    assert res.status == "error"


def test_resume_path_recorded(conn):
    jid = _seed(conn)
    mark_manually_applied(conn, jid, resume_path=r"C:\x\Resume_Data_Platform_Engineer.docx")
    assert ApplicationRepo(conn).list_by_job(jid)[0].generated_resume_path.endswith(".docx")


def test_applied_hash_enters_dedup(conn):
    jid = _seed(conn, canonical_hash="hash-abc")
    mark_manually_applied(conn, jid)
    assert "hash-abc" in JobRepo(conn).applied_canonical_hashes()


def test_sibling_sharing_hash_not_auto_skipped(conn):
    a = _seed(conn, sid="a", canonical_hash="dup")
    b = _seed(conn, sid="b", canonical_hash="dup")
    mark_manually_applied(conn, a)
    assert JobRepo(conn).get(a).state is JobState.APPLIED
    assert JobRepo(conn).get(b).state is JobState.DECIDED  # the sibling is left alone


def test_tx_atomic_on_failure(conn, monkeypatch):
    # Force set_state to blow up AFTER the Application row write; the tx must roll back,
    # leaving no orphan Application row and the job still DECIDED.
    jid = _seed(conn)

    def boom(self, job_id, new_state):
        raise KeyError("simulated mid-tx failure")

    monkeypatch.setattr(JobRepo, "set_state", boom)
    res = mark_manually_applied(conn, jid)
    assert res.status == "error"
    assert ApplicationRepo(conn).list_by_job(jid) == []      # rolled back
    assert JobRepo(conn).get(jid).state is JobState.DECIDED
