"""Phase 4 (1/M) — web skeleton tests.

Coverage:
  * SchedulerService start/stop lifecycle (and idempotency)
  * SchedulerService pause/resume propagates to the scheduler's pause_predicate
  * FastAPI app boots + shuts down cleanly via the lifespan
  * Read-only endpoints return expected JSON shapes against a seeded DB
  * Endpoint behavior degrades cleanly when ``service is None``
  * Index page renders (HTML 200)
  * Source health endpoint reflects the in-memory registry

No real BrowserSession / LLM / scheduler workers run — the service is fed a
sync factory that returns a stub Scheduler whose ``run()`` is awaitable and
respects task cancellation. That isolates the *lifecycle* concerns from the
*pipeline* concerns (which Phase 3 already covers).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.domain.models import Job, utcnow_iso
from auto_applier.domain.state import JobState
from auto_applier.db.repositories import JobRepo
from auto_applier.sources.health import mark_auth_required, mark_healthy, reset_health
from auto_applier.web import SchedulerService, WebState, create_app
from auto_applier.web.service import sync_factory


# --------------------------------------------------------------- stub Scheduler

class _StubScheduler:
    """Drop-in for ``auto_applier.pipeline.Scheduler`` that supports cancellation and
    records the pause-predicate it was built with so tests can poke at it."""

    def __init__(self, pause_predicate):
        self.pause_predicate = pause_predicate
        self.run_called = False
        self.cancelled = False

    async def run(self, max_cycles: int | None = None):
        self.run_called = True
        try:
            # Sleep "forever" until the task is cancelled by stop().
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _make_stub_service():
    """Returns (service, get_scheduler) — service uses the stub, get_scheduler
    reaches into the service after start() to inspect the built stub."""
    holder = {}

    def _build(pause_predicate):
        sched = _StubScheduler(pause_predicate)
        holder["scheduler"] = sched
        return sched

    service = SchedulerService(sync_factory(_build))
    return service, lambda: holder.get("scheduler")


# --------------------------------------------------------------- fixtures

@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    """Build a WebState bound to the same tmp DB the ``conn`` fixture wrote to.

    The ``conn`` fixture is what seeded the schema (via init_app_db) — we
    request it as a fixture dep so the schema is in place before any web
    request runs. The state itself opens fresh per-request connections.
    """
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


@pytest.fixture(autouse=True)
def _clear_health():
    """Each test gets a fresh source-health registry — it's process-global."""
    reset_health()
    yield
    reset_health()


@pytest.fixture
def seeded_job(conn: sqlite3.Connection) -> Job:
    """Insert one DISCOVERED + one REVIEW job so the queue endpoint has data."""
    repo = JobRepo(conn)
    now = utcnow_iso()
    repo.add(Job(
        id="job-discovered-1",
        source="greenhouse", source_job_id="gh-1",
        canonical_hash="hash-1",
        title="Senior Engineer", company="Acme",
        location="Remote", url="https://example.com/1",
        description="JD", compensation="",
        posted_at=now, ghost_score=None,
        state=JobState.DISCOVERED,
        discovered_at=now, updated_at=now,
    ))
    review = Job(
        id="job-review-1",
        source="lever", source_job_id="lv-1",
        canonical_hash="hash-2",
        title="Staff Engineer", company="Beta",
        location="NYC", url="https://example.com/2",
        description="JD2", compensation="",
        posted_at=now, ghost_score=None,
        state=JobState.REVIEW,
        discovered_at=now, updated_at=now,
    )
    repo.add(review)
    return review


# --------------------------------------------------------------- service tests

class TestSchedulerService:
    """SchedulerService lifecycle, isolated from the FastAPI app.

    Each test wraps its async body in ``asyncio.run`` to match the rest of
    the v3 suite (test_scheduler.py et al.) — no pytest-asyncio dependency.
    """

    def test_start_spawns_scheduler_task(self):
        async def _go():
            service, get_sched = _make_stub_service()
            await service.start()
            # Give the event loop one tick to let the task start.
            await asyncio.sleep(0)
            assert service.is_running is True
            sched = get_sched()
            assert sched is not None
            assert sched.run_called is True
            await service.stop()
        asyncio.run(_go())

    def test_stop_cancels_the_task(self):
        async def _go():
            service, get_sched = _make_stub_service()
            await service.start()
            await asyncio.sleep(0)
            await service.stop()
            assert service.is_running is False
            assert get_sched().cancelled is True
        asyncio.run(_go())

    def test_start_is_idempotent(self):
        async def _go():
            service, get_sched = _make_stub_service()
            await service.start()
            first = get_sched()
            await service.start()  # second call should be a no-op
            assert get_sched() is first
            await service.stop()
        asyncio.run(_go())

    def test_pause_predicate_reads_live_flag(self):
        """The closure handed to Scheduler reads ``service._paused`` at call
        time so toggles take effect on the next cycle, not at build time."""
        async def _go():
            service, get_sched = _make_stub_service()
            await service.start()
            sched = get_sched()
            assert sched.pause_predicate() is False
            service.pause()
            assert sched.pause_predicate() is True
            service.resume()
            assert sched.pause_predicate() is False
            await service.stop()
        asyncio.run(_go())

    def test_teardown_runs_after_stop(self):
        teardown_called = {"n": 0}

        async def _teardown():
            teardown_called["n"] += 1

        def _build(_p):
            return _StubScheduler(_p)

        async def _go():
            service = SchedulerService(sync_factory(_build), teardown=_teardown)
            await service.start()
            await service.stop()
        asyncio.run(_go())
        assert teardown_called["n"] == 1

    def test_stop_without_start_is_safe(self):
        async def _go():
            service, _ = _make_stub_service()
            # Should not raise even though there's no task to cancel.
            await service.stop()
            assert service.is_running is False
        asyncio.run(_go())

    def test_snapshot_shape(self):
        """Snapshot keeps its (1/M) running/paused fields and gained the
        (3/M) ``pause_reasons`` dict so the dashboard can render which
        source(s) are pausing."""
        service, _ = _make_stub_service()
        snap = service.snapshot()
        assert snap["running"] is False
        assert snap["paused"] is False
        assert snap["pause_reasons"] == {}
        service.pause()  # zero-arg = ``manual`` source per (3/M) compat
        snap = service.snapshot()
        assert snap["paused"] is True
        # Default manual reason is the canonical "paused from dashboard"
        # string — see ControlState._default_reason.
        assert "manual" in snap["pause_reasons"]


# --------------------------------------------------------------- app + endpoints

class TestReadOnlyApi:
    """Endpoints run against a seeded tmp DB; no scheduler attached."""

    def _make_client(self, web_state: WebState) -> TestClient:
        app = create_app(state=web_state, service=None)
        return TestClient(app)

    def test_health_endpoint(self, web_state: WebState):
        with self._make_client(web_state) as client:
            r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["service"] == "av3-web"
        assert isinstance(body["version"], str) and body["version"]

    def test_status_with_no_service(self, web_state: WebState):
        with self._make_client(web_state) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        # No service -> running=false, paused=false; counts default to zero
        # for every pipeline state.
        assert body["scheduler"] == {"running": False, "paused": False}
        assert "jobs_by_state" in body
        assert all(v == 0 for v in body["jobs_by_state"].values())
        # Last cycle is None until the scheduler has actually run.
        assert body["last_cycle"] is None
        # Pipeline order is the stable column order for the (2/M) dashboard.
        assert "DISCOVERED" in body["pipeline_order"]
        assert body["pipeline_order"][0] == "DISCOVERED"

    def test_status_reports_real_counts(self, web_state: WebState, seeded_job: Job):
        with self._make_client(web_state) as client:
            r = client.get("/api/status")
        body = r.json()
        # seeded_job fixture creates one DISCOVERED + one REVIEW.
        assert body["jobs_by_state"]["DISCOVERED"] == 1
        assert body["jobs_by_state"]["REVIEW"] == 1

    def test_queue_endpoint(self, web_state: WebState, seeded_job: Job):
        with self._make_client(web_state) as client:
            r = client.get("/api/queue")
        assert r.status_code == 200
        body = r.json()
        # The DISCOVERED job is NOT in any queue list — those are REVIEW /
        # QUEUED_APPLY / APPLYING only.
        assert len(body["review"]) == 1
        assert len(body["queued_apply"]) == 0
        assert len(body["applying"]) == 0
        item = body["review"][0]
        assert item["id"] == seeded_job.id
        assert item["title"] == "Staff Engineer"
        assert item["company"] == "Beta"
        assert item["state"] == "REVIEW"
        # Compact shape — full JD never travels through this endpoint.
        assert "description" not in item

    def test_sources_endpoint_empty(self, web_state: WebState):
        with self._make_client(web_state) as client:
            r = client.get("/api/sources")
        assert r.status_code == 200
        # No source has been touched -> empty list (NOT "all healthy" — see
        # views.py).
        assert r.json() == {"sources": []}

    def test_sources_endpoint_reflects_health_registry(self, web_state: WebState):
        mark_auth_required("greenhouse", reason="session expired during apply")
        mark_healthy("lever")  # touched + healthy -> appears in list
        with self._make_client(web_state) as client:
            r = client.get("/api/sources")
        body = r.json()
        sources_by_name = {s["source"]: s for s in body["sources"]}
        assert sources_by_name["greenhouse"]["state"] == "AUTH_REQUIRED"
        assert sources_by_name["greenhouse"]["paused"] is True
        assert sources_by_name["greenhouse"]["reason"].startswith("session expired")
        assert sources_by_name["lever"]["state"] == "HEALTHY"
        assert sources_by_name["lever"]["paused"] is False

    def test_index_page_renders(self, web_state: WebState):
        with self._make_client(web_state) as client:
            r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # Sanity: the splash content actually rendered.
        assert "Auto Applier v3" in r.text

    def test_docs_are_disabled(self, web_state: WebState):
        """The /docs and /openapi.json endpoints are off by design (spec
        local-first concern — don't advertise the API on a 0.0.0.0 bind)."""
        with self._make_client(web_state) as client:
            assert client.get("/docs").status_code == 404
            assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------- lifespan

class TestLifespan:
    """When a service is attached, the lifespan must start + stop it."""

    def test_lifespan_starts_and_stops_service(self, web_state: WebState):
        service, get_sched = _make_stub_service()
        app = create_app(state=web_state, service=service)
        with TestClient(app) as client:
            # Inside the context the service has been started.
            assert service.is_running is True
            sched = get_sched()
            assert sched is not None
            r = client.get("/api/status")
            assert r.status_code == 200
            assert r.json()["scheduler"]["running"] is True
        # Exiting the context shuts the lifespan down.
        assert service.is_running is False
        assert sched.cancelled is True
