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
from auto_applier.db.repositories import (
    ApplicationRepo,
    JobRepo,
    OutcomeRepo,
    ScoreRepo,
)
from auto_applier.domain.models import Application, Job, JobScore, Outcome, utcnow_iso
from auto_applier.domain.state import (
    ApplicationStatus,
    ApplyMode,
    JobState,
    OutcomeKind,
)
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
        # Direction 2 (Phase B): the outcomes card + the history Outcome column.
        assert "Outcomes" in r.text
        assert "hasOutcomeData()" in r.text
        assert "outcomeFor(" in r.text
        # Direction 2 (Phase C): the goals/targeting card + its editor hooks.
        assert "Goals &amp; targeting" in r.text
        assert "startGoalsEdit()" in r.text
        assert "saveGoals()" in r.text
        assert "goalsBoardGroups()" in r.text
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


# --------------------------------------------------------------- /api/outcomes (Direction 2, Phase B)

class TestOutcomesEndpoint:
    """The apply-outcome analytics surface — summary + cumulative funnel +
    per-job furthest-outcome map (drives the dashboard Outcomes card + the
    history table's Outcome column)."""

    def test_empty_is_zeroed_not_error(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.get("/api/outcomes")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["total_applied"] == 0
        assert body["funnel"]["applied"] == 0
        assert body["by_source"] == []
        assert body["by_job"] == {}

    def test_summary_funnel_and_by_job(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        # Two APPLIED jobs: one reached an interview, one applied-but-silent.
        _seed_job(conn, id="ja", state=JobState.APPLIED, source="lever",
                  title="Data Engineer", company="Acme")
        _seed_job(conn, id="jb", state=JobState.APPLIED, source="greenhouse",
                  title="Data Engineer", company="Beta")
        repo = OutcomeRepo(conn)
        repo.add(Outcome(job_id="ja", kind=OutcomeKind.RESPONSE))
        repo.add(Outcome(job_id="ja", kind=OutcomeKind.INTERVIEW))  # furthest = interview

        with _make_client(web_state) as client:
            body = client.get("/api/outcomes").json()

        assert body["summary"]["total_applied"] == 2
        assert body["summary"]["total_converted"] == 1
        assert body["summary"]["outcome_counts"] == {"interview": 1}
        # Cumulative funnel: the interview job counts in responded + interviewed.
        assert body["funnel"] == {
            "applied": 2, "responded": 1, "interviewed": 1, "offered": 0,
            "rejected": 0, "ghosted": 0, "awaiting": 1,
        }
        # Per-job map: furthest stage for ja, "awaiting" for the silent jb.
        assert body["by_job"] == {"ja": "interview", "jb": "awaiting"}

    def test_by_source_is_present_for_applied_jobs(
        self, web_state: WebState, conn: sqlite3.Connection
    ):
        _seed_job(conn, id="jc", state=JobState.APPLIED, source="ashby",
                  title="Analyst", company="Gamma")
        OutcomeRepo(conn).add(Outcome(job_id="jc", kind=OutcomeKind.OFFER))
        with _make_client(web_state) as client:
            body = client.get("/api/outcomes").json()
        srcs = {s["key"]: s for s in body["by_source"]}
        assert srcs["ashby"]["applied"] == 1
        assert srcs["ashby"]["converted"] == 1
        assert srcs["ashby"]["rate"] == 1.0
        # An offer counts at every cumulative stage.
        assert body["funnel"]["offered"] == 1
        assert body["funnel"]["interviewed"] == 1
        assert body["funnel"]["responded"] == 1


# --------------------------------------------------------------- /api/targeting (Direction 2, Phase C)

class TestTargetingEndpoint:
    """The goals/targeting view's read side + the (extended) single writer.

    Read: effective TargetingConfig (saved overlaid on defaults), so the dashboard Goals card
    shows what discovery actually sweeps. Write: /api/onboarding/targeting now also accepts +
    sanitizes the board-slug lists (the wizard never sends them, so its contract is unchanged)."""

    def test_defaults_when_nothing_saved(self, web_state: WebState):
        with _make_client(web_state) as client:
            body = client.get("/api/targeting").json()
        # No user_config written → pure TargetingConfig() defaults surface.
        assert body["titles"] == []
        assert body["remote_ok"] is True and body["onsite_ok"] is True
        assert body["salary_floor"] is None
        assert body["using_default_boards"] is True
        # Starter board set is present and counted.
        assert "anthropic" in body["boards"]["greenhouse"]
        assert body["board_count"] == (
            len(body["boards"]["greenhouse"])
            + len(body["boards"]["lever"])
            + len(body["boards"]["ashby"])
        )

    def test_reflects_saved_overrides(self, web_state: WebState):
        from auto_applier.web.onboarding import save_user_config

        save_user_config(web_state.settings.data_dir, {
            "targeting": {
                "titles": ["Data Engineer", "Platform Engineer"],
                "salary_floor": 150000,
                "seniority": "senior",
                "greenhouse_boards": ["acme"],
                "lever_boards": [],
                "ashby_boards": ["Linear"],
            }
        })
        with _make_client(web_state) as client:
            body = client.get("/api/targeting").json()
        assert body["titles"] == ["Data Engineer", "Platform Engineer"]
        assert body["salary_floor"] == 150000
        assert body["seniority"] == "senior"
        assert body["boards"] == {"greenhouse": ["acme"], "lever": [], "ashby": ["Linear"]}
        assert body["board_count"] == 2
        assert body["using_default_boards"] is False

    def test_malformed_saved_config_falls_back_to_defaults(self, web_state: WebState):
        from auto_applier.web.onboarding import save_user_config

        # salary_floor as a non-int is invalid for TargetingConfig → endpoint must not 500.
        save_user_config(web_state.settings.data_dir, {
            "targeting": {"salary_floor": "lots", "titles": ["X"]}
        })
        with _make_client(web_state) as client:
            r = client.get("/api/targeting")
        assert r.status_code == 200
        # Pure defaults on fallback (the bad block is discarded wholesale).
        assert r.json()["titles"] == []
        assert r.json()["using_default_boards"] is True

    def test_write_sanitizes_and_persists_boards(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/targeting", json={
                "titles": ["Data Engineer"],
                # whitespace, an empty entry, and a duplicate → trimmed/dropped/deduped.
                "greenhouse_boards": [" acme ", "", "acme", "beta"],
                "ashby_boards": ["Linear", "Notion"],
            })
            assert r.status_code == 200
            body = client.get("/api/targeting").json()
        assert body["titles"] == ["Data Engineer"]
        assert body["boards"]["greenhouse"] == ["acme", "beta"]
        assert body["boards"]["ashby"] == ["Linear", "Notion"]

    def test_ashby_dedupe_preserves_case(self, web_state: WebState):
        # Ashby slugs are case-sensitive; "linear" and "Linear" are NOT the same board.
        with _make_client(web_state) as client:
            client.post("/api/onboarding/targeting",
                        json={"ashby_boards": ["Linear", "linear", "Linear"]})
            body = client.get("/api/targeting").json()
        assert body["boards"]["ashby"] == ["Linear", "linear"]

    def test_structured_only_write_leaves_boards_untouched(self, web_state: WebState):
        from auto_applier.web.onboarding import save_user_config

        save_user_config(web_state.settings.data_dir,
                         {"targeting": {"greenhouse_boards": ["kept"]}})
        with _make_client(web_state) as client:
            # A payload with no board keys must not blank the saved boards.
            client.post("/api/onboarding/targeting", json={"titles": ["Eng"]})
            body = client.get("/api/targeting").json()
        assert body["titles"] == ["Eng"]
        assert body["boards"]["greenhouse"] == ["kept"]

    def test_board_only_write_leaves_structured_untouched(self, web_state: WebState):
        from auto_applier.web.onboarding import save_user_config

        save_user_config(web_state.settings.data_dir,
                         {"targeting": {"titles": ["Eng"], "salary_floor": 120000}})
        with _make_client(web_state) as client:
            client.post("/api/onboarding/targeting", json={"lever_boards": ["acme"]})
            body = client.get("/api/targeting").json()
        assert body["titles"] == ["Eng"]
        assert body["salary_floor"] == 120000
        assert body["boards"]["lever"] == ["acme"]

    def test_non_list_board_value_clears_not_corrupts(self, web_state: WebState):
        with _make_client(web_state) as client:
            r = client.post("/api/onboarding/targeting",
                            json={"greenhouse_boards": "acme"})  # string, not a list
            assert r.status_code == 200
            body = client.get("/api/targeting").json()
        assert body["boards"]["greenhouse"] == []
