"""FastAPI routers — JSON API + dashboard HTML pages.

Surface as of Phase 4 (4/M):

  * ``/api/health``                       — liveness probe (no DB hit; cheap)
  * ``/api/status``                       — scheduler state + job counts + last
                                            cycle summary + active pause reasons
  * ``/api/sources``                      — per-source health (the login-needed
                                            badge feed; now carries ``login_url``)
  * ``/api/sources/<source>/login``       — POST: open the source's login URL
                                            in the bot's headed browser (4/M)
  * ``/api/sources/<source>/healthy``     — POST: clear AUTH_REQUIRED (4/M)
  * ``/api/queue``                        — review + queued_apply + applying lists
  * ``/api/history``                      — recent applications + outcomes
                                            joined with jobs
  * ``/api/jobs/<id>``                    — per-job detail (job + score + apps)
  * ``/api/jobs/<id>/assisted/open``      — POST: open the apply URL for
                                            an ASSISTED_PENDING attempt (4/M)
  * ``/api/jobs/<id>/assisted/confirm``   — POST: human-confirmed submit
                                            walks REVIEW→APPLIED (4/M)
  * ``/api/jobs/<id>/assisted/cancel``    — POST: human-cancelled submit
                                            marks attempt FAILED (4/M)
  * ``/api/events``                       — SSE stream of new events.db rows
  * ``/api/control/pause``                — POST: pause via ``manual`` (3/M)
  * ``/api/control/resume``               — POST: clear ``manual`` pause (3/M)
  * ``/``                                 — dashboard (3 panels + activity)
  * ``/jobs/<id>``                        — per-job detail page

(5/M) adds the onboarding flow. Each lands in its own router section to keep
diffs reviewable.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from auto_applier import __version__
from auto_applier.db.repositories import ApplicationRepo, JobRepo, OutcomeRepo, ScoreRepo
from auto_applier.domain.models import utcnow_iso
from auto_applier.domain.state import ApplicationStatus, JobState
from auto_applier.pipeline.apply_worker import PrepareSingleError
from auto_applier.resume.proposed import load_proposed
from auto_applier.sources.health import (
    is_paused as source_is_paused,
    mark_healthy,
    snapshot as health_snapshot,
)
from auto_applier.web.control import SOURCE_MANUAL
from auto_applier.web.onboarding import (
    load_fact_bank,
    load_user_config,
    merge_contact,
    merge_education,
    merge_extras,
    merge_skills,
    merge_work_auth,
    merge_work_history,
    onboarding_status,
    save_fact_bank,
    save_user_config,
)
from auto_applier.web.views import (
    PIPELINE_STATES,
    event_payload,
    health_record,
    history_row,
    job_brief,
    job_detail,
    recent_scheduler_event,
    recent_stage_event,
    review_reason,
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


def _get_launcher(request: Request):
    """Returns the :class:`HeadedBrowserLauncher`. The app factory always sets
    one — a no-bot-browser fallback in tests / ``--no-scheduler``, a
    BrowserSession-bound launcher in production — so this never returns
    ``None``."""
    return request.app.state.headed_launcher


def _get_worker(request: Request):
    """The scheduler's :class:`ApplyWorker`, exposed for the E2 on-demand fill (``prepare_single``).

    ``None`` in ``--no-scheduler`` / pre-onboarding diagnostics, or before the scheduler factory has
    built it — the prepare route 409s with a "start the worker" message in that case. Reusing the
    scheduler's own worker (not a fresh one) is deliberate: the on-demand fill then matches the
    production fill path exactly (the e2-on-demand-fill-design "no second code path" requirement)."""
    holder = getattr(request.app.state, "apply_worker_holder", None)
    return holder.get("worker") if holder else None


def _dispose_batch(request: Request, job_id: str, disposition: str) -> None:
    """Record an owner disposition on the review batch when this job is a current batch member
    (batched assisted review, Phase 4). No-op when batching is off or the job was never batched, so
    the generic disposition endpoints stay safe for ordinary review-queue jobs."""
    batch = getattr(_get_state(request), "review_batch", None)
    if batch is not None:
        batch.dispose(job_id, disposition)


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
    last_stage = None
    if web_state.events_db_path.exists():
        with web_state.events_conn() as ev_conn:
            row = ev_conn.execute(
                "SELECT * FROM events WHERE stage = 'scheduler' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_cycle = recent_scheduler_event(row)
            # The most recent PER-STAGE event so the dashboard can show what the pipeline is
            # actually doing right now ("scoring…", "optimizing…") instead of a bare "Running"
            # — the #1 "is it even working?" confusion on launch.
            stage_row = ev_conn.execute(
                "SELECT * FROM events WHERE stage IN "
                "('discover','filter','score','optimize','apply','inbox') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_stage = recent_stage_event(stage_row)

    # Batched assisted review (Phase 3) — a small snapshot so the dashboard can show an
    # "N awaiting review" badge linking to /in-progress. ``None`` when the feature is off.
    batch = getattr(web_state, "review_batch", None)

    return {
        "scheduler": (
            service.snapshot() if service is not None
            else {"running": False, "paused": False}
        ),
        "jobs_by_state": counts,
        "pipeline_order": [st.value for st in PIPELINE_STATES],
        "last_cycle": last_cycle,
        "last_stage": last_stage,
        "review_batch": batch.snapshot() if batch is not None else None,
    }


# ---------------------------------------------------------------- /api/sources

@api_router.get("/sources")
async def sources(request: Request) -> dict:
    """Per-source health for the 'login needed' badge (spec §8b).

    Reads :func:`auto_applier.sources.health.snapshot` — the in-memory registry, no DB.
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


# ---------------------------------------------------------------- /api/review-queue (Direction 2, A1)

# The actionable assisted-queue feed (Direction 2, Phase A). Where /api/queue
# returns a flat REVIEW list, this enriches each REVIEW job with an INFERRED
# needed-action (submit | login | decide) + a human reason + the bits the
# dashboard needs to act on it (the ASSISTED_PENDING application id, source
# pause state, artifact paths, score). The reason is not stored — it's derived
# every poll from (job, latest Application status, source health) via the pure
# views.review_reason() helper.


@api_router.get("/review-queue")
async def review_queue(request: Request) -> dict:
    """REVIEW jobs grouped into an actionable to-do list (Direction 2, A1).

    For each REVIEW job we compute:
      * ``needed_action`` ∈ {submit, login, decide} + a human ``reason``
        (``views.review_reason`` — pure, unit-tested).
      * ``assisted_application_id`` — the latest ASSISTED_PENDING attempt's id
        (so the dashboard's Open/Confirm/Cancel buttons target the right row),
        or ``None``.
      * ``source_paused`` — whether the job's source is in AUTH_REQUIRED.
      * ``artifacts`` — resume / cover-letter paths off the latest attempt (so
        the user can sanity-check what the bot prepared), or ``None``.
      * ``score_total`` — the JD score, or ``None`` if never scored.

    Read-only; one short-lived connection. Capped at 50 like /api/queue.
    """
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job_repo = JobRepo(conn)
        app_repo = ApplicationRepo(conn)
        score_repo = ScoreRepo(conn)
        jobs_out = []
        for job in job_repo.list_by_state(JobState.REVIEW, limit=50):
            apps = app_repo.list_by_job(job.id)
            # The most-recent attempt drives the reason + the artifact preview.
            # list_by_job orders by submitted_at ASC (empties first), so the
            # last element is the freshest attempt with a real timestamp; an
            # ASSISTED_PENDING attempt (no timestamp) sorts to the FRONT, so we
            # surface it specifically via _latest_assisted_pending.
            pending = _latest_assisted_pending(apps)
            latest_app = pending if pending is not None else (apps[-1] if apps else None)
            source_paused = source_is_paused(job.source)
            needed_action, reason = review_reason(job, latest_app, source_paused)
            score = score_repo.get(job.id)
            artifacts = None
            if latest_app is not None and (
                latest_app.generated_resume_path or latest_app.cover_letter_path
            ):
                artifacts = {
                    "resume": latest_app.generated_resume_path or None,
                    "cover_letter": latest_app.cover_letter_path or None,
                }
            jobs_out.append({
                **job_brief(job),
                "score_total": score.total if score is not None else None,
                "needed_action": needed_action,
                "reason": reason,
                "assisted_application_id": pending.id if pending is not None else None,
                "source_paused": source_paused,
                "artifacts": artifacts,
            })
    return {"jobs": jobs_out}


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


# ---------------------------------------------------------------- /api/outcomes (Direction 2, Phase B)

# The post-apply outcomes surface (Direction 2, Phase B — = Direction 4 Phase D). Reads the
# SAME feed `av3 analytics` does (OutcomeRepo.applied_with_outcomes → analytics) and exposes:
#   * a conversion summary (applied / positive / rate + per-kind tally),
#   * a cumulative apply→response→interview→offer funnel,
#   * top sources by conversion,
#   * a per-job furthest-outcome map so the history table can annotate each APPLIED row.
# Read-only; honesty intact — the feed is APPLIED-only, "awaiting" is NOT a ghost, and an
# outcome never implies APPLIED (APPLIED comes only from a positive submit confirmation).


@api_router.get("/outcomes")
async def outcomes(request: Request) -> dict:
    """Apply-outcome analytics for the dashboard's Outcomes card + history column.

    Aggregates :meth:`OutcomeRepo.applied_with_outcomes` (APPLIED jobs × their recorded
    outcomes) via the pure :mod:`auto_applier.analytics` helpers — the same read-model the
    ``av3 analytics`` CLI renders. Returns:

      * ``summary``  — total_applied / total_converted / overall_rate / per-kind counts.
      * ``funnel``   — cumulative applied→responded→interviewed→offered + rejected/ghosted/
                       awaiting (each job counted once by its furthest stage).
      * ``by_source``— top sources by conversion rate (capped, like the CLI table).
      * ``by_job``   — ``{job_id: kind_value | "awaiting"}`` for every APPLIED job, so the
                       history table renders a per-row outcome pill ("awaiting" = applied but
                       no outcome recorded yet — honestly distinct from a recorded ghost).
    """
    from auto_applier.analytics import (
        compute_conversion_report,
        compute_funnel,
        furthest_outcomes,
    )

    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        feed = OutcomeRepo(conn).applied_with_outcomes()

    report = compute_conversion_report(feed)
    funnel = compute_funnel(feed)
    by_job = {jid: (kind or "awaiting") for jid, kind in furthest_outcomes(feed).items()}
    return {
        "summary": {
            "total_applied": report.total_applied,
            "total_converted": report.total_converted,
            "overall_rate": round(report.overall_rate, 4),
            "outcome_counts": report.outcome_counts,
        },
        "funnel": vars(funnel),
        "by_source": [
            {
                "key": s.key,
                "applied": s.applied,
                "converted": s.converted,
                "ghosted": s.ghosted,
                "rate": round(s.rate, 4),
            }
            for s in report.by_source[:8]
        ],
        "by_job": by_job,
    }


# ---------------------------------------------------------------- /api/targeting (Direction 2, Phase C)

# The goals/targeting view's READ side (Direction 2 Phase C — the dashboard surface for what the
# onboarding journey produced: what the user told the bot they want + the company boards it found).
# Reads the SAVED user_config (the FILE, via load_user_config) overlaid on TargetingConfig defaults
# — NOT web_state.settings, which is frozen at scheduler-build (startup) so it wouldn't reflect an
# edit made this session. The card shows the effective config that the discovery producer WILL sweep
# on the next worker (re)start; the editor writes via the existing /api/onboarding/targeting writer.


@api_router.get("/targeting")
async def targeting(request: Request) -> dict:
    """Effective job-targeting config for the dashboard Goals card.

    Overlays the saved ``user_config.json`` ``targeting`` block onto
    :class:`TargetingConfig` defaults so the card shows what discovery actually sweeps
    (incl. the starter board set when the user hasn't customized it). Returns the structured
    filters + the ATS board slugs grouped by source + a total count + ``using_default_boards``
    (so the UI can label the starter set honestly). Never 500s: a malformed saved block falls
    back to pure defaults.
    """
    from auto_applier.config.settings import TargetingConfig

    web_state = _get_state(request)
    saved = (load_user_config(web_state.settings.data_dir).get("targeting") or {})
    try:
        cfg = TargetingConfig.model_validate(saved)
    except Exception:  # noqa: BLE001 — never let a hand-edited config blank the whole card
        cfg = TargetingConfig()

    boards = {
        "greenhouse": list(cfg.greenhouse_boards),
        "lever": list(cfg.lever_boards),
        "ashby": list(cfg.ashby_boards),
    }
    defaults = TargetingConfig()
    using_default_boards = (
        cfg.greenhouse_boards == defaults.greenhouse_boards
        and cfg.lever_boards == defaults.lever_boards
        and cfg.ashby_boards == defaults.ashby_boards
    )
    return {
        "titles": list(cfg.titles),
        "locations": list(cfg.locations),
        "remote_ok": cfg.remote_ok,
        "onsite_ok": cfg.onsite_ok,
        "salary_floor": cfg.salary_floor,
        "seniority": cfg.seniority,
        "preferences": list(cfg.preferences),
        "boards": boards,
        "board_count": sum(len(v) for v in boards.values()),
        "using_default_boards": using_default_boards,
    }


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


# ---------------------------------------------------------------- /api/sources/.../login (4/M)

# Phase 4 (4/M) — login-on-demand + assisted submit (spec §6a, §8b).
#
# Two surfaces, one launcher:
#   * /api/sources/{source}/login  — opens the captured login_url in the bot's
#     persistent Chrome profile so the user's session cookies land in the
#     same jar the apply worker uses. Does NOT auto-clear the AUTH_REQUIRED
#     flag — the user clicks "Mark logged in" after their sign-in completes.
#   * /api/sources/{source}/healthy — clears AUTH_REQUIRED for a source.
#     Separate from /login because the user might log in via a side channel
#     (their own browser, a password manager popup) and just want to
#     un-pause the source without re-launching the URL.
#
#   * /api/jobs/{id}/assisted/open — opens the apply URL in the bot's
#     profile so the user can review the pre-fill and click submit.
#   * /api/jobs/{id}/assisted/confirm — marks the latest ASSISTED_PENDING
#     attempt as APPLIED + walks the Job state machine REVIEW → QUEUED_APPLY
#     → APPLYING → APPLIED. The chained transition keeps the state-machine
#     audit trail intact (spec §5 "APPLIED requires positive confirmation").
#   * /api/jobs/{id}/assisted/cancel — marks the latest ASSISTED_PENDING
#     attempt as FAILED. The Job stays in REVIEW; the user can re-try or
#     skip from there.


@api_router.post("/sources/{source}/login")
async def source_login(request: Request, source: str) -> dict:
    """Open the captured login URL for ``source`` in the bot's headed
    browser. Does NOT clear the AUTH_REQUIRED flag — the user clicks
    'Mark logged in' once their sign-in is done so we can't race a 'cookies
    landed' assumption with the real auth completion.

    Errors:
      * 404 — source not in the health registry (UI hides the button in
        this state but a stale request might still arrive).
      * 409 — source isn't in AUTH_REQUIRED state (HEALTHY → there's
        nothing to log into; refusing the launch keeps the UI honest).
      * 422 — source is paused but no ``login_url`` was captured (e.g. the
        wall was flagged by a non-URL signal); the UI shows the manual
        'Mark logged in' button without an auto-launch in this state.
    """
    snap = health_snapshot()
    record = snap.get(source)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"source {source!r} not in the health registry",
        )
    if not source_is_paused(source):
        raise HTTPException(
            status_code=409,
            detail=f"source {source!r} is not in AUTH_REQUIRED state",
        )
    if not record.login_url:
        raise HTTPException(
            status_code=422,
            detail=(
                f"no login URL captured for {source!r}; sign in via your own "
                "browser then POST /api/sources/" + source + "/healthy"
            ),
        )
    launcher = _get_launcher(request)
    result = await launcher.open(record.login_url)
    return {
        "source": source,
        "launch": result.to_dict(),
    }


@api_router.post("/sources/{source}/healthy")
async def source_mark_healthy(request: Request, source: str) -> dict:
    """Mark ``source`` HEALTHY — clears the AUTH_REQUIRED flag so the apply
    worker resumes processing jobs from this source on its next cycle.

    Idempotent: re-marking an already-healthy source is a no-op (no event
    fired) so the dashboard's button stays harmless under double-click."""
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    mark_healthy(source)
    return {
        "source": source,
        "state": "HEALTHY",
        "ok": True,
    }


# ---------------------------------------------------------------- /api/jobs/.../assisted (4/M)


@api_router.post("/jobs/{job_id}/assisted/open")
async def assisted_open(request: Request, job_id: str) -> dict:
    """Open the apply URL for an ASSISTED_PENDING job in the bot's headed
    browser so the user can review the pre-fill and click submit.

    Errors:
      * 404 — job doesn't exist.
      * 409 — the job has no ASSISTED_PENDING application waiting (e.g.
        already submitted, already cancelled, or never reached the apply
        step). The UI hides the button in those states.
      * 422 — the job has an ASSISTED_PENDING application but no URL on
        the Job row to open (defensive — the apply worker fills this in
        before flipping to ASSISTED_PENDING, so this is a "shouldn't
        happen" path that still avoids a None.goto()).
    """
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job = JobRepo(conn).get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id} not found"
            )
        apps = ApplicationRepo(conn).list_by_job(job_id)
    pending = _latest_assisted_pending(apps)
    if pending is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} has no ASSISTED_PENDING application waiting"
            ),
        )
    if not job.url:
        raise HTTPException(
            status_code=422,
            detail=f"job {job_id} has no URL to open",
        )
    launcher = _get_launcher(request)
    result = await launcher.open(job.url)
    return {
        "job_id": job_id,
        "application_id": pending.id,
        "launch": result.to_dict(),
    }


@api_router.post("/jobs/{job_id}/open")
async def open_job_in_browser(request: Request, job_id: str) -> dict:
    """Open a job's listing in the bot's headed Chrome (logged-in profile) so the user can take
    it over manually — for assisted-queue jobs the bot couldn't complete ("Needs your decision")
    or that need a sign-in. Unlike ``/assisted/open`` this needs NO pre-filled attempt; it just
    navigates to the listing. Errors: 404 (no job) / 422 (no URL on the row)."""
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job = JobRepo(conn).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if not job.url:
        raise HTTPException(status_code=422, detail=f"job {job_id} has no URL to open")
    result = await _get_launcher(request).open(job.url)
    return {"job_id": job_id, "launch": result.to_dict()}


@api_router.post("/jobs/{job_id}/assisted/prepare")
async def assisted_prepare(request: Request, job_id: str) -> dict:
    """E2 "fill what it can on demand": open a REVIEW job's apply form in the bot's headed Chrome
    and run the production assisted fill on it, leaving a real ``ASSISTED_PENDING`` attempt for the
    owner to review + submit. This **promotes** a "Needs your decision" job into the "Ready to
    finish" lane (it produces the pending attempt the existing assisted endpoints key off).

    The fill is inherently **PARTIAL** — these jobs are in REVIEW *because the bot couldn't complete
    them* — so it fills what it's confident about and leaves essays / sensitive EEO / unresolved
    screeners for the human. It NEVER auto-submits (assisted-only); the human always clicks submit.

    Errors: 404 (no job) · 422 (no URL) · 409 (worker unavailable / no bot browser / non-preparable
    state / unknown source / a fill already in progress).
    """
    worker = _get_worker(request)
    if worker is None:
        raise HTTPException(status_code=409, detail=(
            "the apply worker isn't running — start the scheduler (finish onboarding, "
            "don't use --no-scheduler) to fill applications on demand"
        ))
    launcher = _get_launcher(request)
    if not launcher.has_bot_browser:
        raise HTTPException(status_code=409, detail=(
            "the bot's Chrome isn't available — an on-demand fill needs the bot browser "
            "(it can't drive your default browser)"
        ))

    # Pre-read for a clean 404 / 422 BEFORE we pop a Chrome tab (don't open the browser for a bad
    # id or a URL-less row). prepare_single re-validates state/source authoritatively.
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job = JobRepo(conn).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if not job.url:
        raise HTTPException(status_code=422, detail=f"job {job_id} has no URL to open")

    # Reject a second concurrent fill BEFORE opening a tab (prepare_single also guards, but this
    # avoids popping a Chrome tab we'd immediately 409 on for the common double-click).
    if worker.prepare_in_progress:
        raise HTTPException(status_code=409, detail="a fill is already in progress")

    # Open the apply form in the bot's profile + engage the manual takeover (masks the scheduler's
    # apply stage so it doesn't drive the shared session underneath us), then DRIVE the fill on it.
    page, launch = await launcher.open_page(job.url)
    if page is None:
        raise HTTPException(status_code=409, detail=launch.note or "could not open a bot-browser page")
    try:
        outcome = await worker.prepare_single(job_id, page=page)
    except PrepareSingleError as exc:
        raise HTTPException(status_code=exc.code, detail=str(exc))

    filled = sum(1 for v in outcome.filled.values() if v)
    left = sum(1 for v in outcome.filled.values() if not v)
    return {
        "job_id": job_id,
        "launch": launch.to_dict(),
        "outcome": {
            "status": outcome.status.value if outcome.status else None,
            "filled": filled,
            "left": left,
        },
    }


@api_router.post("/jobs/{job_id}/assisted/confirm")
async def assisted_confirm(request: Request, job_id: str) -> dict:
    """Mark the latest ASSISTED_PENDING attempt as APPLIED — used after the
    user has reviewed the pre-fill and clicked submit themselves.

    Walks the Job state machine REVIEW → QUEUED_APPLY → APPLYING → APPLIED
    in a single DB transaction so the audit trail looks like a normal apply
    flow (spec §5 requires APPLIED to come after APPLYING). The Application
    row gets its ``status`` flipped to ``APPLIED`` and ``submitted_at``
    stamped with the confirmation moment.

    Errors:
      * 404 — job doesn't exist.
      * 409 — no ASSISTED_PENDING application waiting OR the job isn't in
        REVIEW (defensive — the apply worker only puts ASSISTED_PENDING
        attempts into REVIEW, so this catches a stale UI click).
    """
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        job_repo = JobRepo(conn)
        app_repo = ApplicationRepo(conn)
        job = job_repo.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id} not found"
            )
        if job.state is not JobState.REVIEW:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id} is in state {job.state.value}; assisted "
                    "confirm only valid from REVIEW"
                ),
            )
        apps = app_repo.list_by_job(job_id)
        pending = _latest_assisted_pending(apps)
        if pending is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id} has no ASSISTED_PENDING application waiting"
                ),
            )
        # Walk the state machine the same way an auto-apply would: REVIEW →
        # QUEUED_APPLY → APPLYING → APPLIED. Each edge is validated by the
        # transitions table, so an unexpected state mid-walk raises
        # InvalidTransition (caught by FastAPI as a 500 — visible in the
        # event spine via @stage).
        submitted_at = utcnow_iso()
        job_repo.set_state(job_id, JobState.QUEUED_APPLY)
        job_repo.set_state(job_id, JobState.APPLYING)
        job_repo.set_state(job_id, JobState.APPLIED)
        app_repo.set_status(
            pending.id, ApplicationStatus.APPLIED, submitted_at=submitted_at
        )
    return {
        "job_id": job_id,
        "application_id": pending.id,
        "job_state": JobState.APPLIED.value,
        "application_status": ApplicationStatus.APPLIED.value,
        "submitted_at": submitted_at,
    }


@api_router.post("/jobs/{job_id}/assisted/cancel")
async def assisted_cancel(request: Request, job_id: str) -> dict:
    """Mark the latest ASSISTED_PENDING attempt as FAILED — used when the
    user reviewed the pre-fill and decided not to submit (wrong job, bad
    pre-fill, form changed, etc.).

    The Job stays in REVIEW so the user can re-try or move it to SKIPPED
    via a separate action. We DON'T transition the job — the Application
    row carries the cancelled state and the dashboard renders that
    distinctly from a fresh REVIEW.

    Errors:
      * 404 — job doesn't exist.
      * 409 — no ASSISTED_PENDING application waiting.
    """
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        if JobRepo(conn).get(job_id) is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id} not found"
            )
        app_repo = ApplicationRepo(conn)
        apps = app_repo.list_by_job(job_id)
        pending = _latest_assisted_pending(apps)
        if pending is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id} has no ASSISTED_PENDING application waiting"
                ),
            )
        app_repo.set_status(pending.id, ApplicationStatus.FAILED)
    return {
        "job_id": job_id,
        "application_id": pending.id,
        "application_status": ApplicationStatus.FAILED.value,
    }


# ---------------------------------------------------------------- /api/jobs/.../mark-applied + /skip (Direction 2, A2)

# The "Needs your decision" actions for the assisted queue (Direction 2, A2).
#   * mark-applied — the user applied to this job themselves (or finished an
#     assisted form outside the Confirm path) → record a human-attested
#     APPLIED. Reuses pipeline.manual_apply.mark_manually_applied so the
#     state-machine walk + Application row match `av3 applied` exactly (a
#     human attestation is a positive confirmation per spec §5).
#   * skip — the user decided not to pursue it → REVIEW → SKIPPED, mirroring
#     the inline `av3 pass` logic (cli/main.py).


@api_router.post("/jobs/{job_id}/mark-applied")
async def job_mark_applied(request: Request, job_id: str) -> dict:
    """Record a human-attested manual apply for ``job_id`` → APPLIED.

    Thin web wrapper over :func:`auto_applier.pipeline.manual_apply.mark_manually_applied`
    (which opens its own transaction + writes the MANUAL/APPLIED Application
    row, then walks the Job to APPLIED). Allowed only from {DECIDED, REVIEW};
    any other source state comes back as the function's error result.

    Errors:
      * 404 — job doesn't exist.
      * 409 — the job isn't in a state a manual apply can be attested from
        (e.g. already APPLIED, or still SCORED). The detail carries the
        underlying reason verbatim.
    """
    from auto_applier.pipeline.manual_apply import mark_manually_applied

    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        if JobRepo(conn).get(job_id) is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id} not found"
            )
        result = mark_manually_applied(conn, job_id)
    if result.status == "error":
        raise HTTPException(status_code=409, detail=result.detail)
    # Record the disposition on the review batch (no-op for non-batch jobs) so a batch advances
    # once all its members are dealt with (batched assisted review, Phase 4).
    _dispose_batch(request, job_id, "applied")
    return {
        "job_id": job_id,
        "job_state": JobState.APPLIED.value,
        "status": result.status,
        "detail": result.detail,
    }


@api_router.post("/jobs/{job_id}/skip")
async def job_skip(request: Request, job_id: str) -> dict:
    """Move a REVIEW job to SKIPPED — the user decided not to pursue it.

    Mirrors the inline ``av3 pass`` logic (cli/main.py): a validated
    ``set_state`` inside an explicit transaction, catching
    :class:`InvalidTransition` so an illegal source state (e.g. a terminal
    APPLIED) returns a clean 409 instead of a 500.

    Errors:
      * 404 — job doesn't exist.
      * 409 — REVIEW → SKIPPED isn't allowed from the job's current state.
    """
    from auto_applier.db.engine import tx
    from auto_applier.domain.state import InvalidTransition

    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        repo = JobRepo(conn)
        if repo.get(job_id) is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id} not found"
            )
        try:
            with tx(conn):
                repo.set_state(job_id, JobState.SKIPPED)
        except (InvalidTransition, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    _dispose_batch(request, job_id, "skipped")  # batch tracking (Phase 4); no-op for non-batch jobs
    return {"job_id": job_id, "job_state": JobState.SKIPPED.value}


@api_router.post("/jobs/{job_id}/needs-work")
async def job_needs_work(request: Request, job_id: str) -> dict:
    """Mark a batch job as "needs more work" (batched assisted review, Phase 4).

    A SIDE-LANE disposition: it unblocks the batch from advancing but does NOT change the job's
    state — the job stays in REVIEW so it remains in the normal review queue for the owner to
    revisit (edit the fact bank, re-prepare, or handle manually). It does not requeue or
    re-prepare; that's a future enhancement.

    Errors: 404 (no job) / 409 (batched review is off — there is no batch to mark)."""
    web_state = _get_state(request)
    with web_state.app_conn() as conn:
        if JobRepo(conn).get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    batch = getattr(web_state, "review_batch", None)
    if batch is None:
        raise HTTPException(status_code=409, detail="batched review is not enabled")
    batch.dispose(job_id, "needs_work")
    return {"job_id": job_id, "disposition": "needs_work", "batch": batch.snapshot()}


# ---------------------------------------------------------------- /api/batch (batched assisted review, Phase 3)

# The "In Progress" page feed. The apply worker prepares a batch of N jobs then HOLDS (Phase 2);
# this surfaces that batch so the owner can verify / correct / submit each, then release. Each job
# carries its COMPLETE proposed application (Phase 1's per-job artifact) — the confident fills PLUS
# an aggressive draft for every open-ended gap — so the page is the full proposed application, not
# just what the bot typed into the live form.


def _proposed_payload(proposed) -> dict | None:
    """Serialize a :class:`ProposedApplication` for the page, or ``None`` when the job has no
    persisted proposed set yet (e.g. it errored before the artifact was written)."""
    if proposed is None:
        return None
    return {**proposed.to_dict(), "summary": proposed.summary()}


@api_router.get("/batch")
async def review_batch_endpoint(request: Request) -> dict:
    """The batched-assisted-review "In Progress" feed (Phase 3).

    Returns the current batch snapshot plus, for each prepared job in it, the COMPLETE proposed
    application + a job brief + the latest ASSISTED_PENDING attempt id (so the page can re-open the
    bot's pre-fill). ``enabled=false`` when batched review is off (``settings.scheduler.
    batched_review``) — the page then renders an "off" explainer instead of an empty batch.

    Read-only; one short-lived connection. Members whose job row is gone are skipped defensively.
    """
    web_state = _get_state(request)
    batch = getattr(web_state, "review_batch", None)
    if batch is None:
        return {"enabled": False, "batch": None, "jobs": []}
    snap = batch.snapshot()
    jobs_out = []
    with web_state.app_conn() as conn:
        job_repo = JobRepo(conn)
        app_repo = ApplicationRepo(conn)
        for job_id in snap["members"]:
            job = job_repo.get(job_id)
            if job is None:
                continue
            pending = _latest_assisted_pending(app_repo.list_by_job(job_id))
            jobs_out.append({
                **job_brief(job),
                "assisted_application_id": pending.id if pending is not None else None,
                "disposition": snap["dispositions"].get(job_id, "pending"),
                "proposed": _proposed_payload(load_proposed(web_state.settings, job_id)),
            })
    return {"enabled": True, "batch": snap, "jobs": jobs_out}


@api_router.post("/batch/release")
async def review_batch_release(request: Request) -> dict:
    """Release the current review batch → the apply worker prepares the next N on its next cycle
    (Phase 3's "release batch" button).

    Clears the barrier's members and opens a fresh batch (new id), which lifts the hold so the
    scheduler's apply_gate stops masking the apply stage. Returns the fresh (empty) snapshot.

    Errors: 409 when batched review is off (there is nothing to release)."""
    web_state = _get_state(request)
    batch = getattr(web_state, "review_batch", None)
    if batch is None:
        raise HTTPException(status_code=409, detail="batched review is not enabled")
    batch.release()
    return {"released": True, "batch": batch.snapshot()}


def _latest_assisted_pending(apps):
    """Return the most-recent ASSISTED_PENDING application from a job's
    application history, or ``None`` if there isn't one.

    ``ApplicationRepo.list_by_job`` orders by ``submitted_at`` ASC, which
    sorts empty timestamps before real ones — an ASSISTED_PENDING attempt
    (no submitted_at yet) sits at the FRONT of the list, but a later
    confirmed/cancelled attempt would be at the BACK with a real
    timestamp. We want the latest ASSISTED_PENDING; walk from the end.
    """
    for app in reversed(apps):
        if app.status is ApplicationStatus.ASSISTED_PENDING:
            return app
    return None


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


# ---------------------------------------------------------------- /api/onboarding (5/M)

# Phase 4 (5/M) — guided-but-skippable onboarding wizard (spec §11a).
#
# Step-wise endpoints (not one big PUT) so the user can close the tab
# mid-step and reopen later. Each step writes to the same artifacts the
# rest of v3 reads from (master.json + user_config.json) so the dashboard
# / CLI / scheduler-ready gate all see the same truth — no separate
# "wizard state" table to drift out of sync.


@api_router.get("/onboarding/state")
async def onboarding_state(request: Request) -> dict:
    """Return the current onboarding state — which steps are complete,
    plus the current saved values so the wizard renders the existing
    data as defaults when the user re-opens the tab."""
    web_state = _get_state(request)
    status = onboarding_status(web_state.settings.data_dir)
    return status.to_dict()


@api_router.post("/onboarding/extract-resume")
async def onboarding_extract_resume(request: Request) -> dict:
    """Extract a fact-bank DRAFT from an uploaded résumé so the wizard can pre-fill the
    contact / work-history / skills steps for the user to REVIEW.

    Payload is base64-in-JSON (so no python-multipart dependency, and it reuses the same JSON
    request path as every other step): ``{"filename": "me.pdf", "content_b64": "<base64>"}``.
    Returns the fact-bank dict (master.json shape). It does NOT persist — the per-step Save
    endpoints remain the only writers, so the user reviews every extracted field before anything
    is stored (faithful-but-unverified is a draft, never the source of truth until confirmed)."""
    import base64

    from auto_applier.llm.complete import build_default
    from auto_applier.resume.extract import extract_factbank, extract_text_from_bytes
    from auto_applier.web.onboarding import _fact_bank_to_dict

    payload = await _read_json_dict(request)
    filename = str(payload.get("filename") or "")
    content_b64 = payload.get("content_b64") or ""
    if not filename or not content_b64:
        raise HTTPException(status_code=400, detail="need {filename, content_b64}")
    try:
        data = base64.b64decode(content_b64)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid base64: {exc}")
    try:
        text = extract_text_from_bytes(data, filename)
    except ValueError as exc:  # unsupported extension
        raise HTTPException(status_code=400, detail=str(exc))
    web_state = _get_state(request)
    try:
        bank = await extract_factbank(text, build_default(web_state.settings))
    except Exception as exc:  # noqa: BLE001 — LLM/transport failure → 502 (not a server crash)
        raise HTTPException(status_code=502, detail=f"résumé extraction failed: {exc}")
    return _fact_bank_to_dict(bank)


@api_router.post("/onboarding/contact")
async def onboarding_contact(request: Request) -> dict:
    """Save contact info (name + email + phone + location + links).
    Empty strings clear existing fields — the user may need to blank a
    wrong entry."""
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_contact(bank, payload)
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/work-history")
async def onboarding_work_history(request: Request) -> dict:
    """Replace the work-history list wholesale. Payload shape:
    ``{"work_history": [{"company", "title", "start", "end", "bullets"}]}``.
    """
    payload = await _read_json_dict(request)
    entries = payload.get("work_history", [])
    if not isinstance(entries, list):
        raise HTTPException(
            status_code=400, detail="work_history must be a list",
        )
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_work_history(bank, entries)
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/education")
async def onboarding_education(request: Request) -> dict:
    """Replace education entries wholesale. Payload: ``{"education": [...]}.``
    Optional step — empty list is fine if the user has no formal
    education to list."""
    payload = await _read_json_dict(request)
    entries = payload.get("education", [])
    if not isinstance(entries, list):
        raise HTTPException(
            status_code=400, detail="education must be a list",
        )
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_education(bank, entries)
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/skills")
async def onboarding_skills(request: Request) -> dict:
    """Replace the skills list. Payload: ``{"skills": ["Python", ...]}.``
    Dedupes case-insensitively + drops empties before saving."""
    payload = await _read_json_dict(request)
    skills = payload.get("skills", [])
    if not isinstance(skills, list):
        raise HTTPException(
            status_code=400, detail="skills must be a list",
        )
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_skills(bank, [str(s) for s in skills])
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/work-auth")
async def onboarding_work_auth(request: Request) -> dict:
    """Save work-authorization + sponsorship status (spec §6b — no silent
    default). Payload: ``{"work_authorization": "US citizen",
    "requires_sponsorship": false}``. ``requires_sponsorship`` may be
    ``null`` to leave the question unanswered (the apply path then bails
    to REVIEW on that question instead of guessing)."""
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_work_auth(bank, payload)
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/extras")
async def onboarding_extras(request: Request) -> dict:
    """Save the OPTIONAL extra screener fields so the answer resolver can fill them instead of
    bailing to REVIEW. Payload: ``{"primary_nationality": "United States", "notice_period":
    "Two weeks", "languages": ["English"], "gender": "Male"}`` — every field optional; blank clears
    it (resolver then bails that field to assisted, never guessing). Gender is a voluntary EEO
    self-ID (honesty invariant: blank stays "prefer not to answer").

    ``notice_period`` doubles as "earliest start / availability" (one fact-bank field answers both
    phrasings). **Salary is NOT here** — it lives in ``targeting.salary_floor``, written by the
    Targeting step + the conversational onboarding helper (the single targeting writer)."""
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    merge_extras(bank, payload)
    save_fact_bank(web_state.settings.data_dir, bank)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/targeting")
async def onboarding_targeting(request: Request) -> dict:
    """Save job-targeting config — the SINGLE writer for ``targeting`` (the onboarding
    wizard AND the Direction 2 Phase C dashboard Goals editor both POST here). Payload mirrors
    :class:`TargetingConfig`: ``{"titles": [...], "locations": [...], "remote_ok": bool,
    "onsite_ok": bool, "salary_floor": int|null, "seniority": "...", "preferences": [...],
    "greenhouse_boards": [...], "lever_boards": [...], "ashby_boards": [...]}``.

    The board-list keys are accepted from the Goals editor (the wizard never sends them, so its
    behaviour is unchanged) and sanitized — trimmed, empties dropped, deduped by exact value with
    order preserved. Exact-value dedupe (not case-folded) is deliberate: Ashby slugs are
    case-sensitive ("Linear", "Notion", "OpenAI"), so lowercasing would break the probe.
    """
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    cfg = load_user_config(web_state.settings.data_dir)
    targeting = cfg.get("targeting", {}) or {}
    # Merge field-by-field so a partial payload doesn't blank fields the
    # caller didn't include. Lists default to [] when the caller sends
    # them so "I cleared all titles" works.
    for k in ("titles", "locations", "remote_ok", "onsite_ok",
              "salary_floor", "seniority", "preferences"):
        if k in payload:
            targeting[k] = payload[k]
    for k in ("greenhouse_boards", "lever_boards", "ashby_boards"):
        if k in payload:
            targeting[k] = _clean_slug_list(payload[k])
    cfg["targeting"] = targeting
    save_user_config(web_state.settings.data_dir, cfg)
    return onboarding_status(web_state.settings.data_dir).to_dict()


def _clean_slug_list(value) -> list[str]:
    """Trim, drop empties, and dedupe (by exact value, order-preserving) a board-slug list.
    A non-list payload yields ``[]`` so a malformed Goals-editor submit clears rather than
    corrupts. Case is preserved — Ashby slugs are case-sensitive."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        slug = str(item).strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


@api_router.post("/onboarding/goal-chat")
async def onboarding_goal_chat(request: Request) -> dict:
    """Drive the scripted goal-elicitation chat one turn at a time (Direction 1, Phase B).

    Stateless: the client sends the step it's answering, the user's free-text answer, and the
    draft-so-far; the server parses the answer into targeting fields (the LLM is a bounded parser
    with a deterministic fallback — see :mod:`auto_applier.onboarding_chat`), merges into the draft,
    and returns the next scripted question. Payload:
    ``{"step": "roles"|null, "answer": "...", "draft": {...}}`` — a missing/empty ``step`` means
    "start" and returns the first question without parsing anything.

    Does NOT persist: it returns the evolving ``draft`` for the wizard to show for REVIEW; the user
    then saves via ``/onboarding/targeting`` (the single writer), exactly like the résumé-upload
    prefill. Reply object: ``{"reply", "next_step", "draft", "done", "updates"}``."""
    from auto_applier.llm.complete import build_default
    from auto_applier.onboarding_chat import (
        apply_updates,
        detect_relocation,
        first_step,
        next_step_after,
        parse_answer,
        step_for_key,
        suggest_adjacent_roles,
        summarize,
    )

    payload = await _read_json_dict(request)
    step_key = str(payload.get("step") or "")
    answer = str(payload.get("answer") or "")
    draft = payload.get("draft") or {}
    if not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="draft must be an object")

    if not step_key:
        s = first_step()
        return {"reply": s.question, "next_step": s.key, "draft": draft,
                "done": False, "updates": {}}

    if step_for_key(step_key) is None:
        raise HTTPException(status_code=400, detail=f"unknown step '{step_key}'")

    web_state = _get_state(request)
    # Best-effort LLM: parse_answer falls back to deterministic parsing if this is None or errors,
    # so a missing/unreachable Ollama degrades the parse rather than breaking the chat.
    try:
        llm = build_default(web_state.settings)
    except Exception:  # noqa: BLE001
        llm = None

    updates = await parse_answer(step_key, answer, llm)

    notes: list[str] = []
    # Preserve relocation/abroad intent stated at the location step (it was being collapsed to a
    # narrow "country, remote"); record it as a priority so it isn't lost.
    if step_key == "location" and detect_relocation(answer):
        updates = apply_updates(updates, {"preferences": ["open to relocation / visa sponsorship"]})
        notes.append("Noted that you're open to relocating — I've kept on-site roles in and added "
                     "visa sponsorship to your priorities.")

    draft = apply_updates(draft, updates)

    # "Suggest, you confirm" role widening: when the roles answer was vague/narrow, offer adjacent
    # titles the user can tap to add (never auto-added).
    suggestions: dict = {}
    if step_key == "roles" and len(draft.get("titles") or []) <= 2:
        extra = suggest_adjacent_roles(list(draft.get("titles") or []), answer)
        if extra:
            suggestions["roles"] = extra

    nxt = next_step_after(step_key)
    # Don't ask for a minimum salary we already captured from earlier free-text.
    if nxt is not None and nxt.key == "comp" and draft.get("salary_floor"):
        notes.append(f"Using ${int(draft['salary_floor']):,} as your minimum (from earlier).")
        nxt = next_step_after("comp")

    if nxt is None:
        body = summarize(draft)
        reply = (" ".join(notes) + "\n\n" + body) if notes else body
        return {"reply": reply, "next_step": None, "draft": draft, "done": True,
                "updates": updates, "suggestions": suggestions}
    reply = (" ".join(notes) + "\n\n" + nxt.question) if notes else nxt.question
    return {"reply": reply, "next_step": nxt.key, "draft": draft, "done": False,
            "updates": updates, "suggestions": suggestions}


# -- background "find companies" (seed-boards) for the targeting step ----------------
# Single-user local app → one job at a time, held in a module-level dict (NOT on WebState,
# which is read-only-by-design). The probe is sync (httpx + ~1 req/s throttle) so it runs in a
# thread via asyncio.to_thread; the POST returns immediately and the wizard polls /status, so a
# minutes-long sweep never blocks a request and the user keeps onboarding while it runs.
_SEED: dict = {"status": "idle"}
_SEED_TASK = None  # keep a reference so the running task isn't garbage-collected


async def _run_seed_job(settings, titles, limit: int) -> None:
    """Probe candidate slugs, merge the live + title-relevant ones into targeting.*_boards, and
    persist. Updates the module-level ``_SEED`` dict (read by /seed-boards/status)."""
    from auto_applier.pipeline.seed_worker import BoardSeeder
    from auto_applier.web.onboarding import load_user_config, save_user_config

    cache_path = settings.data_dir / "ats_probe_cache.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}

    def _progress(s) -> None:  # called from the worker thread (GIL-safe dict writes)
        _SEED.update(probed=s.probed, kept=s.kept, dead=s.dead,
                     irrelevant=s.live_irrelevant)

    try:
        seeder = BoardSeeder(
            settings=settings,
            titles=titles if titles is not None else list(settings.targeting.titles),
            relevant_only=True,
            limit=limit,
            probe_cache=cache,
            progress=_progress,
        )
        summary = await asyncio.to_thread(seeder.run)
        if summary.kept:
            merged = seeder.merged_targeting(summary)
            cfg = load_user_config(settings.data_dir)
            cfg.setdefault("targeting", {})
            for key, lst in merged.items():
                cfg["targeting"][key] = lst
            save_user_config(settings.data_dir, cfg)
        try:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
        _SEED.update(
            status="done", probed=summary.probed, kept=summary.kept, dead=summary.dead,
            added={k: len(v) for k, v in summary.added.items()},
            note=(f"Added {summary.kept} board(s) in your field."
                  if summary.kept else "No new boards matched — try broadening your titles."),
        )
    except Exception as exc:  # noqa: BLE001 — surface as an error status, never crash the loop
        _SEED.update(status="error", error=str(exc))


@api_router.post("/onboarding/seed-boards/start")
async def onboarding_seed_boards_start(request: Request) -> dict:
    """Kick off a BACKGROUND probe that finds company boards matching the user's titles and
    merges the verified-live ones into ``targeting.*_boards``. Returns immediately; poll
    ``/onboarding/seed-boards/status``. Idempotent while running (a second call returns the
    in-flight job instead of starting another)."""
    global _SEED, _SEED_TASK
    payload = await _read_json_dict(request)
    if _SEED.get("status") == "running":
        return dict(_SEED)
    titles = payload.get("titles")
    if titles is not None and not isinstance(titles, list):
        raise HTTPException(status_code=400, detail="titles must be a list")
    try:
        limit = int(payload.get("limit") or 120)
    except (TypeError, ValueError):
        limit = 120
    limit = max(1, min(limit, 400))
    web_state = _get_state(request)
    _SEED = {"status": "running", "probed": 0, "kept": 0, "dead": 0, "added": {}, "note": ""}
    _SEED_TASK = asyncio.create_task(_run_seed_job(web_state.settings, titles, limit))
    return dict(_SEED)


@api_router.get("/onboarding/seed-boards/status")
async def onboarding_seed_boards_status(request: Request) -> dict:
    """Current state of the background seed-boards job: idle | running | done | error, plus the
    live probed / kept / dead counters."""
    return dict(_SEED)


# -- first-run setup: readiness + in-app bootstrap (pull models / install browser) -----------
# Same one-job-at-a-time, module-level pattern as seed-boards above. The heavy work (multi-GB
# model pull, browser download) is blocking, so each job runs in a thread via asyncio.to_thread
# and the dashboard polls /status. Models/browser are SURFACED here, never a hard gate — the
# scheduler-ready gate stays fact-bank-only (spec §11a onboarding restructure).
_SETUP_ACTIONS = ("pull-models", "install-browser")
_SETUP: dict = {action: {"status": "idle"} for action in _SETUP_ACTIONS}
_SETUP_TASKS: dict = {}  # action -> asyncio.Task (kept so the running task isn't GC'd)


async def _run_pull_models_job(settings) -> None:
    from auto_applier import setup_ops

    def _progress(frag: dict) -> None:  # called from the worker thread (GIL-safe dict writes)
        _SETUP["pull-models"].update(frag)

    try:
        result = await asyncio.to_thread(setup_ops.pull_models, settings, _progress)
        _SETUP["pull-models"].update(
            status="done" if result.ok else "error",
            error="" if result.ok else (result.error or "pull failed"),
        )
    except Exception as exc:  # noqa: BLE001 — surface as an error status, never crash the loop
        _SETUP["pull-models"].update(status="error", error=str(exc))


async def _run_install_browser_job(settings) -> None:
    from auto_applier import setup_ops

    def _progress(frag: dict) -> None:
        _SETUP["install-browser"].update(frag)

    try:
        result = await asyncio.to_thread(setup_ops.install_browser, _progress)
        _SETUP["install-browser"].update(
            status="done" if result.ok else "error",
            backend=result.backend_used,
            error="" if result.ok else (result.error or "install failed"),
        )
    except Exception as exc:  # noqa: BLE001
        _SETUP["install-browser"].update(status="error", error=str(exc))


_SETUP_JOBS = {
    "pull-models": _run_pull_models_job,
    "install-browser": _run_install_browser_job,
}


@api_router.get("/setup/readiness")
async def setup_readiness(request: Request) -> dict:
    """First-run readiness: the LLM-models + browser checks as JSON (reuses `av3 doctor`)."""
    from auto_applier import setup_ops

    web_state = _get_state(request)
    checks = setup_ops.readiness(web_state.settings)
    return {
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail, "fix": c.fix}
            for c in checks
        ]
    }


@api_router.post("/setup/{action}/start")
async def setup_start(action: str, request: Request) -> dict:
    """Kick off a BACKGROUND setup job (``pull-models`` | ``install-browser``). Returns
    immediately; poll ``/setup/{action}/status``. Idempotent while running (a second call
    returns the in-flight job instead of starting another)."""
    if action not in _SETUP_ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown setup action: {action}")
    if _SETUP[action].get("status") == "running":
        return dict(_SETUP[action])
    web_state = _get_state(request)
    _SETUP[action] = {"status": "running", "percent": 0, "phase": "starting", "error": ""}
    _SETUP_TASKS[action] = asyncio.create_task(_SETUP_JOBS[action](web_state.settings))
    return dict(_SETUP[action])


@api_router.get("/setup/{action}/status")
async def setup_status(action: str) -> dict:
    """Current state of a background setup job: idle | running | done | error (+ progress)."""
    if action not in _SETUP_ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown setup action: {action}")
    return dict(_SETUP[action])


@api_router.post("/onboarding/telemetry")
async def onboarding_telemetry(request: Request) -> dict:
    """Save the telemetry opt-in decision (spec §9). Payload:
    ``{"enabled": bool, "handle": str|null, "relay_url": str|null}``.
    The presence of the ``telemetry`` key in user_config — even with
    ``enabled: false`` — counts as "the user made a decision" for the
    onboarding-complete gate."""
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    cfg = load_user_config(web_state.settings.data_dir)
    telemetry = cfg.get("telemetry", {}) or {}
    for k in ("enabled", "handle", "relay_url"):
        if k in payload:
            telemetry[k] = payload[k]
    # Default to OFF when the user submitted without the enabled key —
    # spec §9 says telemetry is opt-IN.
    telemetry.setdefault("enabled", False)
    cfg["telemetry"] = telemetry
    save_user_config(web_state.settings.data_dir, cfg)
    return onboarding_status(web_state.settings.data_dir).to_dict()


@api_router.post("/onboarding/web-prefs")
async def onboarding_web_prefs(request: Request) -> dict:
    """Save the F6 hotkey + idle-detect preferences from the wizard.
    Payload keys mirror :class:`WebConfig`: ``hotkey_enabled``,
    ``hotkey``, ``idle_detect_enabled``, ``idle_threshold_s``.

    A change requires restarting ``av3 serve`` to re-register the
    hotkey — the wizard surfaces that note in the UI. We don't restart
    the watchers from here because that would race with whatever the
    user is doing in the dashboard right now."""
    payload = await _read_json_dict(request)
    web_state = _get_state(request)
    cfg = load_user_config(web_state.settings.data_dir)
    web_cfg = cfg.get("web", {}) or {}
    for k in ("hotkey_enabled", "hotkey",
              "idle_detect_enabled", "idle_threshold_s"):
        if k in payload:
            web_cfg[k] = payload[k]
    cfg["web"] = web_cfg
    save_user_config(web_state.settings.data_dir, cfg)
    return onboarding_status(web_state.settings.data_dir).to_dict()


def _verify_imap(host: str, port: int, user: str, password: str) -> str | None:
    """Best-effort IMAP login check. Returns ``None`` on success, else a short, user-facing
    error string. Most app-password setups fail on a typo'd password, so verifying before we
    save is the single highest-value friction-reducer for a non-technical user."""
    import imaplib

    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=10)
    except Exception as exc:  # noqa: BLE001 — DNS/refused/TLS all surface the same way to the user
        return f"could not connect to {host}:{port} ({type(exc).__name__})"
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        return "authentication failed — double-check the email and the 16-char App Password"
    except Exception as exc:  # noqa: BLE001
        return f"sign-in error ({type(exc).__name__})"
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass
    return None


@api_router.post("/onboarding/inbox")
async def onboarding_inbox(request: Request) -> dict:
    """Connect the user's mailbox for the email outcome loop (Direction 4 Phase D).

    The guided alternative to hand-editing ``user_config.json`` + ``.env``. Payload:
    ``{"user": "you@gmail.com", "password": "<16-char app password>", "host"?, "port"?}``.
    Verifies the credentials with a live IMAP login FIRST (so a typo'd password fails here,
    not silently at the next scheduler cycle); only on success does it persist. The non-secret
    fields (enabled/user/host/port) go to ``user_config.json``; the **password goes ONLY to
    ``<data_dir>/.env`` as ``AV3_IMAP_PASSWORD``** — never to the JSON (the project's .env-only
    secrets rule). Takes effect for the inbox gather stage on the next worker restart.
    """
    from dotenv import set_key

    payload = await _read_json_dict(request)
    user = str(payload.get("user") or "").strip()
    password = str(payload.get("password") or "").strip()
    host = str(payload.get("host") or "imap.gmail.com").strip() or "imap.gmail.com"
    try:
        port = int(payload.get("port") or 993)
    except (TypeError, ValueError):
        port = 993
    if not user or not password:
        raise HTTPException(status_code=400,
                            detail="email address and App Password are both required")

    err = _verify_imap(host, port, user, password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    web_state = _get_state(request)
    data_dir = web_state.settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    # Secret → .env (never user_config.json). set_key creates the file if absent + quotes safely.
    env_path = data_dir / ".env"
    env_path.touch(exist_ok=True)
    set_key(str(env_path), "AV3_IMAP_PASSWORD", password)
    os.environ["AV3_IMAP_PASSWORD"] = password  # available to this process immediately

    # Non-secret config → user_config.json (merge so we don't clobber other inbox knobs).
    cfg = load_user_config(data_dir)
    inbox = cfg.get("inbox", {}) or {}
    inbox.update(enabled=True, user=user, host=host, port=port)
    cfg["inbox"] = inbox
    save_user_config(data_dir, cfg)

    return {"ok": True, "user": user, "host": host, "port": port,
            "note": "Email connected. Outcome tracking starts on the next worker restart."}


async def _read_json_dict(request: Request) -> dict:
    """Parse a JSON body as a dict; reject other shapes with 400. The
    onboarding endpoints all take dict payloads, so a list/scalar should
    fail fast rather than mysteriously do nothing."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="request body must be valid JSON",
        )
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="request body must be a JSON object",
        )
    return body


# ---------------------------------------------------------------- /api/reconcile (7/M)
#
# The interactive skill-reconciliation conversation (spec §7b) — the web
# counterpart of `av3 reconcile`. The conversation shape: the app SURFACES
# skills the stored JDs demand that the fact bank lacks; the user CONFIRMS the
# ones they actually have; only that explicit confirmation mutates the bank
# (Rule 2.6 — the bank is the fabrication guard's source of truth, additive
# insert only).

@api_router.get("/reconcile/proposals")
async def reconcile_proposals(request: Request, min_count: int = 1) -> dict:
    """Open skill-gap proposals + bank size. Pure read."""
    from auto_applier.db.repositories import SkillGapRepo
    from auto_applier.reconcile import build_proposals

    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    with web_state.app_conn() as conn:
        proposals = build_proposals(bank, SkillGapRepo(conn), min_count=min_count)
    return {
        "proposals": [{"skill": p.skill, "count": p.count} for p in proposals],
        "bank_skill_count": len(bank.skills),
    }


@api_router.post("/reconcile/scan")
async def reconcile_scan(request: Request) -> dict:
    """Scan every stored JD and record demanded-but-missing skills as gaps.
    Gather-only — writes the gap table, never the fact bank."""
    from auto_applier.db.repositories import SkillGapRepo
    from auto_applier.reconcile import record_batch_gaps

    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    with web_state.app_conn() as conn:
        jobs = JobRepo(conn).list_all_with_description()
        bumps = record_batch_gaps(jobs, bank, SkillGapRepo(conn))
        conn.commit()
    return {"scanned": len(jobs), "bumps": bumps}


@api_router.post("/reconcile/apply")
async def reconcile_apply(request: Request) -> dict:
    """Insert the user-confirmed skills into the fact bank (the gated act).
    Additive only — appends to master.json, marks those gaps reconciled."""
    from auto_applier.db.repositories import SkillGapRepo
    from auto_applier.reconcile import apply_proposals

    payload = await _read_json_dict(request)
    skills = payload.get("skills")
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        raise HTTPException(status_code=400, detail="'skills' must be a list of strings")
    approved = [s.strip() for s in skills if s.strip()]
    if not approved:
        raise HTTPException(status_code=400, detail="no skills provided")

    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)
    before = len(bank.skills)
    apply_proposals(bank, approved)
    save_fact_bank(web_state.settings.data_dir, bank)
    with web_state.app_conn() as conn:
        gap_repo = SkillGapRepo(conn)
        for skill in approved:
            gap_repo.set_status(skill, "certified")
        conn.commit()
    return {
        "added": len(bank.skills) - before,
        "reconciled": len(approved),
        "bank_skill_count": len(bank.skills),
    }


# ---------------------------------------------------------------- /api/copilot (§8f)

@api_router.post("/copilot/ask")
async def copilot_ask(request: Request) -> dict:
    """Application copilot (spec §8f): one honest, audited answer per question.
    Payload: ``{"question": str, "job_id": str?}``. The deterministic evidence
    audit fails an unsupported yes closed to review; sensitive questions are
    answered from bank/config policy, never the LLM. Advisory only — nothing
    here flows into a form unattended."""
    from auto_applier.copilot import Copilot
    from auto_applier.llm.complete import build_default
    from auto_applier.resume.salary import format_ask, parse_posted_range, recommend_ask

    payload = await _read_json_dict(request)
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(status_code=400, detail="'question' must be a non-empty string")

    web_state = _get_state(request)
    bank = load_fact_bank(web_state.settings.data_dir)

    job = None
    job_id = payload.get("job_id")
    if job_id:
        with web_state.app_conn() as conn:
            job = JobRepo(conn).get(str(job_id))
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    cfg = web_state.settings.salary
    posted = parse_posted_range(job.compensation if job else None)
    salary_ask = format_ask(
        recommend_ask(user_floor=cfg.floor, user_ceiling=cfg.ceiling, posted=posted)
    )

    copilot = Copilot(build_default(web_state.settings))
    answer = await copilot.answer(question, bank, job=job, salary_ask=salary_ask)
    return vars(answer)


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


@pages_router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request) -> HTMLResponse:
    """The onboarding wizard. Single-page Alpine.js app that walks the
    user through contact → work history → skills → work-auth → targeting
    → telemetry → web prefs. Each step posts to the matching
    /api/onboarding/* endpoint so closing the tab mid-step is safe."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {"version": __version__},
    )


@pages_router.get("/copilot", response_class=HTMLResponse)
async def copilot_page(request: Request) -> HTMLResponse:
    """The application copilot (spec §8f): paste a screener question, optionally
    attach a job id, get back an honest, evidence-audited answer."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "copilot.html",
        {"version": __version__},
    )


@pages_router.get("/reconcile", response_class=HTMLResponse)
async def reconcile_page(request: Request) -> HTMLResponse:
    """The interactive skill-reconciliation conversation (spec §7b, 7/M).
    Surfaces JD-demanded skills the fact bank lacks; the user checks the
    ones they actually have and confirms — the only path that mutates the
    bank, and it's additive."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "reconcile.html",
        {"version": __version__},
    )


@pages_router.get("/in-progress", response_class=HTMLResponse)
async def in_progress_page(request: Request) -> HTMLResponse:
    """The batched assisted-review "In Progress" page (Phase 3). One tab per prepared job, each
    showing the COMPLETE proposed application (confident fills + must-verify drafts), per-field
    plain-text copy buttons, an open-the-application action, per-job disposition (mark applied /
    skip), and a release-batch button. Single-page Alpine.js app polling ``/api/batch``."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "in_progress.html",
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
