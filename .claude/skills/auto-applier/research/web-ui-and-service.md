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

---

## Phase 4 (3/M) — F6 hotkey + idle-detect + ControlState landed

The (1/M) bare `bool` pause flag became a thread-safe **union of three
sources** — `manual`, `hotkey`, `idle` — so the dashboard button, the
F6 system hotkey, and the optional idle-detector all OR cleanly behind
one predicate. `/api/control/pause` + `/api/control/resume` ship; the
status bar grew a real Pause/Resume button + a "Paused — F6 control-handoff
+ user active 3s ago" reason readout. `tests_v3/test_web_control.py`
(+36 tests; full v3 suite 433 green, 11 deselected by design). **Spec
§7a + §11b Phase 4 bullet 4 satisfied.**

### New surface

| Module | Purpose |
|---|---|
| `av3/web/control.py` | `ControlState` — thread-safe OR union, three sources, immutable `PauseSnapshot` for the API |
| `av3/web/hotkey.py` | `HotkeyWatcher` — Win32 `RegisterHotKey` on a daemon thread; soft-fails non-Win32 |
| `av3/web/idle.py` | `IdleWatcher` — `GetLastInputInfo` poll on a daemon thread; soft-fails non-Win32 |
| `av3/web/service.py` | Refactored: `_paused: bool` → `_control: ControlState`; `pause(source=...)`, `resume(source=...)`, `toggle(source=...)` |
| `av3/web/app.py` | `create_app(... watchers=...)` — lifespan starts watchers AFTER service, stops them BEFORE service teardown |
| `POST /api/control/pause` | Manual pause + optional `{"reason": ...}` body |
| `POST /api/control/resume` | Clears `manual` source only — F6 / idle pauses keep going |
| `/api/status` | Now includes `pause_reasons: {source: reason_string}` |

### Why a union object (and not three bools)

Three pause sources fire from three different threads:

* `manual` — FastAPI request handler (asyncio loop)
* `hotkey` — Win32 message loop on a daemon thread
* `idle`   — poll loop on another daemon thread

A naïve "`if self._manual or self._hotkey or self._idle:`" predicate has
a two-flag race window: thread A clears `_manual` while thread B is
reading `_hotkey`. The `ControlState` centralizes the lock in one place
so the predicate is atomic w.r.t. every mutation. Each mutator returns
the resulting `PauseSnapshot` so the HTTP layer can echo the new state
without a follow-up GET.

### Why Win32 `RegisterHotKey` (and not `pynput` / `keyboard`)

Spec §7a is explicit: *"a system-level key hook so it works even while
the browser has focus."* That rules out per-window listeners. The two
common Python options are:

* **`keyboard` (BoppreH)** — Windows OK without admin, but **requires
  root on Linux** and is incompatible with macOS in modern releases.
  Last release 2022.
* **`pynput`** — cleaner cross-platform story but ~1 MB of Python +
  pyobjc on macOS for one keypress.

Win32 `RegisterHotKey` via `ctypes`:

* Zero new dependency. `ctypes` ships with Python.
* Non-elevated integrity — no admin / no UAC.
* The canonical system-wide hotkey API on Windows (Win+L uses it).
* Soft-fail story matches the spec: non-Windows → `start()` returns
  `False`, the dashboard pause button still works, and the spec
  itself says *"On a dedicated runner box, neither matters — the bot
  owns the screen"*.

The watcher runs a message loop on a **daemon thread** because
`RegisterHotKey` requires the registering thread to dispatch
`WM_HOTKEY`. Daemon means a stuck loop never blocks process exit if
teardown hits an edge case; `WM_QUIT` is the clean stop signal.

### Why Win32 `GetLastInputInfo` (and not a global keyboard / mouse hook)

The idle detector needs *system-wide* "any input" — keyboard or mouse,
in our process or somebody else's. `GetLastInputInfo` returns the
tick-count of the last system input, which is exactly the signal we
want. The alternatives (`SetWindowsHookEx`, raw input registration)
would force us to inspect every key/mouse event in the system to
update a timer — heavy-handed for a 60-second threshold check.

The reader is a static module function (`_win32_idle_seconds`) so tests
inject a fake `read_idle_seconds` callable; production picks up the
Win32 default. macOS / Linux backends can swap in later via the same
seam (v3.1 likely).

### Semantic: user-active → bot paused

The idle source pauses the scheduler when `idle_seconds < threshold` —
i.e. *the opposite* of what "idle" sounds like. The naming is awkward
but matches the dashboard's "Paused — user active" reason copy and the
spec's "auto-pause on input" language. We pay the cognitive cost once
in the docstring instead of inverting the source name and breaking the
"every pause source has a positive pause meaning" pattern.

### Why resume(manual) doesn't clear hotkey

The dashboard's Resume button POSTs `/api/control/resume`, which clears
**only** the `manual` source. If F6 is engaged or the user is actively
typing, the scheduler stays paused — visible via the lingering reason
in the status bar. Otherwise the UI would let the user "un-do" their
own F6 press from the dashboard, which surprises them when the next
cycle still pauses (hotkey is still ON). Each source has one
authoritative writer.

### Lifespan ordering: watchers between service and shutdown

```
startup:  service.start()  →  watchers[*].start()
shutdown: watchers[*].stop()  →  service.stop()
```

* **Watchers AFTER service on startup** so a watcher firing during init
  can't race with a half-constructed scheduler.
* **Watchers BEFORE service on shutdown** so a watcher can't fire a
  final pause/resume against a torn-down service.
* Watcher exceptions are swallowed at both ends — losing F6 is better
  than failing app boot or stalling teardown.

### Edge cases covered (see `tests_v3/test_web_control.py`)

| Concern | Test |
|---|---|
| Union pauses iff any source is paused | `test_multi_source_union` |
| Snapshot is an immutable copy (no leak) | `test_snapshot_is_immutable_copy` |
| Unknown source raises ValueError → 400 | `test_unknown_source_rejected` |
| Last reason wins on idempotent re-pause | `test_pause_is_idempotent_last_reason_wins` |
| Thread-safety under contention | `test_thread_safety_smoke` |
| Legacy zero-arg `pause()` = manual | `test_legacy_zero_arg_pause_uses_manual_source` |
| Predicate reads union live, not at build | `test_predicate_reads_union_live` |
| External writer (shared control) visible | `test_shared_control_state_visible_to_external_writer` |
| /api/control/pause flips manual source | `test_pause_endpoint_flips_manual_source` |
| Empty body is a valid pause | `test_pause_endpoint_accepts_empty_body` |
| Resume(manual) keeps hotkey pause | `test_resume_only_clears_manual_not_hotkey` |
| /api/status surfaces pause reasons | `test_status_endpoint_surfaces_pause_reasons` |
| 409 without service (read-only mode) | `test_endpoints_409_without_service` |
| Hotkey non-Windows soft-fail | `test_non_windows_soft_fail` |
| Hotkey unknown key soft-fail | `test_unknown_key_soft_fail` |
| Hotkey stop without start safe | `test_stop_without_start_is_safe` |
| F6 toggle source = hotkey (not manual) | `test_build_hotkey_toggle_targets_hotkey_source` |
| Idle pauses on user active | `test_pauses_when_user_recently_active` |
| Idle resumes after threshold | `test_resumes_when_user_goes_idle` |
| Idle stop releases lingering pause | `test_stop_clears_lingering_pause` |
| Idle read error doesn't kill loop | `test_read_error_does_not_kill_loop` |
| Lifespan boots + tears down watchers | `test_lifespan_starts_and_stops_watchers` |
| Watcher start error doesn't fail boot | `test_lifespan_swallows_watcher_start_errors` |

### What's NOT in this sub-phase

* **Login-on-demand UX.** `/api/sources` already shows the badge data;
  the "Log in" button + headed-browser launch is (4/M).
* **Onboarding wizard.** F6 + idle-detect config (`web.hotkey`,
  `web.idle_detect_enabled`, etc.) is hand-edit-only until (5/M) ships
  the wizard panel that asks.
* **One-click launcher.** Ships `.cmd` wrapper in (6/M).
* **macOS / Linux idle-detect.** Soft-fails today; CGEventSource +
  XScreenSaverQueryInfo backends are a v3.1 ask if a non-Windows user
  needs the feature.
* **Hotkey reconfiguration without restart.** Changing the key requires
  restarting `av3 serve` so the watcher re-registers. The (5/M)
  onboarding wizard can collect this and prompt for the restart.

### Carry-over: onboarding-collected hotkey + idle preferences

The watchers respect `settings.web.hotkey` / `idle_detect_enabled` /
`idle_threshold_s` already; (5/M) wires the onboarding wizard to
populate those into `user_config.json`. No code change needed in (3/M)
— the seam exists, it's UI-only follow-through.

---

## Phase 4 (4/M) — login-on-demand + assisted submit landed

A `HeadedBrowserLauncher` primitive + five new endpoints turn the
`AUTH_REQUIRED` source badges and `ASSISTED_PENDING` application rows
into actionable UX. The dashboard's source list grew per-row "Log in" +
"Mark logged in" buttons; the per-job page grew an assisted-submit card
that opens the apply form, confirms a human-submitted apply, or cancels
the attempt. `tests_v3/test_web_login_assist.py` (+28 tests; full v3
suite 461 green, 11 deselected by design). **Spec §6a + §8b + §11b
Phase 4 bullet 2 ("review queue + login-needed badges, application
history + outcomes") satisfied.**

### New surface

| Module / endpoint | Purpose |
|---|---|
| `av3/web/headed.py` | `HeadedBrowserLauncher.open(url)` — opens in the bot's persistent Chrome profile via injected `new_page`, falls back to `webbrowser.open` |
| `POST /api/sources/<source>/login` | Launches the captured `login_url` for an `AUTH_REQUIRED` source |
| `POST /api/sources/<source>/healthy` | Clears the AUTH_REQUIRED flag after the user signs back in |
| `POST /api/jobs/<id>/assisted/open` | Opens the apply URL for an `ASSISTED_PENDING` attempt |
| `POST /api/jobs/<id>/assisted/confirm` | Walks REVIEW → QUEUED_APPLY → APPLYING → APPLIED + marks the Application APPLIED |
| `POST /api/jobs/<id>/assisted/cancel` | Marks the Application FAILED; Job stays in REVIEW |
| `av3.sources.health.SourceHealthRecord.login_url` | New field — captured by `check_auth_wall()` from `page.url` when the wall fires |

### Why open URLs through the bot's persistent profile (not the OS default)

Login-on-demand exists because **the apply worker needs the user's
cookies in the bot's persistent Chrome profile** — that's the whole
point of `BrowserSession`'s `user_data_dir`. If we open the login URL
in the OS default browser:

* The user signs in fine — but to *their* browser's cookie jar.
* The next apply cycle still hits AUTH_REQUIRED because the bot's
  profile never saw the cookies.
* The user re-paused source loops back into "log in" forever.

By calling `BrowserSession.new_page()` from the launcher, the login
page opens as a fresh tab in the SAME persistent context the apply
worker uses — cookies land in the right jar on first sign-in.

The fallback to `webbrowser.open` exists for `--no-scheduler`
diagnostics + tests; the LaunchResult's `mode` field tells the UI
which path fired so the user understands whether cookies will reach
the bot.

### Why `resume(manual)` doesn't auto-trigger login (and login doesn't auto-mark-healthy)

The two flows are deliberately decoupled:

* **`/sources/<s>/login` opens the URL but does NOT clear the
  AUTH_REQUIRED flag.** The user might fail to sign in (wrong
  password, MFA timeout, account locked); flipping HEALTHY on launch
  would lie about the state until the next apply cycle re-detected
  the wall.
* **`/sources/<s>/healthy` is a separate confirm.** The user clicks
  "Mark logged in" once their sign-in is actually complete. This is
  the same pattern as assisted-submit-confirm: the human is the
  positive-signal source per spec §5.

The dashboard sequences these naturally — both buttons sit side-by-side
on each paused source row; the user clicks Log in → signs in → clicks
Mark logged in.

### Why /assisted/confirm walks the full state machine

The spec says **APPLIED requires positive confirmation set by the apply
worker**. An assisted submit doesn't have an apply-worker confirmation —
the human is the signal. We honor the spec's *audit shape* by walking
the same state edges the auto path would: REVIEW → QUEUED_APPLY →
APPLYING → APPLIED, in a single chained set_state inside one
`with web_state.app_conn()` block.

Two reasons not to add a `REVIEW → APPLIED` shortcut edge to the
transitions table:

1. **The transitions table is the contract.** Adding a shortcut for
   one endpoint normalizes "shortcuts" — every state would eventually
   gain one.
2. **The event spine reads the edges.** `@stage` writes one event per
   transition; the chained walk leaves a four-edge audit that mirrors
   an auto-apply. Diff tools, retention queries, and the dashboard's
   per-job history all stay uniform.

### Why a separate `assisted/cancel` (not "skip" via REVIEW UI)

When the user reviews a pre-fill and decides not to submit, the bot's
Application row needs to come out of `ASSISTED_PENDING` — otherwise
the dashboard keeps prompting "open the application" forever. The
cancel endpoint marks the Application FAILED (so the per-job history
shows the attempt + its cancellation) but **leaves the Job in
REVIEW** — the user can re-try (the apply worker will pick it up on
the next QUEUED_APPLY drain) or move it to SKIPPED via a separate
action. Job-state and Application-status are independent: the cancel
applies only to *this attempt*.

### WebState.app_conn now autocommits

The (1/M)–(3/M) handlers were read-only, so the per-request connection
ran in Python's default isolation (implicit BEGIN, no explicit COMMIT)
and writes would have been lost on close. (4/M) is the first sub-phase
where the web layer writes to app.db (the chained set_state +
set_status in assisted/confirm), so `WebState.app_conn` now uses
`isolation_level=None` — same posture as
`av3.db.engine.connect`. Statement-level autocommit is the right
granularity here: per-request batches are tiny (≤4 statements), and a
half-written batch is recoverable by the next click.

### `login_url` on the health record + `check_auth_wall` plumbing

`SourceHealthRecord.login_url` is the URL `apply_base.check_auth_wall`
captured at the moment the wall fired (typically `page.url`). The
dashboard's "Log in" button only renders when this is non-empty;
otherwise the UI shows just "Mark logged in" (and the user signs in
through whatever side channel they prefer). The field round-trips
through `health_snapshot()` and is reset to empty when
`mark_healthy()` re-creates the record — so a re-paused source picks
up its NEW login URL on the next wall detection, not a stale one
from a prior cycle.

### Edge cases covered (see `tests_v3/test_web_login_assist.py`)

| Concern | Test |
|---|---|
| Launcher with no URL returns unavailable | `test_no_url_returns_unavailable` |
| Bot-browser path when new_page provided | `test_opens_in_bot_browser_when_new_page_provided` |
| Fallback to default browser without session | `test_falls_back_to_default_browser_when_no_session` |
| `new_page()` raise → fallback fires | `test_falls_back_when_new_page_raises` |
| `page.goto()` raise → fallback fires | `test_falls_back_when_goto_raises` |
| Fallback returning False reports unavailable | `test_fallback_returning_false_reports_unavailable` |
| Login opens captured URL | `test_login_opens_captured_url` |
| Login 404 for unknown source | `test_login_404_when_source_unknown` |
| Login 409 when source HEALTHY | `test_login_409_when_source_healthy` |
| Login 422 when no URL captured | `test_login_422_when_no_login_url_captured` |
| Mark-healthy clears AUTH_REQUIRED | `test_mark_healthy_clears_paused` |
| Mark-healthy idempotent under double-click | `test_mark_healthy_is_idempotent` |
| /api/sources carries `login_url` | `test_sources_endpoint_carries_login_url` |
| Assisted/open launches apply URL | `test_open_launches_apply_url` |
| Assisted/open 404 on missing job | `test_open_404_when_job_missing` |
| Assisted/open 409 with no PENDING attempt | `test_open_409_when_no_assisted_pending` |
| Assisted/open 422 when job URL empty | `test_open_422_when_job_has_no_url` |
| Assisted/confirm walks REVIEW→APPLIED | `test_confirm_walks_state_machine_to_applied` |
| Confirm 404 on missing job | `test_confirm_404_when_job_missing` |
| Confirm 409 when job not in REVIEW | `test_confirm_409_when_job_not_in_review` |
| Confirm 409 with no PENDING attempt | `test_confirm_409_when_no_assisted_pending` |
| Cancel flips Application, keeps Job in REVIEW | `test_cancel_flips_application_keeps_job_in_review` |
| Cancel 404 on missing job | `test_cancel_404_when_job_missing` |
| Cancel 409 with no PENDING attempt | `test_cancel_409_when_no_pending` |
| Latest-pending picks most recent of multiple | `test_picks_most_recent_pending` |
| login_url round-trips through snapshot | `test_login_url_round_trips` |
| login_url clears on mark_healthy | `test_login_url_clears_on_mark_healthy` |
| login_url defaults to empty | `test_login_url_empty_when_not_provided` |

### What's NOT in this sub-phase

* **Per-source login URL configuration.** The dashboard only shows
  "Log in" when the apply driver captured a `login_url`. A static
  config registry (e.g. `"greenhouse": "https://login.greenhouse.io/"`)
  would let the user log in *before* the bot ever hits a wall — but
  most ATS apply pages don't have a stable login page (candidate
  forms are public), and v3 doesn't have a non-ATS source that needs
  pre-emptive login yet. Add when Phase 2 ships Indeed/Dice/ZipRecruiter.
* **Pausing the scheduler while the user is mid-login.** Today the
  AUTH_REQUIRED flag on the source already pauses just that source
  (the apply worker skips paused sources per spec §8b), so the user
  isn't racing the bot for the login tab. If `--no-scheduler` mode
  were the rule we'd add a launch-time pause; for now the source
  flag is the right granularity.
* **Bulk "mark all healthy".** Each source clears individually. If a
  hard pause cascades across multiple sources (e.g. captcha
  detection during a discovery sweep), the user clicks each. Worth
  revisiting if (Phase 2) source breadth makes this tedious.
* **`SKIPPED` transition from cancelled assisted attempt.** Cancel
  leaves the job in REVIEW. If the user wants to drop the job
  entirely, they need another action — REVIEW → SKIPPED isn't wired
  from the dashboard yet. (5/M) onboarding wizard will likely add
  the bulk-skip controls for this.
* **Reload semantics if the user clicks Log in while a prior
  apply tab is open.** `BrowserSession.new_page()` opens a fresh tab;
  the old tab stays. Fine for ATS sites, but boards with single-tab
  semantics (Dice's modal apply flow) might surprise the user. Note
  for Phase 2 when boards land.

### Carry-over: onboarding-time hotkey + login URL prefs

The onboarding wizard (5/M) is the natural place to set:

* `web.hotkey` / `web.idle_detect_enabled` (already collected at the
  scheduler-config step).
* A future "do you want the bot to auto-launch login URLs in its
  profile?" toggle — for users who'd rather sign in via a password
  manager popup in their own browser. The launcher already supports
  the fallback; the wizard just adds the user-config knob.
