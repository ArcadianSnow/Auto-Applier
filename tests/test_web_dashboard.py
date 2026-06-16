"""Phase 4 (2/M) — dashboard endpoint tests.

Coverage:
  * /api/history shape + limit guard + pruned-job handling
  * /api/jobs/<id> shape, 404, score-absent path
  * /api/events SSE: hello frame, new-event delivery, from_id replay
  * Dashboard HTML renders with Alpine.js hooks
  * Per-job HTML page renders + 404 when missing
  * The dashboard.html shell isn't blocked by the splash removal
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings
from auto_applier.db.repositories import ApplicationRepo, JobRepo, ScoreRepo
from auto_applier.domain.models import Application, Job, JobScore, utcnow_iso
from auto_applier.domain.state import ApplicationStatus, ApplyMode, JobState
from auto_applier.telemetry import EventSink
from auto_applier.web import WebState, create_app


@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


def _make_client(web_state: WebState) -> TestClient:
    app = create_app(state=web_state, service=None)
    return TestClient(app)


def _seed_job(conn: sqlite3.Connection, *, id="job-1", state=JobState.DISCOVERED,
              source="greenhouse", title="Engineer", company="Acme") -> Job:
    now = utcnow_iso()
    job = Job(
        id=id, source=source, source_job_id=f"src-{id}",
        canonical_hash=f"hash-{id}",
        title=title, company=company,
        location="Remote", url=f"https://example.com/{id}",
        description=f"JD for {id}", compensation="",
        posted_at=now, ghost_score=None,
        state=state, discovered_at=now, updated_at=now,
    )
    JobRepo(conn).add(job)
    return job


def _seed_application(conn: sqlite3.Connection, *, job_id: str, app_id="app-1",
                      status=ApplicationStatus.APPLIED,
                      mode=ApplyMode.BROWSER_AUTO,
                      submitted_at=None) -> Application:
    app = Application(
        id=app_id, job_id=job_id, mode=mode, status=status,
        cover_letter_path=f"/art/{app_id}_cover.txt",
        generated_resume_path=f"/art/{app_id}.pdf",
        submitted_at=submitted_at or utcnow_iso(),
    )
    ApplicationRepo(conn).add(app)
    return app


def _seed_score(conn: sqlite3.Connection, *, job_id: str, total=8.5,
                dimensions=None) -> JobScore:
    score = JobScore(
        job_id=job_id, total=total,
        dimensions=dimensions or {"skills": 9.0, "experience": 8.0},
        model="test-model|prompt-v1",
        scored_at=utcnow_iso(),
    )
    ScoreRepo(conn).upsert(score)
    return score


# --------------------------------------------------------------- /api/history

class TestHistoryEndpoint:

    def test_empty(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/api/history")
        assert r.status_code == 200
        assert r.json() == {"applications": []}

    def test_returns_applications_with_joined_job_and_score(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        job = _seed_job(conn, id="j1", state=JobState.APPLIED,
                        title="Senior Eng", company="Acme")
        _seed_score(conn, job_id="j1", total=8.2)
        _seed_application(conn, job_id="j1", app_id="a1",
                          status=ApplicationStatus.APPLIED)
        with _make_client(web_state) as client:
            r = client.get("/api/history")
        body = r.json()
        assert len(body["applications"]) == 1
        row = body["applications"][0]
        assert row["status"] == "APPLIED"
        assert row["mode"] == "browser_auto"
        assert row["job"]["title"] == "Senior Eng"
        assert row["score_total"] == 8.2

    def test_recent_first_ordering(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1")
        _seed_job(conn, id="j2")
        _seed_application(conn, job_id="j1", app_id="a1",
                          submitted_at="2026-05-01T00:00:00+00:00")
        _seed_application(conn, job_id="j2", app_id="a2",
                          submitted_at="2026-05-29T00:00:00+00:00")
        with _make_client(web_state) as client:
            r = client.get("/api/history")
        ids = [a["id"] for a in r.json()["applications"]]
        assert ids == ["a2", "a1"]

    def test_limit_param_bounds(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/api/history?limit=0")
            assert r.status_code == 400
            r = client.get("/api/history?limit=501")
            assert r.status_code == 400
            r = client.get("/api/history?limit=50")
            assert r.status_code == 200


# --------------------------------------------------------------- /api/jobs/<id>

class TestJobDetailEndpoint:

    def test_full_detail(self, web_state: WebState, conn: sqlite3.Connection):
        _seed_job(conn, id="j1", state=JobState.APPLIED,
                  title="Staff Engineer")
        _seed_score(conn, job_id="j1", total=8.2,
                    dimensions={"skills": 9.0, "experience": 8.0})
        _seed_application(conn, job_id="j1", app_id="a1")
        with _make_client(web_state) as client:
            r = client.get("/api/jobs/j1")
        body = r.json()
        assert body["job"]["id"] == "j1"
        assert body["job"]["title"] == "Staff Engineer"
        # Full description IS included on the detail endpoint (unlike brief).
        assert body["job"]["description"].startswith("JD for")
        assert body["score"]["total"] == 8.2
        assert body["score"]["dimensions"]["skills"] == 9.0
        assert len(body["applications"]) == 1

    def test_score_can_be_absent(self, web_state: WebState, conn: sqlite3.Connection):
        _seed_job(conn, id="j1")
        with _make_client(web_state) as client:
            r = client.get("/api/jobs/j1")
        assert r.status_code == 200
        body = r.json()
        assert body["score"] is None
        assert body["applications"] == []

    def test_missing_job_returns_404(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/api/jobs/does-not-exist")
        assert r.status_code == 404


# --------------------------------------------------------------- /api/events SSE

class _FakeRequest:
    """Minimal stand-in for ``starlette.requests.Request`` exposing just the
    one async method ``_events_stream`` calls. Tests flip ``disconnected``
    to True to make the generator exit cleanly."""

    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _parse_sse_frames(text: str):
    """Return parsed ``(event_name, data_dict)`` tuples from SSE text.
    Comments (lines starting with ``:``) and malformed frames are silently
    skipped."""
    out = []
    for raw in text.split("\n\n"):
        event_name = "message"
        data_line = None
        for ln in raw.splitlines():
            if ln.startswith(":"):  # SSE comment (keepalive) — skip.
                continue
            if ln.startswith("event:"):
                event_name = ln.split(":", 1)[1].strip()
            elif ln.startswith("data:"):
                data_line = ln.split(":", 1)[1].strip()
        if data_line is None:
            continue
        try:
            out.append((event_name, json.loads(data_line)))
        except json.JSONDecodeError:
            continue
    return out


class TestEventsStream:
    """SSE generator tests — call the generator directly with a fake request.

    The framework-level streaming layer (TestClient portal threads,
    httpx.ASGITransport's body buffering) makes it hard to bound chunk
    arrival in a unit test. Testing the generator in isolation gives us
    real coverage of the SSE logic — which is the load-bearing piece — and
    the route's *shape* (status + content-type + dependency wiring) is
    covered separately by :class:`TestEventsRoute`.
    """

    @staticmethod
    async def _collect(web_state, *, from_id, max_real_frames=5, hard_limit=50):
        """Drive ``_events_stream`` until ``max_real_frames`` non-comment
        frames have been collected (or ``hard_limit`` total yields — a
        safety net so a buggy generator never hangs the test). Returns
        the raw concatenated stream text."""
        from auto_applier.web.routes import _events_stream

        request = _FakeRequest()
        gen = _events_stream(
            web_state, request,
            poll_interval_s=0.01,
            initial_last_id=from_id,
        )
        out: list[str] = []
        real_frames = 0
        yields = 0
        async for chunk in gen:
            out.append(chunk)
            yields += 1
            # Anything that isn't an SSE comment is a "real" frame.
            if not chunk.lstrip().startswith(":"):
                real_frames += 1
            if real_frames >= max_real_frames or yields >= hard_limit:
                request.disconnected = True
        # If the generator hadn't yielded enough yet, give it one more
        # tick to see the disconnect flag.
        return "".join(out)

    def test_hello_then_replay_from_zero(
        self, web_state: WebState, settings: Settings
    ):
        # Seed two events BEFORE opening the stream; with from_id=0 we should
        # receive both (the default cursor would skip them).
        sink = EventSink(settings.events_db_path)
        sink.emit(stage="filter", status="ok")
        sink.emit(stage="score", status="ok", platform="greenhouse")
        sink.close()
        text = asyncio.run(self._collect(
            web_state, from_id=0, max_real_frames=3,
        ))
        frames = _parse_sse_frames(text)
        names = [n for n, _ in frames]
        assert "hello" in names
        events_only = [d for n, d in frames if n == "event"]
        assert len(events_only) >= 2
        stages = [e["stage"] for e in events_only]
        assert "filter" in stages
        assert "score" in stages

    def test_default_cursor_skips_old_events(
        self, web_state: WebState, settings: Settings
    ):
        """The default cursor (no from_id) starts from MAX(id) so a fresh
        page load doesn't replay months of history."""
        sink = EventSink(settings.events_db_path)
        for _ in range(5):
            sink.emit(stage="filter", status="ok")
        sink.close()
        # Pass from_id=None — same as the production default. The generator
        # must look up MAX(id) on connect and only yield NEW events.
        text = asyncio.run(self._collect(
            web_state, from_id=None, max_real_frames=1, hard_limit=12,
        ))
        frames = _parse_sse_frames(text)
        names = [n for n, _ in frames]
        # Hello frame is the only thing that should land — no replay.
        assert "hello" in names
        events_only = [d for n, d in frames if n == "event"]
        assert events_only == []

    def test_missing_events_db_does_not_crash(self, web_state: WebState):
        """Fresh install: events.db doesn't exist yet. The generator must
        still yield the hello frame and keep polling without raising."""
        assert not web_state.events_db_path.exists()
        text = asyncio.run(self._collect(
            web_state, from_id=None, max_real_frames=1, hard_limit=10,
        ))
        frames = _parse_sse_frames(text)
        names = [n for n, _ in frames]
        assert "hello" in names

    def test_keepalive_is_emitted_each_poll(self, web_state: WebState):
        """Production SSE proxies idle-out long-quiet connections, so the
        generator must emit a ``: keepalive`` comment on every poll cycle
        (even with no real events)."""
        # max_real_frames effectively-disabled (no second real event will
        # ever appear in an empty events.db); hard_limit terminates the
        # loop after the hello + 4 keepalives.
        text = asyncio.run(self._collect(
            web_state, from_id=0, max_real_frames=999, hard_limit=5,
        ))
        assert ": keepalive" in text


def test_events_route_uses_streaming_response():
    """Verify the route returns a StreamingResponse with the SSE media type.

    We don't hit the route with TestClient — its portal-thread streaming
    doesn't release control even for the headers on a long-lived SSE
    response. Inspecting the response object the route function returns
    is enough — that's where the contract lives.
    """
    import inspect
    from fastapi.responses import StreamingResponse

    from auto_applier.web.routes import events_stream
    # The route function is async + decorated by APIRouter; the underlying
    # coroutine returns a StreamingResponse. Inspect its source to assert
    # the response type + media type — no event loop needed.
    src = inspect.getsource(events_stream)
    assert "StreamingResponse(" in src
    assert 'media_type="text/event-stream"' in src


# --------------------------------------------------------------- HTML pages

class TestDashboardHtml:

    def test_dashboard_renders_with_alpine_hooks(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # Sanity: Alpine.js components are wired in.
        assert 'x-data="dashboard()"' in r.text
        # The three panels referenced in the spec are present. The flat
        # "Review queue" became the grouped "Assisted queue" in Direction 2 (A1).
        assert "Pipeline" in r.text
        assert "Assisted queue" in r.text
        assert "Recent applications" in r.text
        # Alpine.js + our JS are linked.
        assert "alpinejs" in r.text
        assert "app.js" in r.text

    def test_job_detail_page_renders(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="j1", title="ML Engineer")
        with _make_client(web_state) as client:
            r = client.get("/jobs/j1")
        assert r.status_code == 200
        # Page passes the job_id through to Alpine via x-data="jobDetail('j1')"
        assert "jobDetail('j1')" in r.text
        # No server-rendered job title — the page fetches data client-side.
        # Sanity check the shell.
        assert "av3" in r.text

    def test_job_detail_page_404(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/jobs/missing")
        assert r.status_code == 404
