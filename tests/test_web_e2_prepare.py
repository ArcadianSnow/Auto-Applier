"""E2 on-demand fill route (Phase B) — ``POST /api/jobs/{id}/assisted/prepare``.

Route-level contract only: the worker (``prepare_single``) and the launcher (``open_page``) are fed
stubs, so these tests assert the WIRING — validation order, worker reachability, launcher gating,
PrepareSingleError → HTTP code mapping, and the response shape. The real fill behaviour is covered by
``test_apply_worker.py`` (prepare_single) and ``test_web_login_assist.py`` (the launcher).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import JobRepo
from auto_applier.domain.models import Job, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, JobState
from auto_applier.pipeline.apply_worker import PrepareSingleError
from auto_applier.web import WebState, create_app
from auto_applier.web.headed import LaunchResult


# --------------------------------------------------------------- fixtures / stubs

@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


def _seed_review_job(conn: sqlite3.Connection, *, id="job-1", state=JobState.REVIEW,
                     source="greenhouse", url="https://example.com/apply/job-1") -> Job:
    now = utcnow_iso()
    job = Job(
        id=id, source=source, source_job_id=f"src-{id}", canonical_hash=f"hash-{id}",
        title="Engineer", company="Acme", location="Remote", url=url,
        description="JD", compensation="", posted_at=now, ghost_score=None,
        state=state, discovered_at=now, updated_at=now,
    )
    JobRepo(conn).add(job)
    return job


class _StubPage:
    """An opaque page object — the stub worker just records that it received THIS one."""


class _StubLauncher:
    """Mirrors the bits of HeadedBrowserLauncher the prepare route uses."""

    def __init__(self, *, has_bot=True, page: object | None = None, returns_none=False):
        self.opened: list[str] = []
        self._has_bot = has_bot
        self._page = page if page is not None else _StubPage()
        self._returns_none = returns_none

    @property
    def has_bot_browser(self) -> bool:
        return self._has_bot

    async def open_page(self, url: str = ""):
        self.opened.append(url)
        if self._returns_none:
            return None, LaunchResult(ok=False, mode="unavailable", url=url, note="no bot page")
        return self._page, LaunchResult(ok=True, mode="bot_browser", url=url, note="stub")

    async def open(self, url: str) -> LaunchResult:  # unused by the prepare route, kept for parity
        return LaunchResult(ok=True, mode="bot_browser", url=url, note="stub")


class _StubWorker:
    """Records prepare_single calls; returns a canned outcome or raises."""

    def __init__(self, *, outcome=None, raises: Exception | None = None, in_progress=False):
        self._outcome = outcome
        self._raises = raises
        self._in_progress = in_progress
        self.calls: list[tuple[str, object]] = []

    @property
    def prepare_in_progress(self) -> bool:
        return self._in_progress

    async def prepare_single(self, job_id: str, *, page):
        self.calls.append((job_id, page))
        if self._raises is not None:
            raise self._raises
        return self._outcome


def _outcome(*, filled: dict[str, bool], status=ApplicationStatus.ASSISTED_PENDING):
    return SimpleNamespace(filled=filled, status=status)


def _make_client(web_state: WebState, *, launcher, worker_holder=None) -> TestClient:
    app = create_app(state=web_state, service=None, launcher=launcher)
    if worker_holder is not None:
        app.state.apply_worker_holder = worker_holder
    return TestClient(app)


# --------------------------------------------------------------- happy path

def test_prepare_success_fills_and_returns_summary(web_state, conn):
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher()
    worker = _StubWorker(outcome=_outcome(filled={"a": True, "b": False, "c": True}))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")

    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "j1"
    assert body["launch"]["mode"] == "bot_browser"
    assert body["outcome"] == {"status": "ASSISTED_PENDING", "filled": 2, "left": 1}
    # The route opened the job's apply URL and drove the fill on THAT launcher page.
    assert launcher.opened == ["https://example.com/apply/job-1"]
    assert worker.calls and worker.calls[0][0] == "j1"
    assert worker.calls[0][1] is launcher._page


# --------------------------------------------------------------- gating / validation

def test_prepare_409_when_worker_unavailable(web_state, conn):
    """No worker holder (--no-scheduler / pre-onboarding) → 409, and no tab is opened."""
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher()
    with _make_client(web_state, launcher=launcher) as client:  # no worker_holder
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 409
    assert launcher.opened == []


def test_prepare_409_when_holder_has_no_worker(web_state, conn):
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher()
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": None}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 409
    assert launcher.opened == []


def test_prepare_409_when_no_bot_browser(web_state, conn):
    """A default-browser-only launcher can't drive a fill → 409 before any open."""
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher(has_bot=False)
    worker = _StubWorker(outcome=_outcome(filled={"a": True}))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 409
    assert launcher.opened == []
    assert worker.calls == []


def test_prepare_404_when_job_missing(web_state, conn):
    launcher = _StubLauncher()
    worker = _StubWorker(outcome=_outcome(filled={"a": True}))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/nope/assisted/prepare")
    assert r.status_code == 404
    assert launcher.opened == []          # no tab popped for a bad id
    assert worker.calls == []


def test_prepare_422_when_job_has_no_url(web_state, conn):
    _seed_review_job(conn, id="j1", url="")
    launcher = _StubLauncher()
    worker = _StubWorker(outcome=_outcome(filled={"a": True}))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 422
    assert launcher.opened == []
    assert worker.calls == []


def test_prepare_409_when_a_fill_is_already_in_progress(web_state, conn):
    """The route's fast 409 (worker.prepare_in_progress) fires BEFORE a tab is opened."""
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher()
    worker = _StubWorker(outcome=_outcome(filled={"a": True}), in_progress=True)
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 409
    assert launcher.opened == []
    assert worker.calls == []


def test_prepare_409_when_launcher_returns_no_page(web_state, conn):
    """open_page can soft-fail to (None, result) (new_page raised) → 409, worker never driven."""
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher(returns_none=True)
    worker = _StubWorker(outcome=_outcome(filled={"a": True}))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == 409
    assert launcher.opened == ["https://example.com/apply/job-1"]  # it tried
    assert worker.calls == []


@pytest.mark.parametrize("code", [404, 409, 422])
def test_prepare_maps_prepare_single_error_code(web_state, conn, code):
    """A PrepareSingleError raised by prepare_single (e.g. a state change between the pre-read and
    the fill) maps to its carried HTTP code."""
    _seed_review_job(conn, id="j1")
    launcher = _StubLauncher()
    worker = _StubWorker(raises=PrepareSingleError("boom", code=code))
    with _make_client(web_state, launcher=launcher, worker_holder={"worker": worker}) as client:
        r = client.post("/api/jobs/j1/assisted/prepare")
    assert r.status_code == code
    assert worker.calls and worker.calls[0][0] == "j1"   # it DID reach prepare_single
