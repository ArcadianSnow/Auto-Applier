"""Batched assisted review, Phase 3 — the "In Progress" page + its feed.

Coverage:
  * GET /api/batch: disabled (no batch) → enabled=false; lists batch members with their COMPLETE
    proposed application; proposed=null for a member with no saved artifact; skips a member whose
    job row is gone; passes through the latest ASSISTED_PENDING attempt id.
  * POST /api/batch/release: 409 when disabled; clears members + opens a fresh batch id.
  * GET /api/status: carries the review_batch snapshot when enabled, null when off.
  * GET /in-progress: the page shell renders.

Harness mirrors tests/test_web_review_queue.py (web_state via create_app + TestClient).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Application, Job, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.pipeline.review_batch import ReviewBatch
from auto_applier.resume.proposed import (
    ProposedApplication,
    ProposedField,
    save_proposed,
)
from auto_applier.web import WebState, create_app


# --------------------------------------------------------------- fixtures / helpers

def _web_state(settings: Settings, *, review_batch=None) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
        review_batch=review_batch,
    )


def _make_client(web_state: WebState) -> TestClient:
    return TestClient(create_app(state=web_state, service=None, launcher=None))


def _seed_job(conn: sqlite3.Connection, *, id: str, state=JobState.REVIEW) -> Job:
    now = utcnow_iso()
    job = Job(
        id=id, source="greenhouse", source_job_id=f"src-{id}", canonical_hash=f"h-{id}",
        title="Data Engineer", company="Acme", location="Remote",
        url=f"https://example.com/apply/{id}", description="JD", compensation="",
        posted_at=now, ghost_score=None, state=state, discovered_at=now, updated_at=now,
    )
    JobRepo(conn).add(job)
    return job


def _save_proposed(settings: Settings, job_id: str) -> None:
    pa = ProposedApplication(
        job_id=job_id,
        resume_path="/artifacts/uploads/Resume.pdf",
        cover_letter_path="",
        fields=[
            ProposedField(
                key="applicant:email", label="Email", value="pat@example.com",
                kind="standard", source="applicant", confidence=1.0,
                required=True, needs_verify=False, is_draft=False,
            ),
            ProposedField(
                key="q:cq1", label="Why this company?", value="A grounded first draft.",
                kind="textarea", source="draft", confidence=0.0,
                required=True, needs_verify=True, is_draft=True,
                note="freeform DRAFT pre-filled for review",
            ),
            ProposedField(
                key="q:cq2", label="What is your gender?", value="",
                kind="select", source="review", confidence=0.0,
                required=False, needs_verify=True, is_draft=False, note="EEO: you decide",
            ),
        ],
    )
    save_proposed(settings, pa)


# --------------------------------------------------------------- GET /api/batch

def test_batch_feed_disabled_when_no_batch(settings):
    with _make_client(_web_state(settings)) as client:
        r = client.get("/api/batch")
    assert r.status_code == 200
    body = r.json()
    assert body == {"enabled": False, "batch": None, "jobs": []}


def test_batch_feed_lists_members_with_proposed(settings, conn):
    _seed_job(conn, id="j1")
    _seed_job(conn, id="j2")
    _save_proposed(settings, "j1")        # j1 has a proposed set; j2 doesn't yet
    batch = ReviewBatch(size=5)
    batch.add("j1")
    batch.add("j2")

    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.get("/api/batch")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["batch"]["count"] == 2
    jobs = {j["id"]: j for j in body["jobs"]}
    assert set(jobs) == {"j1", "j2"}

    j1 = jobs["j1"]
    assert j1["title"] == "Data Engineer"
    assert j1["proposed"] is not None
    assert j1["proposed"]["summary"] == {
        "total": 3, "confident": 1, "drafted": 1, "needs_verify": 2,
    }
    labels = [f["label"] for f in j1["proposed"]["fields"]]
    assert "Why this company?" in labels
    draft = next(f for f in j1["proposed"]["fields"] if f["key"] == "q:cq1")
    assert draft["is_draft"] is True and draft["value"]

    assert jobs["j2"]["proposed"] is None  # no artifact saved → null, page degrades gracefully


def test_batch_feed_passes_assisted_application_id(settings, conn):
    _seed_job(conn, id="j1")
    ApplicationRepo(conn).add(Application(
        id="a1", job_id="j1", mode=ApplyMode.BROWSER_ASSISTED,
        status=ApplicationStatus.ASSISTED_PENDING,
    ))
    batch = ReviewBatch(size=5)
    batch.add("j1")
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.get("/api/batch")
    assert r.json()["jobs"][0]["assisted_application_id"] == "a1"


def test_batch_feed_skips_missing_job_row(settings, conn):
    _seed_job(conn, id="j1")
    batch = ReviewBatch(size=5)
    batch.add("j1")
    batch.add("ghost")                    # no job row for this id
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.get("/api/batch")
    ids = [j["id"] for j in r.json()["jobs"]]
    assert ids == ["j1"]                   # ghost skipped, no error


# --------------------------------------------------------------- POST /api/batch/release

def test_batch_release_409_when_disabled(settings):
    with _make_client(_web_state(settings)) as client:
        r = client.post("/api/batch/release")
    assert r.status_code == 409


def test_batch_release_clears_and_opens_fresh_batch(settings, conn):
    _seed_job(conn, id="j1")
    batch = ReviewBatch(size=2)
    batch.add("j1")
    batch.add("j2")
    assert batch.is_holding() is True
    old_id = batch.batch_id

    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.post("/api/batch/release")
        assert r.status_code == 200
        snap = r.json()["batch"]
        assert snap["count"] == 0
        assert snap["holding"] is False
        assert snap["batch_id"] != old_id
        # the feed is now empty
        assert client.get("/api/batch").json()["jobs"] == []
    assert batch.is_holding() is False


# --------------------------------------------------------------- /api/status badge

def test_status_carries_review_batch_when_enabled(settings, conn):
    batch = ReviewBatch(size=3)
    batch.add("j1")
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.get("/api/status")
    rb = r.json()["review_batch"]
    assert rb is not None
    assert rb["size"] == 3 and rb["count"] == 1


def test_status_review_batch_null_when_disabled(settings, conn):
    with _make_client(_web_state(settings)) as client:
        r = client.get("/api/status")
    assert r.json()["review_batch"] is None


# --------------------------------------------------------------- page shell

def test_in_progress_page_renders(settings):
    with _make_client(_web_state(settings)) as client:
        r = client.get("/in-progress")
    assert r.status_code == 200
    assert "In Progress" in r.text
    assert "inProgress()" in r.text       # the Alpine factory is wired


# --------------------------------------------------------------- disposition (Phase 4)

def test_mark_applied_disposes_batch_member(settings, conn):
    _seed_job(conn, id="j1", state=JobState.REVIEW)
    batch = ReviewBatch(size=2)
    batch.add("j1")
    batch.add("j2")                        # second member keeps the batch from auto-advancing
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        assert client.post("/api/jobs/j1/mark-applied").status_code == 200
    assert batch.snapshot()["dispositions"]["j1"] == "applied"
    assert JobRepo(conn).get("j1").state is JobState.APPLIED


def test_skip_disposes_batch_member(settings, conn):
    _seed_job(conn, id="j1", state=JobState.REVIEW)
    batch = ReviewBatch(size=2)
    batch.add("j1")
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        assert client.post("/api/jobs/j1/skip").status_code == 200
    assert batch.snapshot()["dispositions"]["j1"] == "skipped"
    assert JobRepo(conn).get("j1").state is JobState.SKIPPED


def test_needs_work_disposes_and_leaves_job_in_review(settings, conn):
    _seed_job(conn, id="j1", state=JobState.REVIEW)
    batch = ReviewBatch(size=2)
    batch.add("j1")
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        r = client.post("/api/jobs/j1/needs-work")
        assert r.status_code == 200
        assert r.json()["disposition"] == "needs_work"
    assert batch.snapshot()["dispositions"]["j1"] == "needs_work"
    assert JobRepo(conn).get("j1").state is JobState.REVIEW   # side-lane: state unchanged


def test_needs_work_404_when_no_job(settings, conn):
    batch = ReviewBatch(size=1)
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        assert client.post("/api/jobs/nope/needs-work").status_code == 404


def test_needs_work_409_when_batching_off(settings, conn):
    _seed_job(conn, id="j1", state=JobState.REVIEW)
    with _make_client(_web_state(settings)) as client:        # no review_batch
        assert client.post("/api/jobs/j1/needs-work").status_code == 409


def test_feed_carries_disposition(settings, conn):
    _seed_job(conn, id="j1", state=JobState.REVIEW)
    _save_proposed(settings, "j1")
    batch = ReviewBatch(size=2)
    batch.add("j1")
    batch.dispose("j1", "applied")
    with _make_client(_web_state(settings, review_batch=batch)) as client:
        body = client.get("/api/batch").json()
    j1 = next(j for j in body["jobs"] if j["id"] == "j1")
    assert j1["disposition"] == "applied"
    assert body["batch"]["pending"] == 0   # j1 is the only member and it's dispositioned
    assert body["batch"]["all_dispositioned"] is True
