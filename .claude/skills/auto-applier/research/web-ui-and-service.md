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
