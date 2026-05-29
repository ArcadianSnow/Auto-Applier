"""FastAPI routers — read-only JSON API + the placeholder HTML index.

Phase 4 (1/M) keeps the surface intentionally small:

  * ``/api/health``  — liveness probe (no DB hit; cheap)
  * ``/api/status``  — scheduler state + job counts + last cycle summary
  * ``/api/sources`` — per-source health (the (4/M) login-needed badge feed)
  * ``/api/queue``   — review + queued_apply + applying lists
  * ``/``            — splash HTML pointing at the JSON endpoints

The (2/M) dashboard adds SSE + the real per-page handlers; (3/M) adds the
pause toggle endpoint; (4/M) adds login-on-demand; (5/M) adds the onboarding
flow. Each lands in its own router file to keep diffs reviewable.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from av3 import __version__
from av3.db.repositories import JobRepo
from av3.domain.state import JobState
from av3.sources.health import snapshot as health_snapshot
from av3.web.views import (
    PIPELINE_STATES,
    health_record,
    job_brief,
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


# ---------------------------------------------------------------- /  (HTML)

@pages_router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Placeholder splash. The real dashboard lands in Phase 4 (2/M); this
    page exists so a Phase 4 (1/M) install boots without a 404."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": __version__},
    )
