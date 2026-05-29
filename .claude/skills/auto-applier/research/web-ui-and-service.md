# Web UI & worker service — Phase 4 (spec §3, §10, §11b)

Knowledge base for the Phase 4 work that turns the v3 CLI-driven scheduler
into a long-running service with a local web dashboard. Where `pipeline-staging.md`
was about the queue between workers, this doc is about the **process boundary**:

```
av3 serve  ──►  FastAPI app
                ├─ lifespan → SchedulerService → asyncio.Task(Scheduler.run)
                ├─ /api/*   read-only JSON for the dashboard
                ├─ /        Jinja2 templates + static assets (Alpine.js)
                └─ /events  SSE stream (2/M) tailing events.db
```

Each sub-phase appends a `## Phase 4 (X/M) — <slug>` section here as the
work lands.

---

## Phase 4 (1/M) — web skeleton + worker service landed

`av3/web/` package, `av3 serve` CLI, 16 new tests. **Spec §3 + §11b Phase 4
bullet 1 satisfied**: the staged-worker scheduler now runs as an asyncio task
inside a FastAPI lifespan; read-only JSON endpoints expose pipeline state for
the (2/M) dashboard to consume. The dashboard itself is a (2/M) deliverable;
this sub-phase ships the placeholder splash page and the API surface it will
consume.

### Module layout

```
av3/web/
  __init__.py        re-exports + lazy ``create_app`` shim
  app.py             FastAPI factory + lifespan (start/stop SchedulerService)
  service.py         SchedulerService — lifecycle wrapper for av3.pipeline.Scheduler
  routes.py          api_router (JSON) + pages_router (HTML)
  state.py           WebState — settings + app.db path + events.db path
  views.py           Domain → dict DTO helpers; PIPELINE_STATES column order
  templates/
    base.html
    index.html       Splash page (lists JSON endpoints)
  static/
    app.css          ~150 lines, dark-mode aware, system font stack
```

### Why an async factory (and the `teardown` callback)

`SchedulerService.__init__` takes an `AsyncSchedulerFactory` (callable returning
a `Scheduler`), not a pre-built scheduler. Two forces converge here:

1. **Pause predicate is build-time-immutable.** The `Scheduler` constructor
   captures its `pause_predicate` closure once. The service needs to inject a
   predicate that reads `self._paused`, which only exists after the service is
   instantiated. The factory closure resolves this circular ownership cleanly.
2. **Real builders are async.** `BrowserSession.start()` is awaitable, and the
   LLM clients may health-probe. Forcing a sync factory would push that setup
   out to the CLI before uvicorn owns the event loop, fragmenting lifecycle.
   The async factory runs inside `service.start()`, which itself runs inside
   the FastAPI lifespan.

A `teardown` callback mirrors `factory` for the closing side — typically the
CLI uses it to stop the BrowserSession the factory started. Tests pass
`sync_factory(lambda p: StubScheduler(p))` and skip teardown.

### Why per-request sqlite3 connections (and not a shared one)

The WebState exposes `app_conn()` / `events_conn()` as **context managers**
that open short-lived connections per request. Why not share a long-lived
connection like the workers do?

* **Thread affinity.** `sqlite3.Connection` forbids cross-thread use by
  default. Uvicorn's event loop runs on the main thread (fine), but FastAPI's
  `TestClient` and any future threadpool dispatch can hop threads, breaking
  the shared-conn pattern. Per-request connections sidestep this entirely.
* **WAL makes it cheap.** Opening a SQLite WAL file is ~1 ms; there's no
  socket, no auth, no negotiation. The dashboard polls maybe once a second
  per panel — overhead is invisible.
* **Concurrent requests scale naturally.** If we later want to serve the
  dashboard from a runner box to multiple browsers, this pattern Just Works
  without coordination.

The scheduler workers keep their own long-lived connections (single asyncio
loop → single thread; sharing is safe). The web layer is read-only;
contention is bounded.

### Why disable `/docs` and `/openapi.json`

The local dashboard IS the UI. FastAPI's default Swagger UI advertises every
endpoint, and if a user ever flips `host: "0.0.0.0"` to share the dashboard
on the LAN, that Swagger page leaks the API surface and the scheduler state
to anything on the network. Off by default; we'll add real auth + selective
exposure if (when) v3.1 ships a multi-user mode.

### Why `--no-scheduler` mode (and pre-onboarding ergonomics)

`av3 serve` runs the web UI even when the fact bank + résumé prerequisites
aren't satisfied — the dashboard needs to **load** before the (5/M)
onboarding wizard exists, otherwise there's no way to seed those
prerequisites through the UI. The CLI prints a friendly note to stderr but
the FastAPI app boots normally with `service=None`. All read-only endpoints
answer; the status endpoint reports `{"running": false, "paused": false}`.

`--no-scheduler` is an explicit flag for the same posture (diagnostics mode
on a populated install).

### Edge cases covered (see `tests_v3/test_web_skeleton.py`)

| Concern | Test |
|---|---|
| Scheduler task spawns + records its predicate | `test_start_spawns_scheduler_task` |
| Cancellation propagates cleanly | `test_stop_cancels_the_task` |
| Double-start is a no-op | `test_start_is_idempotent` |
| Pause flag reads live, not at build time | `test_pause_predicate_reads_live_flag` |
| Teardown runs even if task already exited | `test_teardown_runs_after_stop` |
| Stop without start is safe | `test_stop_without_start_is_safe` |
| Snapshot shape stable | `test_snapshot_shape` |
| Read-only API answers without a service | `test_status_with_no_service` |
| Counts surface real seeded data | `test_status_reports_real_counts` |
| Queue endpoint excludes DISCOVERED | `test_queue_endpoint` |
| Sources empty list != "all healthy" | `test_sources_endpoint_empty` |
| Health registry → API shape | `test_sources_endpoint_reflects_health_registry` |
| Splash page renders 200 | `test_index_page_renders` |
| /docs disabled | `test_docs_are_disabled` |
| Lifespan boots + tears down the service | `test_lifespan_starts_and_stops_service` |

### What's NOT in this sub-phase

* **Dashboard UI.** Only the splash page exists; the live pipeline + queue +
  history panels arrive in (2/M) along with the SSE event stream.
* **Pause toggle endpoint.** `SchedulerService.pause()` / `.resume()` are
  callable, but no API endpoint exposes them. (3/M) wires the dashboard
  button + F6 hotkey + idle-detect into the same predicate.
* **Login-on-demand UX.** `/api/sources` returns the health snapshot; the
  "log in" button + headed-browser launch is (4/M).
* **Onboarding wizard.** Pre-onboarding the scheduler silently doesn't
  start; (5/M) makes the dashboard walk the user through fact-bank seeding,
  résumé upload, targeting filters, telemetry opt-in.
* **One-click launcher.** Right now you run `av3 serve` from a terminal;
  (6/M) ships a `.cmd` wrapper that activates the venv, runs `av3 serve`,
  and opens the default browser to `http://127.0.0.1:8765`.

### Carry-over: shared scheduler-builder duplication

`av3 serve` and `av3 run` (the CLI's existing scheduler entry) construct the
same set of workers + BrowserSession nearly verbatim. Right now the
duplication is acceptable because both entries have different lifecycle
shapes (uvicorn vs. plain asyncio.run), but if (5/M) or (6/M) grows a third
caller, the worker construction needs to factor into one builder. Note this
in the next sub-phase if it bites.

---

## Phase 4 (2/M) — dashboard UI landed

Three new JSON endpoints + SSE stream + the three-panel dashboard +
per-job detail page. `tests_v3/test_web_dashboard.py` (+15 tests; full v3
suite 397 green, 11 deselected by design). **Spec §10 + §11b Phase 4
bullets 2–3 satisfied:** "live pipeline status, review queue +
login-needed badges, application history + outcomes (with confirmation
status)" all surface in the dashboard.

### New surface

| Endpoint | Purpose |
|---|---|
| `/api/history?limit=N` | Recent applications joined with jobs + scores |
| `/api/jobs/<id>` | Per-job detail (job + score + applications) |
| `/api/events` | SSE stream tailing events.db (live activity) |
| `/jobs/<id>` | Per-job HTML page (fetches `/api/jobs/<id>` on load) |
| `/` (rewritten) | The real 3-panel dashboard, replaces the (1/M) splash |

### Dashboard layout (templates/dashboard.html)

```
┌─────────────────────────────────────────────┐
│ ● status bar — running/paused + last cycle  │
├─────────────────────────────────────────────┤
│ Pipeline                                    │
│  DISCOVERED  DESCRIBED  SCORED  ...  APPLIED│  ← 11 state cells
├─────────────────────────────────────────────┤
│ Sources                                     │
│  greenhouse  HEALTHY                        │
│  lever       AUTH_REQUIRED  session expired │  ← (4/M) login-needed badge feed
├──────────────────────┬──────────────────────┤
│ Review queue (N)     │ Queued + applying    │
│   job-1              │   APPLYING  job-7    │
│   job-2              │   QUEUED    job-8    │
├──────────────────────┴──────────────────────┤
│ Recent applications                         │
│  ts  status  mode  job  score               │  ← /api/history
├─────────────────────────────────────────────┤
│ Live activity (SSE feed)                    │
│  filter   ok    job-1                       │
│  score    ok    job-1                       │
│  apply    error  …                          │
└─────────────────────────────────────────────┘
```

### Why polling + SSE both

The dashboard runs two refresh paths:

* **Polling** (`/api/status` + `/api/sources` + `/api/queue` +
  `/api/history` every 5 s). What keeps the panel counts truthful — SSE
  alone can't tell you the REVIEW count after a burst.
* **SSE** (`/api/events`). Drives the recent-activity feed and *prods*
  the next poll cycle to pick up state changes promptly. The poll alone
  would be ≤5 s late on every transition; the SSE refresh nudge cuts
  that to one cycle.

Both can fail gracefully — if SSE drops (proxy idle-out), the polling
keeps the dashboard truthful; if polling fails for a tick, SSE still
shows live activity.

### Why a keepalive comment

The SSE generator yields `: keepalive\n\n` on every poll cycle even when
there's no new event. SSE comments (`:` prefix) are silently dropped by
EventSource clients but they do two jobs:

1. **Prevent proxy idle-out.** Nginx, Cloudflare, and most reverse proxies
   close long-quiet HTTP streams. A regular comment frame keeps the
   socket warm — needed once `host=0.0.0.0` lands the dashboard behind
   any proxy.
2. **Make the stream testable.** Without a steady chunk cadence, unit
   tests that read from the stream block indefinitely waiting for the
   next real event. The keepalive guarantees forward progress so a test
   can read N frames + cancel cleanly.

### Why test the SSE generator directly (not the HTTP route)

FastAPI's `TestClient` runs the ASGI app in an anyio portal thread, and
that path *doesn't release SSE chunks promptly* to the consumer — neither
`iter_raw()` chunks nor even the response headers come back in a bounded
time. `httpx.AsyncClient + ASGITransport` buffers the body before
yielding. Both fail to test SSE responsively.

The fix is to call the underlying `_events_stream` async generator
directly with a fake request object whose `is_disconnected()` returns
True when the test wants to cut the stream. That gives:

* **Reliable, fast termination** — the disconnect signal flows through
  the generator's natural check point.
* **Real coverage of the load-bearing logic** — cursor management, row
  fetching, keepalive cadence, exception isolation.
* **The route's HTTP shape** (StreamingResponse + media type) covered by
  a static `inspect.getsource` assertion — no event loop needed for what
  doesn't actually fail.

### Pruning + history endpoint contract

The history endpoint joins each `Application` row with its `Job` and
`JobScore` in Python (one repo read per row). Cheap on SQLite WAL and
avoids a brittle hand-rolled JOIN. The defensive `job: null` fallback
for missing jobs **stays in `history_row`** even though
`FK ON DELETE CASCADE` in `schema.sql` means pruning a job cascades the
applications away too — orphan rows are schema-impossible. The
defensive code is ~3 lines and survives a future schema change where
ON DELETE switches to SET NULL; the test for that branch was removed
because it can't actually be triggered.

### What's NOT in this sub-phase

* **Pause button** + the `/api/control` POST endpoint that drives it —
  (3/M) lands those along with F6 + idle-detect.
* **"Log in" button** for paused sources — (4/M).
* **Job-state filter on history** (e.g. "show me only FAILED in the last
  week"). The dashboard's history panel shows the most-recent N rows
  regardless of status; richer filtering is v3.1 analytics.
* **Server-rendered JD highlights** on the per-job page (e.g. matched
  skills bolded). Static JD only; the score breakdown already covers
  per-axis judgment.
* **Real-time JD streaming** as the apply worker scrolls a form. Just
  the spine events.

### Carry-over: per-app pause control state

`SchedulerService.pause()` / `.resume()` work but no endpoint exposes
them. (3/M) lands `/api/control/pause` + `/api/control/resume` and wires
the dashboard's status bar to flip via Alpine. The (1/M) pause-predicate
plumbing already supports it; the missing piece is the HTTP verb.
