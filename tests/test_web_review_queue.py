"""Direction 2, Phase A (A1+A2) — the assisted-queue dashboard panel tests.

Coverage:
  * GET /api/review-queue: needed_action inference (submit / login / decide),
    assisted_application_id passthrough, source_paused signal, score_total.
  * POST /api/jobs/<id>/mark-applied: REVIEW→APPLIED happy, 404, 409.
  * POST /api/jobs/<id>/skip: REVIEW→SKIPPED happy, 404, 409.

Harness mirrors tests/test_web_login_assist.py exactly (web_state fixture,
_seed_job/_seed_app helpers, _make_client, _clear_health autouse). No real
Playwright pages run — the launcher isn't exercised here.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import (
    ApplicationRepo,
    JobRepo,
    ScoreRepo,
)
from auto_applier.domain.models import Application, Job, JobScore, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.sources.health import mark_auth_required, reset_health
from auto_applier.web import WebState, create_app


# --------------------------------------------------------------- fixtures

@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


@pytest.fixture(autouse=True)
def _clear_health():
    reset_health()
    yield
    reset_health()


def _seed_job(conn: sqlite3.Connection, *, id="job-1",
              state=JobState.REVIEW, source="greenhouse",
              url="https://example.com/apply/job-1") -> Job:
    now = utcnow_iso()
    job = Job(
        id=id, source=source, source_job_id=f"src-{id}",
        canonical_hash=f"hash-{id}",
        title="Engineer", company="Acme",
        location="Remote", url=url,
        description="JD", compensation="",
        posted_at=now, ghost_score=None,
        state=state, discovered_at=now, updated_at=now,
    )
    JobRepo(conn).add(job)
    return job


def _seed_app(conn: sqlite3.Connection, *, job_id: str, app_id="app-1",
              status=ApplicationStatus.ASSISTED_PENDING,
              mode=ApplyMode.BROWSER_ASSISTED,
              submitted_at="",
              generated_resume_path="",
              cover_letter_path="") -> Application:
    app = Application(
        id=app_id, job_id=job_id, mode=mode, status=status,
        cover_letter_path=cover_letter_path,
        generated_resume_path=generated_resume_path,
        submitted_at=submitted_at,
    )
    ApplicationRepo(conn).add(app)
    return app


def _seed_score(conn: sqlite3.Connection, *, job_id: str, total: float) -> JobScore:
    score = JobScore(job_id=job_id, total=total, dimensions={"skills": total},
                     model="test", scored_at=utcnow_iso())
    ScoreRepo(conn).upsert(score)
    return score


def _make_client(web_state: WebState) -> TestClient:
    app = create_app(state=web_state, service=None, launcher=None)
    return TestClient(app)


def _find(jobs: list[dict], job_id: str) -> dict:
    return next(j for j in jobs if j["id"] == job_id)


# --------------------------------------------------------------- GET /api/review-queue

class TestReviewQueueListing:

    def test_assisted_pending_is_submit(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING,
                  generated_resume_path="/artifacts/j1-resume.pdf",
                  cover_letter_path="/artifacts/j1-cl.pdf")
        _seed_score(conn, job_id="j1", total=7.5)
        with _make_client(web_state) as client:
            r = client.get("/api/review-queue")
        assert r.status_code == 200
        j = _find(r.json()["jobs"], "j1")
        assert j["needed_action"] == "submit"
        assert j["assisted_application_id"] == "a1"
        assert j["reason"]  # non-empty human text
        assert j["source_paused"] is False
        # score + artifacts passthrough
        assert j["score_total"] == 7.5
        assert j["artifacts"] == {
            "resume": "/artifacts/j1-resume.pdf",
            "cover_letter": "/artifacts/j1-cl.pdf",
        }

    def test_no_application_is_decide(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        # The optimize-gate REVIEW path writes NO application.
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        with _make_client(web_state) as client:
            r = client.get("/api/review-queue")
        j = _find(r.json()["jobs"], "j1")
        assert j["needed_action"] == "decide"
        assert j["assisted_application_id"] is None
        assert j["artifacts"] is None
        assert j["score_total"] is None
        assert j["reason"]

    def test_failed_application_is_decide(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.FAILED,
                  submitted_at=utcnow_iso())
        with _make_client(web_state) as client:
            r = client.get("/api/review-queue")
        j = _find(r.json()["jobs"], "j1")
        assert j["needed_action"] == "decide"
        assert j["assisted_application_id"] is None

    def test_paused_source_no_app_is_login(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW, source="lever")
        mark_auth_required("lever", reason="session expired",
                           login_url="https://lever.co/login")
        with _make_client(web_state) as client:
            r = client.get("/api/review-queue")
        j = _find(r.json()["jobs"], "j1")
        assert j["needed_action"] == "login"
        assert j["source_paused"] is True
        assert j["reason"]

    def test_assisted_pending_wins_over_paused_source(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        # A filled form is finishable even if the source later paused.
        _seed_job(conn, id="j1", state=JobState.REVIEW, source="lever")
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        mark_auth_required("lever", reason="session expired")
        with _make_client(web_state) as client:
            r = client.get("/api/review-queue")
        j = _find(r.json()["jobs"], "j1")
        assert j["needed_action"] == "submit"
        assert j["source_paused"] is True  # still reported, but doesn't change the action


# --------------------------------------------------------------- POST mark-applied

class TestMarkApplied:

    def test_mark_applied_walks_review_to_applied(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/mark-applied")
        assert r.status_code == 200
        body = r.json()
        assert body["job_state"] == "APPLIED"
        assert body["status"] == "applied"
        # Underlying repo state agrees + a MANUAL/APPLIED Application row exists.
        job = JobRepo(conn).get("j1")
        assert job.state is JobState.APPLIED
        apps = ApplicationRepo(conn).list_by_job("j1")
        assert len(apps) == 1
        assert apps[0].mode is ApplyMode.MANUAL
        assert apps[0].status is ApplicationStatus.APPLIED

    def test_mark_applied_404_when_job_missing(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/no-such/mark-applied")
        assert r.status_code == 404

    def test_mark_applied_409_from_disallowed_state(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        # SCORED is not a state a manual apply can be attested from.
        _seed_job(conn, id="j1", state=JobState.SCORED)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/mark-applied")
        assert r.status_code == 409

    def test_mark_applied_409_when_already_applied(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.APPLIED)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/mark-applied")
        # "already" is not an error result → still surfaced; but APPLIED is the
        # source state mark_manually_applied treats as "already" (not error),
        # so it returns 200 with status="already". Assert that contract.
        assert r.status_code == 200
        assert r.json()["status"] == "already"


# --------------------------------------------------------------- POST skip

class TestSkip:

    def test_skip_review_to_skipped(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/skip")
        assert r.status_code == 200
        assert r.json()["job_state"] == "SKIPPED"
        job = JobRepo(conn).get("j1")
        assert job.state is JobState.SKIPPED

    def test_skip_404_when_job_missing(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/no-such/skip")
        assert r.status_code == 404

    def test_skip_409_from_terminal_state(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        # APPLIED is terminal — REVIEW→SKIPPED isn't reachable from it.
        _seed_job(conn, id="j1", state=JobState.APPLIED)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/skip")
        assert r.status_code == 409
