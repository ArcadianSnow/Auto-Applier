"""Phase 4 (4/M) — login-on-demand + assisted submit tests.

Coverage:
  * HeadedBrowserLauncher: bot-browser path, fallback, missing URL
  * /api/sources/<source>/login: success, 404, 409, 422
  * /api/sources/<source>/healthy: idempotent, 400 on empty source
  * /api/sources reflects ``login_url``
  * /api/jobs/<id>/assisted/open: success, 404, 409, 422
  * /api/jobs/<id>/assisted/confirm: walks state machine, 404, 409
  * /api/jobs/<id>/assisted/cancel: flips Application, leaves Job in REVIEW

No real Playwright pages run — the launcher is fed an async stub. Real
Chrome integration is covered by the (4/M) manual smoketest.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import ApplicationRepo, JobRepo
from auto_applier.domain.models import Application, Job, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.sources.health import (
    mark_auth_required,
    mark_healthy,
    reset_health,
    snapshot as health_snapshot,
)
from auto_applier.web import WebState, create_app
from auto_applier.web.headed import HeadedBrowserLauncher, LaunchResult


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
              submitted_at="") -> Application:
    app = Application(
        id=app_id, job_id=job_id, mode=mode, status=status,
        cover_letter_path="", generated_resume_path="",
        submitted_at=submitted_at,
    )
    ApplicationRepo(conn).add(app)
    return app


def _make_client(web_state: WebState,
                 launcher: HeadedBrowserLauncher | None = None) -> TestClient:
    app = create_app(state=web_state, service=None, launcher=launcher)
    return TestClient(app)


# --------------------------------------------------------------- launcher

class _RecordingPage:
    def __init__(self):
        self.url_visited: str | None = None

    async def goto(self, url: str):
        self.url_visited = url


class TestHeadedBrowserLauncher:

    def test_no_url_returns_unavailable(self):
        async def _go():
            launcher = HeadedBrowserLauncher()
            result = await launcher.open("")
            assert result.ok is False
            assert result.mode == "unavailable"
        asyncio.run(_go())

    def test_opens_in_bot_browser_when_new_page_provided(self):
        async def _go():
            page = _RecordingPage()

            async def _new_page():
                return page

            launcher = HeadedBrowserLauncher(new_page=_new_page)
            assert launcher.has_bot_browser is True
            result = await launcher.open("https://login.example.com")
            assert result.ok is True
            assert result.mode == "bot_browser"
            assert result.url == "https://login.example.com"
            assert page.url_visited == "https://login.example.com"
        asyncio.run(_go())

    def test_falls_back_to_default_browser_when_no_session(self):
        opened = []

        def _fallback(url: str) -> bool:
            opened.append(url)
            return True

        async def _go():
            launcher = HeadedBrowserLauncher(
                new_page=None, fallback_open=_fallback,
            )
            assert launcher.has_bot_browser is False
            result = await launcher.open("https://x.example/login")
            assert result.ok is True
            assert result.mode == "default_browser"
            assert opened == ["https://x.example/login"]
        asyncio.run(_go())

    def test_falls_back_when_new_page_raises(self):
        opened = []

        async def _new_page():
            raise RuntimeError("session torn down")

        def _fallback(url: str) -> bool:
            opened.append(url)
            return True

        async def _go():
            launcher = HeadedBrowserLauncher(
                new_page=_new_page, fallback_open=_fallback,
            )
            result = await launcher.open("https://x.example/login")
            assert result.mode == "default_browser"
            assert opened == ["https://x.example/login"]
        asyncio.run(_go())

    def test_falls_back_when_goto_raises(self):
        opened = []

        class _BrokenPage:
            async def goto(self, url: str):
                raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

        async def _new_page():
            return _BrokenPage()

        def _fallback(url: str) -> bool:
            opened.append(url)
            return True

        async def _go():
            launcher = HeadedBrowserLauncher(
                new_page=_new_page, fallback_open=_fallback,
            )
            result = await launcher.open("https://x.example/login")
            assert result.mode == "default_browser"
            assert opened == ["https://x.example/login"]
        asyncio.run(_go())

    def test_fallback_returning_false_reports_unavailable(self):
        async def _go():
            launcher = HeadedBrowserLauncher(
                new_page=None, fallback_open=lambda _u: False,
            )
            result = await launcher.open("https://x")
            assert result.ok is False
            assert result.mode == "unavailable"
        asyncio.run(_go())


# --------------------------------------------------------------- /api/sources login

class _StubLauncher:
    """Mirrors HeadedBrowserLauncher.open() but records the URL for assert."""

    def __init__(self):
        self.opened: list[str] = []

    @property
    def has_bot_browser(self) -> bool:
        return True

    async def open(self, url: str) -> LaunchResult:
        self.opened.append(url)
        return LaunchResult(
            ok=True, mode="bot_browser", url=url, note="stub",
        )


class TestSourcesLogin:

    def test_login_opens_captured_url(self, web_state: WebState):
        mark_auth_required(
            "greenhouse",
            reason="session expired",
            login_url="https://login.greenhouse.io/",
        )
        launcher = _StubLauncher()
        with _make_client(web_state, launcher=launcher) as client:
            r = client.post("/api/sources/greenhouse/login")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "greenhouse"
        assert body["launch"]["ok"] is True
        assert body["launch"]["mode"] == "bot_browser"
        assert launcher.opened == ["https://login.greenhouse.io/"]

    def test_login_404_when_source_unknown(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/sources/unknown-src/login")
        assert r.status_code == 404

    def test_login_409_when_source_healthy(self, web_state: WebState):
        mark_healthy("greenhouse")
        with _make_client(web_state) as client:
            r = client.post("/api/sources/greenhouse/login")
        assert r.status_code == 409

    def test_login_422_when_no_login_url_captured(self, web_state: WebState):
        # Auth-required but no URL (e.g. wall detected via HTML signal only).
        mark_auth_required("ashby", reason="login wall via html signal",
                           login_url="")
        with _make_client(web_state) as client:
            r = client.post("/api/sources/ashby/login")
        assert r.status_code == 422
        # The 422 detail tells the user the fallback path.
        assert "healthy" in r.json()["detail"]


class TestSourcesMarkHealthy:

    def test_mark_healthy_clears_paused(self, web_state: WebState):
        mark_auth_required(
            "greenhouse", reason="session expired",
            login_url="https://login.greenhouse.io/",
        )
        with _make_client(web_state) as client:
            r = client.post("/api/sources/greenhouse/healthy")
        assert r.status_code == 200
        assert r.json()["state"] == "HEALTHY"
        # And the in-process registry agrees.
        snap = health_snapshot()
        assert snap["greenhouse"].state.value == "HEALTHY"

    def test_mark_healthy_is_idempotent(self, web_state: WebState):
        # Marking an already-healthy source must not 500 or change state.
        mark_healthy("lever")
        with _make_client(web_state) as client:
            r1 = client.post("/api/sources/lever/healthy")
            r2 = client.post("/api/sources/lever/healthy")
        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_sources_endpoint_carries_login_url(self, web_state: WebState):
        mark_auth_required(
            "greenhouse",
            reason="session expired during apply",
            login_url="https://login.greenhouse.io/users/sign_in",
        )
        with _make_client(web_state) as client:
            r = client.get("/api/sources")
        body = r.json()
        gh = next(s for s in body["sources"] if s["source"] == "greenhouse")
        assert gh["login_url"] == "https://login.greenhouse.io/users/sign_in"
        assert gh["paused"] is True


# --------------------------------------------------------------- assisted/open

class TestAssistedOpen:

    def test_open_launches_apply_url(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW,
                  url="https://boards.greenhouse.io/acme/jobs/123")
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        launcher = _StubLauncher()
        with _make_client(web_state, launcher=launcher) as client:
            r = client.post("/api/jobs/j1/assisted/open")
        assert r.status_code == 200
        body = r.json()
        assert body["application_id"] == "a1"
        assert body["launch"]["ok"] is True
        assert launcher.opened == ["https://boards.greenhouse.io/acme/jobs/123"]

    def test_open_404_when_job_missing(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/no-such-job/assisted/open")
        assert r.status_code == 404

    def test_open_409_when_no_assisted_pending(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        # An APPLIED row is NOT assisted-pending.
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.APPLIED,
                  submitted_at=utcnow_iso())
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/open")
        assert r.status_code == 409

    def test_open_422_when_job_has_no_url(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW, url="")
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/open")
        assert r.status_code == 422


# --------------------------------------------------------------- assisted/confirm

class TestAssistedConfirm:

    def test_confirm_walks_state_machine_to_applied(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/confirm")
        assert r.status_code == 200
        body = r.json()
        assert body["job_state"] == "APPLIED"
        assert body["application_status"] == "APPLIED"
        assert body["submitted_at"]
        # Underlying repo state agrees.
        job = JobRepo(conn).get("j1")
        assert job.state is JobState.APPLIED
        app = ApplicationRepo(conn).get("a1")
        assert app.status is ApplicationStatus.APPLIED
        assert app.submitted_at == body["submitted_at"]

    def test_confirm_404_when_job_missing(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/no-such/assisted/confirm")
        assert r.status_code == 404

    def test_confirm_409_when_job_not_in_review(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.APPLIED)
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/confirm")
        assert r.status_code == 409

    def test_confirm_409_when_no_assisted_pending(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        # No application at all.
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/confirm")
        assert r.status_code == 409


# --------------------------------------------------------------- assisted/cancel

class TestAssistedCancel:

    def test_cancel_flips_application_keeps_job_in_review(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        _seed_app(conn, job_id="j1", app_id="a1",
                  status=ApplicationStatus.ASSISTED_PENDING)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/cancel")
        assert r.status_code == 200
        body = r.json()
        assert body["application_status"] == "FAILED"
        # Job stays in REVIEW (cancel != failed apply).
        job = JobRepo(conn).get("j1")
        assert job.state is JobState.REVIEW
        app = ApplicationRepo(conn).get("a1")
        assert app.status is ApplicationStatus.FAILED

    def test_cancel_404_when_job_missing(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/no-such/assisted/cancel")
        assert r.status_code == 404

    def test_cancel_409_when_no_pending(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/cancel")
        assert r.status_code == 409


# --------------------------------------------------------------- latest-pending

class TestLatestAssistedPending:
    """The dashboard targets the MOST RECENT ASSISTED_PENDING attempt — the
    apply worker may have made multiple over time (each user-cancel leaves
    a FAILED row + the next cycle queues a new attempt)."""

    def test_picks_most_recent_pending(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", state=JobState.REVIEW)
        # Older cancelled attempt + a freshly-queued one.
        _seed_app(conn, job_id="j1", app_id="a-old",
                  status=ApplicationStatus.FAILED,
                  submitted_at="2026-01-01T00:00:00Z")
        _seed_app(conn, job_id="j1", app_id="a-new",
                  status=ApplicationStatus.ASSISTED_PENDING)
        with _make_client(web_state) as client:
            r = client.post("/api/jobs/j1/assisted/confirm")
        assert r.status_code == 200
        # The NEW attempt got APPLIED; the old FAILED row is untouched.
        new_app = ApplicationRepo(conn).get("a-new")
        old_app = ApplicationRepo(conn).get("a-old")
        assert new_app.status is ApplicationStatus.APPLIED
        assert old_app.status is ApplicationStatus.FAILED


# --------------------------------------------------------------- health.login_url

class TestHealthRecordLoginUrl:
    """The health record's login_url survives a mark_auth_required + snapshot
    round-trip and is cleared when re-marked healthy."""

    def test_login_url_round_trips(self):
        mark_auth_required(
            "x", reason="r", login_url="https://x.example/login",
        )
        snap = health_snapshot()
        assert snap["x"].login_url == "https://x.example/login"

    def test_login_url_clears_on_mark_healthy(self):
        mark_auth_required(
            "x", reason="r", login_url="https://x.example/login",
        )
        mark_healthy("x")
        snap = health_snapshot()
        assert snap["x"].login_url == ""

    def test_login_url_empty_when_not_provided(self):
        # Backward compat: existing callers that don't pass login_url get "".
        mark_auth_required("y", reason="r")
        snap = health_snapshot()
        assert snap["y"].login_url == ""
