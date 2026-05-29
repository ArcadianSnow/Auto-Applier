"""FastAPI routers — JSON API + dashboard HTML pages.

Surface as of Phase 4 (3/M):

  * ``/api/health``           — liveness probe (no DB hit; cheap)
  * ``/api/status``           — scheduler state + job counts + last cycle
                                summary + active pause reasons
  * ``/api/sources``          — per-source health (the (4/M) login-needed badge feed)
  * ``/api/queue``            — review + queued_apply + applying lists
  * ``/api/history``          — recent applications + outcomes joined with jobs
  * ``/api/jobs/<id>``        — per-job detail (job + score + applications)
  * ``/api/events``           — SSE stream of new events.db rows (live activity)
  * ``/api/control/pause``    — POST: pause via ``manual`` source (3/M)
  * ``/api/control/resume``   — POST: clear ``manual`` pause (3/M)
  * ``/``                     — dashboard (3 panels + recent activity)
  * ``/jobs/<id>``            — per-job detail page

(4/M) adds login-on-demand; (5/M) adds the onboarding flow. Each lands
in its own router section to keep diffs reviewable.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from av3 import __version__
from av3.db.repositories import ApplicationRepo, JobRepo, ScoreRepo
from av3.domain.state import JobState
from av3.sources.health import snapshot as health_snapshot
from av3.web.control import SOURCE_MANUAL
from av3.web.views import (
    PIPELINE_STATES,
    event_payload,
    health_record,
    history_row,
    job_brief,
    job_detail,
    recent_scheduler_event,
)

api_router = APIRouter()
pages_router = APIRouter()


# ---------------------------------------------------------------- helpers

def _get_state(request: Request):
    """Pull the WebState off ``app.state``. Routes use this instead of FastAPI
    dependency injection because the WebState is a process-wide singleton —
    Depends() would just rewrap the same reference."""
    return request.app.state.web_state


def _get_service(request: Request):
    """Returns the SchedulerService or ``None`` (the app supports both modes —
    headless diagnostics OR full live service)."""
    return getattr(request.app.state, "scheduler_service", None)


# ---------------------------------------------------------------- /api/health

@api_router.get("/health")
async def health() -> dict:
    """Cheap liveness probe. Returns the package version so an external
    monitor can verify the running build. Never touches the DB so a corrupt
    app.db can still be diagnosed by hitting this endpoint."""
    return {
        "ok": True,
        "service": "av3-web",
        "version": __version__,
    }


# ---------------------------------------------------------------- /api/status

# Handlers below are ``async def`` deliberately — FastAPI then runs them in
# the event loop (no threadpool dispatch). Each handler opens its own
# short-lived sqlite3 connection via WebState (~1 ms on a WAL'd file), so
# there's no thread-affinity bug and the loop only blocks for the duration
# of a single indexed read. Fine for a localhost dashboard.

@api_router.get("/status")
async def status(request: Request) -> dict:
    """Headline numbers for the dashboard.

    Combines four cheap reads:
      * scheduler running/paused flags (the service snapshot, no DB hit)
      * jobs-by-state counts (one ``GROUP BY`` in app.db)
      * the last 'scheduler' event row (cycle marker; ``None`` until the
        scheduler has completed at least one cycle)
      * pipeline state order (so the UI renders consistently across browsers)
    """
    web_state = _get_state(request)
    service = _get_service(request)

    with web_state.app_conn() as conn:
        counts_raw = JobRepo(conn).count_by_state()
    # Surface every pipeline state even if zero — keeps the dashboard's column
    # set stable across cycles (no jitter on the layout when a state hits 0).
    counts = {st.value: counts_raw.get(st.value, 0) for st in PIPELINE_STATES}

    last_cycle = None
    if web_state.events_db_path.exists():
        with web_state.events_conn() as ev_conn:
            row = ev_conn.execute(
                "SELECT * FROM events WHERE stage = 'scheduler' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_cycle = recent_scheduler_event(row)

    return {
        "scheduler": (
            service.snapshot() if service is not None
            else {"running": False, "paused": False}
        ),
        "jobs_by_state": counts,
        "pipeline_order": [st.value for st in PIPELINE_STATES],
        "last_cycle": last_cycle,
    }


# ---------------------------------------------------------------- /api/sources

@api_router.get("/sources")
async def sources(request: Request) -> dict:
    """Per-source health for the 'login needed' badge (spec §8b).

    Reads :func:`av3.sources.health.snapshot` — the in-memory registry, no DB.
    Empty result is meaningful: it means no source has been touched this
    process lifetime, NOT that all sources are healthy. The dashboard renders
    a hint when the list is empty."""
    snap = health_snapshot()
    return {"sources": [health_record(r) for r in snap.values()]}


# ---------------------------------------------------------------- /api/queue

@api_router.get("/queue")
async def queue(request: Request) -> dict:
    """Three current-work lists for the dashboard's queue panel.

    * ``review``        — jobs awaiting human action (guard-flagged, novel
                          question, FAILED apply); rendered with the reason
                          in (2/M).
    * ``queued_apply``  — passed the optimize+Strict gate; apply worker will
                          pick these up next cycle.
    * ``applying``      — apply in flight (a crash-sweep restarts these per
                          §5; visible to the user so they know something is
                          actively happening).
    """
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        repo = JobRepo(conn)
        return {
            "review": [
                job_brief(j) for j in repo.list_by_state(JobState.REVIEW, limit=50)
            ],
            "queued_apply": [
                job_brief(j)
                for j in repo.list_by_state(JobState.QUEUED_APPLY, limit=50)
            ],
            "applying": [
                job_brief(j) for j in repo.list_by_state(JobState.APPLYING, limit=50)
            ],
        }


# ---------------------------------------------------------------- /api/history

@api_router.get("/history")
async def history(request: Request, limit: int = 50) -> dict:
    """Recent applications + outcomes for the dashboard's history panel.

    Joins each application with its job and JobScore in Python (one query
    per row is cheap on SQLite WAL and avoids a brittle hand-rolled JOIN).
    The job + score may be missing if retention pruned them out (APPLIED
    jobs are kept forever per spec §4, but the apply row itself outlives
    everything — we still surface those rows with ``job: null``)."""
    if limit < 1 or limit > 500:
        # Defensive upper bound — the dashboard never asks for more than 50,
        # but external callers (cli ad-hoc curl) shouldn't be able to DOS
        # the loop by pulling the whole applications table.
        raise HTTPException(status_code=400, detail="limit must be 1..500")
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        app_repo = ApplicationRepo(conn)
        job_repo = JobRepo(conn)
        score_repo = ScoreRepo(conn)
        rows = []
        for app in app_repo.list_recent(limit=limit):
            job = job_repo.get(app.job_id)
            score = score_repo.get(app.job_id) if job is not None else None
            rows.append(history_row(app, job, score))
    return {"applications": rows}


# ---------------------------------------------------------------- /api/jobs/<id>

@api_router.get("/jobs/{job_id}")
async def job_detail_endpoint(request: Request, job_id: str) -> dict:
    """Full per-job detail (job + score + applications). The per-job HTML
    page hits this once on load — no live polling, the data is static
    enough that a refresh is the right interaction."""
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job = JobRepo(conn).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        score = ScoreRepo(conn).get(job_id)
        apps = ApplicationRepo(conn).list_by_job(job_id)
    return job_detail(job, score, apps)


# ---------------------------------------------------------------- /api/events (SSE)

# How fast we poll events.db for new rows. SQLite has no built-in change
# notification, so the dashboard's "live activity" stream is just a tight
# polling loop. 1s gives sub-cycle responsiveness without burning CPU; tests
# inject a faster value.
_DEFAULT_SSE_POLL_INTERVAL_S = 1.0

# Cap on how many rows one poll cycle emits. Keeps a backlog from one
# request bloating into thousands of SSE frames if a worker just dumped a
# spike. Excess flows on the next poll.
_SSE_BATCH_SIZE = 100


async def _events_stream(
    web_state,
    request,
    *,
    poll_interval_s: float,
    initial_last_id: int | None = None,
):
    """Async generator yielding text/event-stream frames for new events.db rows.

    Yields the schema as the first event (``event: hello``) so the UI knows
    the connection is live before any worker activity. Disconnects are
    detected via ``request.is_disconnected()`` so a closed tab doesn't keep
    looping forever.
    """
    # Initial cursor: skip everything that existed before the connection
    # opened, so a fresh page-load doesn't replay old events. Tests override
    # by passing ``initial_last_id=0`` to see everything.
    if initial_last_id is None:
        last_id = 0
        if web_state.events_db_path.exists():
            with web_state.events_conn() as conn:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM events"
                ).fetchone()
                last_id = int(row["m"])
    else:
        last_id = int(initial_last_id)

    # Initial hello so a JS EventSource fires its 'open' handler with content.
    yield f"event: hello\ndata: {json.dumps({'last_id': last_id})}\n\n"

    while True:
        if await request.is_disconnected():
            return
        if web_state.events_db_path.exists():
            try:
                with web_state.events_conn() as conn:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE id > ? "
                        "ORDER BY id ASC LIMIT ?",
                        (last_id, _SSE_BATCH_SIZE),
                    ).fetchall()
            except Exception:
                # Don't kill the stream on a transient DB error — try again
                # next poll. The error itself gets recorded by whatever
                # raised it (this loop isn't a stage in the spine).
                rows = []
            for row in rows:
                last_id = int(row["id"])
                payload = event_payload(row)
                yield f"event: event\ndata: {json.dumps(payload)}\n\n"
        # Periodic keepalive comment. Two jobs:
        #   1. Real SSE proxies (nginx, cloudflare) idle-out long-quiet
        #      connections. A regular comment frame keeps the socket alive.
        #   2. Test clients that read raw chunks need *something* to arrive
        #      every poll so they can re-check their own deadline budget;
        #      otherwise iter_raw blocks indefinitely waiting for the next
        #      real event.
        # The leading colon makes this an SSE comment — EventSource clients
        # silently drop it; only the bytes matter to the network layer.
        yield ": keepalive\n\n"
        await asyncio.sleep(poll_interval_s)


@api_router.get("/events")
async def events_stream(
    request: Request,
    poll_interval_s: float | None = None,
    from_id: int | None = None,
) -> StreamingResponse:
    """Server-Sent Events stream tailing events.db.

    Query params:
      * ``poll_interval_s`` — override the default 1s poll cadence
        (tests pass small values; the dashboard uses the default).
      * ``from_id`` — start from this row id (tests pass 0 to replay
        everything; the dashboard omits and gets only new rows).
    """
    web_state = _get_state(request)
    interval = (
        poll_interval_s
        if poll_interval_s is not None and poll_interval_s > 0
        else _DEFAULT_SSE_POLL_INTERVAL_S
    )
    gen = _events_stream(
        web_state, request,
        poll_interval_s=interval,
        initial_last_id=from_id,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        # Disable proxy buffering so events flow promptly even behind
        # nginx-style reverse proxies (irrelevant for the localhost default
        # but harmless and right when host=0.0.0.0 sits behind one).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- /api/control

# Manual pause / resume — driven from the dashboard's status-bar toggle and
# from any CLI / curl ergonomics. The hotkey + idle watchers flip OTHER
# sources on the same union; the manual source is what these endpoints
# touch so a user pressing "Pause" on the dashboard doesn't fight the
# hotkey state when they later toggle F6.
#
# We deliberately don't expose an endpoint that touches the hotkey/idle
# sources directly — those are watcher-owned, and a UI override that
# pretends F6 is off when the watcher is still flipping it would be
# misleading.

@api_router.post("/control/pause")
async def control_pause(request: Request) -> dict:
    """Pause the scheduler via the ``manual`` source.

    Optional JSON body: ``{"reason": "..."}`` — a short human-readable
    string the dashboard renders in the status bar. Returns the updated
    pause snapshot so the UI can refresh without a follow-up GET.

    No service attached (read-only diagnostics mode) → 409: there's
    nothing to pause.
    """
    service = _get_service(request)
    if service is None:
        raise HTTPException(
            status_code=409,
            detail="no scheduler service is attached (read-only mode)",
        )
    reason = await _read_optional_reason(request)
    snap = service.pause(SOURCE_MANUAL, reason=reason)
    return snap.to_dict()


@api_router.post("/control/resume")
async def control_resume(request: Request) -> dict:
    """Clear the ``manual`` pause source. Other sources (``hotkey``,
    ``idle``) keep their state — the scheduler stays paused if either of
    them is still holding it, with the active reasons visible in the
    response. That's deliberate: a user can't unpause the bot from the
    dashboard while F6 is engaged.

    No service attached → 409 (same as pause)."""
    service = _get_service(request)
    if service is None:
        raise HTTPException(
            status_code=409,
            detail="no scheduler service is attached (read-only mode)",
        )
    snap = service.resume(SOURCE_MANUAL)
    return snap.to_dict()


async def _read_optional_reason(request: Request) -> str:
    """Pull the optional ``reason`` field out of a JSON body without
    forcing the client to send one (a curl ``-X POST`` with no body is
    a valid pause request).

    A malformed body is ignored — the user pressing the dashboard button
    shouldn't get a 400 because their browser sent an empty string.
    """
    try:
        body = await request.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    raw = body.get("reason", "")
    return raw if isinstance(raw, str) else ""


# ---------------------------------------------------------------- /  (HTML)

@pages_router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The dashboard. Three panels (pipeline + queue + history) + a live
    activity feed driven by SSE. Server renders the shell; Alpine.js wires
    up polling + the SSE EventSource."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"version": __version__},
    )


@pages_router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: str) -> HTMLResponse:
    """Per-job page — score breakdown + apply attempts + artifact paths.

    Renders the shell; the page fetches ``/api/jobs/<id>`` once on load
    (no live polling — the data is static enough that an F5 is the right
    re-fetch UX)."""
    web_state = _get_state(request)
    # Pre-flight the job exists so a typo'd URL gets a clean 404 instead
    # of rendering a broken page that then fetches a 404.
    with web_state.app_conn() as conn:
        if JobRepo(conn).get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"version": __version__, "job_id": job_id},
    )
