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

## Direction 2 (Phase A) — Assisted-queue panel landed (2026-06-16, commit 46f88d6)

Turned the flat REVIEW list into a grouped, actionable to-do list on the dashboard ("N filled,
waiting for you to submit / log in / decide") — the realistic Greenhouse outcome. Evolved the
existing Alpine app (no framework rewrite, per future-directions.md Direction 2).

- **New endpoints** (`routes.py`): `GET /api/review-queue` (REVIEW jobs enriched with
  `needed_action`/`reason`/`assisted_application_id`/`source_paused`/`score_total`),
  `POST /api/jobs/{id}/mark-applied` (reuses `manual_apply.mark_manually_applied` — note: an already-
  APPLIED job returns **200 `status="already"`**, not an error, so the 409 case is a genuinely
  disallowed state like SCORED), `POST /api/jobs/{id}/skip` (REVIEW→SKIPPED). Open/confirm/cancel/login
  REUSE the existing (4/M) assisted endpoints.
- **The "reason" is INFERRED, not stored** (`views.review_reason`, pure + unit-tested): latest
  Application `ASSISTED_PENDING` → "submit"; source `is_paused` → "login"; else (no app = optimize-gate
  REVIEW, or a FAILED app) → "decide". No schema change. The security-code-vs-plain-submit distinction
  is NOT derivable from app.db alone (the `CAPTCHA_CHALLENGE` signal lives only in the driver outcome /
  events) → collapsed into "submit" for now; deferred to A3 (ties to the Direction 3 question).
- Honesty preserved: `mark-applied`/`confirm` are human attestations (same as `av3 applied`), never a
  fabricated bot confirmation; assisted finish goes through the existing headed-browser launcher.

### ⚠️ DURABLE GOTCHA — Alpine `:disabled` (and any boolean attr) bound to `undefined` does NOT clear

`:disabled="reviewBusy[j.id]"` with `reviewBusy` a `{}` dict (so the value is `undefined` when the row
isn't busy) rendered EVERY action button **permanently `disabled`**. Alpine only *removes* a boolean
attribute when the bound value is explicitly `false` — `undefined`/`null` leave it set. Proven live
(playwright): setting the keys to explicit `false` → enabled; resetting to `{}` (undefined) → disabled
again. **Fix: coerce dict-indexed boolean bindings to a real boolean — `:disabled="!!reviewBusy[j.id]"`.**
The same latent bug existed in the pre-existing Sources-panel bindings (`!!sourceBusy[s.source]`), now
fixed too. **Rule for this codebase: any `:disabled="<dict>[key]"` MUST be `!!`-coerced.** TestClient
tests can't catch this (it's render-time JS) — only the playwright browser-smoke did.
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

---

## Phase 4 (5/M) — onboarding wizard landed

Seven-step guided-but-skippable wizard at `/onboarding`: contact →
work history → skills → work-auth → targeting → telemetry opt-in →
control prefs. Power users can hit the dashboard's "Skip to dashboard"
link at any step. The dashboard renders an onboarding banner until
`is_complete` flips. `tests_v3/test_web_onboarding.py` (+28 tests;
full v3 suite 489 green, 11 deselected by design). **Spec §11a +
§11b Phase 4 bullet 3 ("guided-but-skippable onboarding incl.
fact-bank review") satisfied.**

### New surface

| Module / endpoint | Purpose |
|---|---|
| `av3/web/onboarding.py` | `OnboardingStatus` (per-step gates), atomic `save_fact_bank` / `save_user_config`, merge helpers for contact / work-history / education / skills / work-auth |
| `av3/config/settings.py` `TargetingConfig` | Job-targeting filters (titles, locations, remote/onsite, salary floor, seniority) — spec §6c |
| `GET /api/onboarding/state` | Status snapshot — which steps are done + current values for hydration |
| `POST /api/onboarding/contact` | Save name + email + phone + location |
| `POST /api/onboarding/work-history` | Wholesale-replace work entries |
| `POST /api/onboarding/education` | Wholesale-replace education entries |
| `POST /api/onboarding/skills` | Replace skills (case-insensitive dedupe + empty drop) |
| `POST /api/onboarding/work-auth` | Save work_authorization + tri-state requires_sponsorship |
| `POST /api/onboarding/targeting` | Partial-merge targeting fields into user_config |
| `POST /api/onboarding/telemetry` | Save the opt-in decision (default OFF) |
| `POST /api/onboarding/web-prefs` | Save F6 / idle preferences |
| `GET /onboarding` | The wizard HTML |
| Dashboard banner | "Finish onboarding to start the scheduler" — shows iff `!is_complete` |

### Why step-wise endpoints, not one big PUT

The wizard must survive a tab close in the middle. Each "Save &
continue" button posts to its endpoint and waits for the new status
snapshot before advancing — so the user can refresh, close the laptop,
come back tomorrow, and the wizard re-opens at the first incomplete
step. One big PUT-on-finish would either:

* lose every field if the user closed before the final submit, or
* require client-side localStorage shadowing (which then diverges from
  the actual fact bank, generating its own bug class).

Each step's endpoint is idempotent; re-posting the same step just
overwrites. The wizard tracks `step` client-side because *that's* UI
state, not domain state — the server doesn't care which page the user
is on.

### Why there's no separate "wizard state" table

`is_complete` is derived from the same files the rest of v3 already
reads: `data/profile/master.json` (fact bank) + `data/user_config.json`
(settings). That means:

* The CLI's `serve_cmd` scheduler-ready gate (which checks the fact
  bank exists) and the dashboard's "finish onboarding" banner are
  **answering the same question** with the same query — they can't
  drift.
* Power users who hand-edit `master.json` (the spec explicitly
  supports this — "user-reviewable doc") don't get a stale "you
  haven't onboarded yet" banner from a separate state table.
* Resetting onboarding is just `rm data/profile/master.json
  data/user_config.json` — no "ONBOARDING_DONE" flag to forget about.

The wizard is a thin façade over file ops; the domain owns the truth.

### Why we DON'T LLM-parse a pasted résumé in v3.0

The spec lists "upload résumé → review the extracted fact bank" as
the ideal flow. Skipping the auto-extract for v3.0 is deliberate:

1. **Variant-merge is research-heavy.** Spec §6b ("Fact-bank merge
   conflicts: keep all variants, user picks canonical") requires a
   merge UX that's its own design problem — not a freebie that drops
   out of "parse the PDF."
2. **Fabrication risk.** An LLM extraction that hallucinates a
   credential into the bank breaks the load-bearing fabrication
   invariant *upstream of the guard*. The guard checks generation
   against the bank; if the bank itself is wrong, the guard can't
   save us.
3. **Eval gap.** v3.0 doesn't yet have a golden-set eval for résumé
   extraction. Adding extraction without one is the v2 mistake.

v3.0 collects the structured fields manually; the wizard supports
pasting raw text into the work-history textarea as a copy-paste
convenience. v3.1 lands upload → LLM extract → user-review-as-merge
once the extraction prompt has its own eval gate.

### Why work_authorization + requires_sponsorship are tri-state

Spec §6b is explicit: *"work-authorization + sponsorship status
captured explicitly in onboarding — no silent default"*. v2's "default
to Yes because the user is probably American" produced wrong answers
on real applications. v3's `requires_sponsorship` is `True | False |
None`:

* `False` → user explicitly answered "no" → answer resolver fills
  "No" on the form.
* `True` → user explicitly answered "yes" → resolver fills "Yes".
* `None` → user **left the field unanswered** → resolver **bails to
  REVIEW** on that question rather than guessing.

The merge helper preserves the `None` (doesn't coerce to `False`)
and the radio widget exposes a "Skip / unanswered" option that POSTs
`null`. The completeness gate counts the question as answered if
*either* `work_authorization` is non-empty *or*
`requires_sponsorship` is not None — so a citizen who skips
sponsorship still passes the gate, and a non-citizen who answers
sponsorship can leave work-auth blank.

### Atomic writes via temp-and-replace

`save_fact_bank` and `save_user_config` both write to a `.tmp` sibling
then `Path.replace()` — POSIX-atomic on the same filesystem so a
crash mid-write can't leave a half-written file that breaks the next
load. Important because:

* The fact bank is load-bearing — every apply path reads from it. A
  corrupt load is a hard service failure.
* The wizard's UX gives the user a "Save" button that implies
  durability. A non-atomic write makes that an implicit lie.

`load_user_config` quarantines an unreadable file to
`user_config.json.broken` and returns `{}` so the wizard recovers
without surfacing a JSON parse error — the original file is
preserved for forensics, but the user isn't stuck unable to onboard.

### Edge cases covered (see `tests_v3/test_web_onboarding.py`)

| Concern | Test |
|---|---|
| Fact bank round-trips through save/load | `test_save_and_load_fact_bank_round_trip` |
| Missing fact bank returns empty (not raise) | `test_load_fact_bank_returns_empty_when_missing` |
| user_config write leaves no .tmp on success | `test_save_user_config_is_atomic` |
| Corrupt user_config quarantines + returns {} | `test_corrupt_user_config_is_quarantined` |
| Contact merge replaces existing fields | `test_merge_contact_replaces_fields` |
| Work-history merge is wholesale, not partial | `test_merge_work_history_is_wholesale_replace` |
| Skills dedupe case-insensitively + drop empties | `test_merge_skills_dedupes_case_insensitively` |
| work-auth tri-state preserves explicit null | `test_merge_work_auth_no_silent_default` |
| work-auth accepts explicit False | `test_merge_work_auth_accepts_false` |
| Empty state has no completed gates | `test_empty_state_is_not_complete` |
| Full population flips is_complete | `test_complete_when_all_gates_pass` |
| Telemetry-decision = False still counts | `test_telemetry_decision_counts_when_disabled` |
| work-auth gate passes on sponsorship alone | `test_work_auth_gate_passes_on_sponsorship_alone` |
| /contact persists + returns status | `test_post_persists_and_returns_status` |
| /contact rejects non-JSON-object body | `test_post_400_when_body_not_json_object` |
| /work-history wholesale-replace | `test_replace_wholesale` |
| /work-history 400 on non-list payload | `test_400_when_payload_not_list` |
| /skills dedupe-and-save | `test_dedupe_and_save` |
| /work-auth persists explicit null | `test_explicit_null_sponsorship_persists` |
| /work-auth no silent default on empty | `test_no_default_when_unset` |
| /targeting persists into user_config | `test_persists_into_user_config` |
| /targeting partial update preserves keys | `test_partial_update_preserves_other_keys` |
| /telemetry disabled decision counts | `test_disabled_decision_counts` |
| /telemetry defaults enabled=False when omitted | `test_default_enabled_false_when_omitted` |
| /web-prefs persists hotkey + idle threshold | `test_persists_hotkey_and_idle` |
| /state empty install returns hydration defaults | `test_empty_install` |
| /onboarding HTML renders | `test_renders` |
| Full step-by-step flow flips is_complete | `test_step_by_step` |

### What's NOT in this sub-phase

* **LLM-extract from uploaded résumé.** v3.0 collects fields
  manually; v3.1 lands upload + extract + variant-merge once the
  extraction prompt has its own eval. The status field
  `has_resume` exists so a future upload form can light it up.
* **NL-intent targeting** ("remote senior data analyst, $120k+,
  no clearance" → LLM parses into structured filters). v3.0 ships
  the structured form straight; the underlying TargetingConfig
  shape is identical so the LLM-parse step is a UI nicety that
  drops in later.
* **Live config reload.** Changing `web.hotkey` / `idle_*` via the
  wizard writes user_config but the running uvicorn process keeps
  the old values until restart. The wizard surfaces a "applies
  after restart" note next to those fields. Hot-reloading the
  watchers is non-trivial (the Win32 message loop is on a daemon
  thread we'd need to tear down + recreate); not worth the
  complexity for a setting most users tweak once.
* **Bulk skill / work-history paste-and-parse.** The textareas
  accept raw text but each entry is structured. A pasted résumé
  text → structured-entries parser would help here; deferred to
  the v3.1 upload-and-extract work above.
* **Onboarding reset from the UI.** The user has to
  `rm master.json user_config.json` if they want to redo the
  wizard. A dashboard "Reset onboarding" button would be helpful
  but is destructive enough that requiring the file delete is the
  safer v3.0 default.

### Carry-over: targeting config not yet read by the discovery producer

`TargetingConfig` lives on `Settings` and persists from the wizard,
but the existing Greenhouse/Lever/Ashby discovery sources still use
their own seed-list config (per spec Phase 1 — sources were "wire
their own discovery"). Phase 2's discovery producer will read
`settings.targeting` directly. Until then the wizard's
titles/locations are captured but inert — visible in the status
snapshot, ready for Phase 2 to consume. Noted so the next phase
doesn't re-invent the schema.

---

## Phase 4 (6/M) — one-click launcher landed

`av3 launch` CLI command + `scripts/av3-launcher.{cmd,sh}` wrappers
close out Phase 4's distribution UX. `tests_v3/test_cli_launch.py`
(+6 tests; full v3 suite 495 green, 11 deselected by design). **Spec
§11a "one-click launcher starts the worker+server and opens the
dashboard tab" satisfied; the bundled-installer + auto-update feed
(Phase 5) consumes this entry point.**

### New surface

| Module / endpoint | Purpose |
|---|---|
| `av3.cli.main.launch_cmd` | `av3 launch` — spawns `av3 serve` in a child process, port-probes for readiness, opens the default browser to the dashboard |
| `av3.cli.main._wait_for_port` | TCP-connect poll until the port accepts or timeout — never raises |
| `scripts/av3-launcher.cmd` | Windows double-click target — finds `.venv`, calls `av3 launch`, keeps window open for logs |
| `scripts/av3-launcher.sh` | POSIX counterpart of the .cmd wrapper |

### Why a child process + probe + browser-open (vs. opening the browser before starting uvicorn)

Three time-sensitive concerns combine on launch:

1. **Browser-open is a one-shot.** `webbrowser.open` fires whenever
   it's called; we have to call it AFTER the server can answer or
   the user sees "couldn't connect to localhost".
2. **uvicorn is blocking.** Once `uvicorn.run()` starts, the calling
   thread loops the event loop forever. Anything that needs to run
   AFTER uvicorn starts but BEFORE the user sees the page (i.e.
   the port-probe + browser-open) has to happen in a separate
   thread or process.
3. **Ctrl-C semantics matter.** A user hitting Ctrl-C in the
   launcher window should cleanly stop the server, not orphan it.

A child process gives us all three for free:

* The parent (launch_cmd) port-probes + opens the browser between
  spawn and the child's first request.
* uvicorn's Ctrl-C handler in the child runs unchanged, tearing
  down the lifespan (scheduler + watchers per (3/M)) cleanly.
* The parent's `child.wait()` blocks on the child's lifetime; a
  SIGINT to the parent triggers `child.terminate()` so the child
  gets the signal it expects.

An in-process threading approach would have required teaching
uvicorn about the "wait then open browser" flow, which the
upstream API doesn't expose cleanly.

### Why the launcher uses `python -m av3.cli.main`, not the installed `av3` script

Two reasons:

1. **Repo-checkout robustness.** A fresh `git clone` doesn't have
   the console script available until `pip install -e .`. The
   module path works as soon as the source is on PYTHONPATH (which
   running `python -m` from the repo root guarantees).
2. **Interpreter fidelity.** `sys.executable -m av3.cli.main`
   guarantees the child runs in the SAME interpreter (and same
   venv) as the parent. The installed `av3` script could be a
   stale binstub from a previous venv if multiple are active.

The `.cmd` / `.sh` wrappers do find the venv's Python explicitly
before calling `python -m`; this gives non-technical users the
"double-click and go" UX while keeping power users on the
straight `av3 launch` invocation.

### Why `--host 0.0.0.0` rewrites the probe + browser URL to 127.0.0.1

When the user binds the server to `0.0.0.0` (spec §3 LAN-access
mode), the launcher must still open the browser at `127.0.0.1`:

* `http://0.0.0.0:port` either fails outright (browsers reject
  bind-any as a target) or routes through the LAN gateway, which
  fails on most home networks.
* The user IS on the same machine; their browser sees the same
  server fine via localhost.
* Other LAN clients still reach the server at the runner box's
  real IP — they just don't get the auto-open. Same UX as today's
  v2 dashboard.

The probe target is rewritten in lockstep so the port-readiness
check actually verifies what the browser will hit, not what
uvicorn binds to.

### Why the probe times out + opens the browser anyway

If the server fails to start (e.g. port already in use), the probe
runs out of time. We open the browser anyway because:

* The user sees a clean "couldn't connect" error in the tab — they
  understand what failed and can read the launcher window for the
  uvicorn error.
* The alternative — refusing to open the browser — leaves the user
  staring at the launcher window with no signal that something
  went wrong vs. "the launcher is just slow."

The probe is best-effort UX, not a correctness check.

### Edge cases covered (see `tests_v3/test_cli_launch.py`)

| Concern | Test |
|---|---|
| Port probe returns False on closed port | `test_returns_false_when_nothing_listening` |
| Spawns `python -m av3.cli.main serve` with host+port | `test_spawns_serve_with_default_host_port` |
| `--no-browser` skips `webbrowser.open` | `test_no_browser_skips_open` |
| `--host 0.0.0.0` rewrites probe + URL to 127.0.0.1 | `test_host_0_0_0_0_rewrites_probe_to_localhost` |
| Probe timeout still opens browser (best-effort) | `test_probe_timeout_still_opens_browser` |
| Child non-zero exit propagates from launch_cmd | `test_child_nonzero_exit_propagates` |

### What's NOT in this sub-phase

* **Bundled installer (PyInstaller / py2app / msix).** Spec §11a
  lists this as a Phase 5 deliverable. The launcher exists so the
  installer's shortcut has a clean entry point — `av3 launch` is
  what the installed `.exe` calls.
* **Auto-update feed.** Also Phase 5 (release-feed polling +
  prompt-to-update). The launcher doesn't check for updates; a
  Phase-5 hook will add that to launch_cmd's startup path.
* **Headless runner mode.** `--no-browser` exists for autostarting
  on a runner box, but the systemd/Windows-service integration is
  Phase 5 distribution work — the launcher just provides the
  primitive that wraps cleanly into either.
* **Crash recovery / auto-restart.** If `av3 serve` crashes, the
  launcher exits with the child's return code. A supervisor (NSSM
  on Windows, systemd elsewhere) handles re-launch; baking that
  into the launcher would re-implement what the OS does better.

### Carry-over: Phase 5 distribution hooks

The launcher's seams are intentionally narrow so Phase 5 can wrap
it without touching `serve_cmd`:

* `av3 launch --check-updates` (future flag) will gate the spawn
  on the auto-update feed result.
* A `--quiet` flag will hide the launcher window after the browser
  opens (Windows-specific via `pythonw.exe`).
* The bundled installer's shortcut just runs `av3-launcher.cmd`;
  changing the entry point breaks nothing.

---

## Onboarding UX hardening (2026-06-01) — work-auth never auto-skipped + clearer sponsorship wording

Found during the **first real onboarding session** (building the
primary user's fact bank from three résumés). Two defects in the wizard,
both fixed in commit **26d7a72** (frontend + template only; no Python
change; 36 onboarding-related tests still green).

### Bug 1 — `load()` auto-jump silently skips a "complete-looking" step

`onboarding.js load()` calls `_firstIncomplete()` and lands the user
on the first step whose status gate is false. **`has_work_auth` is an
OR gate** (`onboarding.py`): it's True if `work_authorization` is
non-empty **OR** `requires_sponsorship is not None`. So a value
pre-seeded into `master.json` (here: `work_authorization` was set
before the wizard ran) satisfied the gate, and `_firstIncomplete()`
jumped the user **clean past Step 4 to Targeting** — they never saw
the sponsorship radios and never confirmed the answer. Result:
`requires_sponsorship: true` sat alongside `work_authorization:
"US Citizen"` (a contradiction that auto-rejects at most US employers).

**Fix:** clamp the auto-jump so it can never land *after* `work-auth`
(`order.indexOf(step) > order.indexOf('work-auth')` → force
`work-auth`). The legally-sensitive step is now always shown for
explicit confirmation at least once. New users still walk steps 1→N
naturally (they land on `contact`); only the "jumped past it" case is
corrected. Trade-off: a fully-onboarded returning user now lands on
`work-auth` rather than `done` — acceptable, and the step-nav lets
them click anywhere.

> **Deeper smell (NOT fixed, candidate for later):** the OR gate means
> a value can't tell "user explicitly chose" from "pre-seeded /
> never confirmed" — `requires_sponsorship` is tri-state where `None`
> is BOTH "skipped" and "never set". A real confirmed-this-session
> flag would be the principled fix; the navigation clamp is the
> pragmatic one.

### Bug 2 — ambiguous sponsorship wording

Legend was *"Does this role require sponsorship?"* — but there is no
specific role during onboarding, and "this role" reads as "does the
employer offer sponsorship". Reworded to the standard, region-neutral
application phrasing: *"Do you require visa sponsorship for employment
(now or in the future)?"*. (Binding itself was fine — Alpine `:value`
x-bind preserves the boolean/null type; no string-coercion bug.)

### ⚠️ BLOCKER for the first dry run — discovery producer is UNBUILT (confirmed 2026-06-01)

Ran this down end-to-end. **There is no wired discovery producer.** The
pieces exist but nothing connects them:

* Source adapters have `.discover()` (`greenhouse.discover(token)`,
  `lever.discover(site)`, `ashby.discover(slug)`, `jobspy.discover(query)`).
* The repo has the write path `JobRepository.upsert_discovered()`
  (`db/repositories.py:72`).
* **But `upsert_discovered` is called ONLY in `tests/test_db.py`** — never
  in production. And `.discover()` is called only in the apply drivers
  (custom-question discovery) + survey, never to populate the jobs table.
* `av3 --help` has **no `discover` verb**. The scheduler explicitly
  excludes discovery ("Discovery still lives in the source adapters and
  runs separately — CLI / cron / Phase 4 dashboard button"), but that
  CLI/button **does not exist** (`routes.py` has no discover endpoint).

Net: the staged pipeline (filter→score→optimize→apply) can drain jobs,
but **nothing feeds DISCOVERED rows in**. So `av3 run` finds nothing;
the per-stage workers have an empty queue. This is consistent with the
memory note "v3 has never applied to a live job end-to-end."

**Consequence for targeting:** the wizard's `titles`/`locations` are
captured but currently inert (no producer reads `settings.targeting`).
ATS discovery is **by company board token**, not by title search — titles
drive the downstream filter/score, not the ATS query. So discovery also
needs a **seed list of company board tokens** (see
`research/ats-discovery-seeding.md`), not just the target titles.

**Tomorrow's actual first task** (was mis-scoped as "run the dry
pipeline"): **build a discovery bootstrap** — either a real `av3 discover`
command or a throwaway seeding script — that (1) takes a few real
Greenhouse/Lever/Ashby company tokens, (2) calls `source.discover()`,
(3) writes via `job_repo.upsert_discovered()`. THEN the dry
filter→score→optimize→apply chain can be exercised. Decide build-vs-script
with the user first (a real `av3 discover` verb is the right long-term
fix; a script is faster to unblock testing).

## Phase 4 retrospective (six sub-phases, 2026-05-29)

The vertical slice strategy worked: each sub-phase added one
demonstrable user-facing change with its own decision rationale.
Carrying load-bearing decisions through as KB doc sections meant the
next sub-phase didn't have to re-derive context.

**Sub-phase landings:**

| # | Title | Tests added | Commit |
|---|---|---|---|
| (1/M) | Web skeleton + worker service | 16 | `13da1ab` |
| (2/M) | Dashboard UI + SSE event stream | 15 | `976d414` |
| (3/M) | F6 hotkey + idle-detect + ControlState | 36 | `b331315` |
| (4/M) | Login-on-demand + assisted submit | 28 | `6f3fd38` |
| (5/M) | Onboarding wizard | 28 | `2602172` |
| (6/M) | One-click launcher | 6 | this commit |

**98 new tests across Phase 4** (`397 → 495` v3 suite total). The
6-test (6/M) is smallest because the launcher is a thin orchestrator
over already-tested code; the underlying scheduler + watchers were
covered in (1/M)–(3/M).

**Major architectural decisions worth re-reading before Phase 5:**

1. **`SchedulerService` factory** (1/M) — async factory + teardown
   closes lifecycle around BrowserSession cleanly. Phase 5
   observability adds a relay client; the factory pattern absorbs
   it without re-shaping.
2. **`ControlState` OR-union** (3/M) — three pause sources behind
   one lock. Phase 5's auto-update check might add a fourth
   ("updating") source; the union absorbs it.
3. **HeadedBrowserLauncher with bot/fallback modes** (4/M) — bound
   to BrowserSession's persistent profile when available. Phase 5's
   distribution tweaks DON'T need to change this — installed
   wheels reach the same `new_page` callable.
4. **Onboarding state = file derivation** (5/M) — no separate
   `wizard_state` table. Phase 5 doesn't need a migration because
   there's nothing to migrate.
5. **Launcher = child process + port probe + browser open** (6/M)
   — leaves uvicorn's lifespan unchanged, supports Ctrl-C cleanly.

**Carry-overs into Phase 5:**

* TargetingConfig is captured but inert (Phase 2 consumes it).
* LLM résumé-extract → v3.1 (needs its own eval gate first).
* NL-intent targeting → v3.1 (UI nicety over existing config).
* Live config reload → not planned (restart UX is good enough;
  hot-reload would re-spawn the Win32 watcher thread, not worth it).
* Auto-update feed integration → wraps `launch_cmd` (seam exists).
* Headless / runner-box service integration → wraps `av3 launch
  --no-browser` (no code change needed).
* `assisted/cancel → SKIPPED` shortcut from the dashboard — leave
  the job in REVIEW; add a separate "Skip job" action in Phase 5
  analytics if needed.
* macOS / Linux idle-detect backends → v3.1 (Win32 covers the
  primary user base; soft-fail is the spec posture for the rest).

---

## Direction 2 (Phase B) — apply-outcomes surface landed (2026-06-20)

The dashboard's "what happened after I applied" surface — the consumer of the Direction 4
email-outcome loop (this IS Direction 4 Phase D). Added a read-only **Outcomes card** + a
per-row **Outcome column** on the history table, both sourced from the SAME feed
`av3 analytics` renders (`OutcomeRepo.applied_with_outcomes()` → `auto_applier.analytics`).
Evolves the existing Alpine app (no framework rewrite, per future-directions Direction 2).
Browser-verified live against the production data dir (29 APPLIED jobs, 18 real email
outcomes). Full suite **1275 passed / 13 deselected**.

### New surface

| Module / endpoint | Purpose |
|---|---|
| `GET /api/outcomes` | `{summary, funnel, by_source[top8], by_job}` — the whole card + column in one read |
| `analytics.furthest_outcomes(feed)` | pure `{job_id: kind.value \| None}` (reuses `_furthest_per_job`) — drives the history Outcome column |
| `analytics.OutcomeFunnel` + `compute_funnel(feed)` | pure cumulative funnel (applied / responded / interviewed / offered + rejected / ghosted / awaiting) |
| dashboard.html Outcomes card | funnel bars + terminal/pending pills + a By-source mini table; `x-show="hasOutcomeData()"` hides it pre-apply |
| history table Outcome column | per-row pill via `outcomeFor(jobId)` (absent → `—`, `"awaiting"` → Awaiting, kind → coloured pill) |

### Load-bearing design decisions

- **Reuse the analytics read-model verbatim — do NOT re-aggregate in the web layer.** The CLI
  (`av3 analytics`) and the dashboard now answer "how are applications converting?" from one
  pure function set, so they can't drift. The endpoint is a thin DTO over
  `compute_conversion_report` + the two new helpers.
- **The funnel is CUMULATIVE and `responded == total_converted`.** Each APPLIED job is counted
  once by its furthest stage; an OFFER counts in responded+interviewed+offered so the bars read
  as a shrinking ladder. A regression test pins `compute_funnel(feed).responded ==
  compute_conversion_report(feed).total_converted` so the two read-models never disagree.
- **"Awaiting" ≠ "Ghosted" (honesty).** `furthest_outcomes` returns `None` for an applied-but-
  silent job; the endpoint maps `None → "awaiting"`. The card shows Awaiting separately from a
  *recorded* GHOST. Conversion-rate denominators are unchanged (`compute_conversion_report`
  still folds silent jobs into the honest non-converted denominator) — the distinction is
  display-only. The rate is labelled **"positive reply rate"**, never "success".
- **⚠️ Pre-existing ladder quirk surfaced, NOT changed: `RESPONSE` (rank 2) outranks `REJECTION`
  (rank 1).** On real data, 7 jobs have BOTH an "application received" RESPONSE and a later
  REJECTION → furthest = RESPONSE → they show as "Responded" + count as converted. That's why
  live `outcome_counts` is `{response:9, rejection:4}` (not 9/9) and the funnel shows responded=9
  / rejected=4. This is the existing `OutcomeKind.rank` ordering (state.py) + existing
  `compute_conversion_report` behaviour — the new code faithfully mirrors the CLI; it is **out of
  scope to "fix"** here. If a terminal rejection should ever dominate an earlier ack, that's an
  `OutcomeKind.rank`/§8e modelling change (affects CLI ranking too), to design separately.
- **No new egress, no honesty erosion.** Read-only; the feed is APPLIED-only; an outcome never
  implies APPLIED (APPLIED still requires a positive submit confirmation / human attestation).
  `mark-applied`/`assisted/confirm` are untouched.
- **Alpine binding safety.** None of the new bindings is the `:disabled="<dict>[key]"` →
  undefined trap (the Phase A durable gotcha) — the column uses `x-if`/`x-text`, the bars use
  string `:class`/`:style`. `outcomeFor()` uses `hasOwnProperty` so an applied-but-`null` job is
  distinguishable from a non-applied (absent) one. Browser-smoke confirmed: card + column render,
  correct pills (Awaiting / Responded / Rejected, `—` for the non-APPLIED UNCONFIRMED row),
  **0 console errors**.

### Tests

- `tests/test_analytics.py` — `compute_funnel` cumulative + empty + responded==converted parity,
  `furthest_outcomes` furthest-wins + silent-None (pure, reuse the `_feed_row` builder).
- `tests/test_web_dashboard.py::TestOutcomesEndpoint` — empty zeroed (not error), summary+funnel+
  by_job (interview job + silent job), by_source for an offer. Plus the dashboard-render test now
  asserts the card + `hasOutcomeData()` + `outcomeFor(` are wired.

### What's NOT in this slice (future)

- **No interview/offer in real data yet** — funnel handles them (tested), just zero live.
- **Funnel is furthest-stage, not time-series.** No "applied this week vs last" trend; that's a
  later analytics slice if wanted.
- **By-source only** on the card (titles/score-bands exist in the report + CLI). Added if the
  by-source table proves too coarse.
- **The RESPONSE>REJECTION ladder question** (above) — left as a documented modelling decision,
  not a UI bug.

---

## Direction 2 (Phase C) — goals/targeting view landed (2026-06-20)

The dashboard's "what am I targeting" cockpit surface — the consumer of what the Direction 1
onboarding journey produces (`TargetingConfig`: the structured filters + the ATS board slugs
discovery sweeps). A **read view by default + an inline editor** on a new Goals card, writing
through the **single existing targeting writer** (`POST /api/onboarding/targeting`, now extended).
Evolves the Alpine app (no framework rewrite, per future-directions Direction 2). Full suite
**1283 passed / 13 deselected** (+8). Browser-verified live against a THROWAWAY temp data dir
(never the prod targeting): read renders, edit round-trips with sanitization, 0 console errors.

### New surface

| Module / endpoint | Purpose |
|---|---|
| `GET /api/targeting` | Effective `TargetingConfig` DTO: 7 structured fields + `boards{greenhouse,lever,ashby}` + `board_count` + `using_default_boards`. Reads the FILE (`load_user_config`) overlaid on defaults, never `settings`. |
| `POST /api/onboarding/targeting` (extended) | Now ALSO accepts `greenhouse_boards`/`lever_boards`/`ashby_boards`, each sanitized via `_clean_slug_list`. Purely additive → the wizard (never sends them) is unchanged. Still the SINGLE targeting writer. |
| dashboard.html Goals card | Read view (chips + counts + starter-set hint + "applies after restart" note + "Guided setup" link to the wizard) / edit view (textareas split on newline/comma; checkboxes; number; seniority select; three board textareas). |
| `app.js` dashboard() | `targeting` state + `goalsEditing/goalsDraft/goalsBusy/goalsNote`; `/api/targeting` in `refreshAll` (poll-clobber guarded); `goalsBoardGroups/startGoalsEdit/cancelGoalsEdit/saveGoals` + `_parseList`. |

### Load-bearing design decisions

- **Read the FILE, not `web_state.settings`.** `settings.targeting` is frozen at scheduler-build
  (startup) — the discovery/seed workers read it there (`discover_worker.py:153,293`,
  `seed_worker.py:171,210`), so an edit made this session wouldn't show if we read `settings`.
  `GET /api/targeting` reads `load_user_config` overlaid on `TargetingConfig` defaults via
  `model_validate`, so the card reflects saves immediately and shows what discovery WILL sweep on
  the next worker (re)start. The "Changes apply the next time the worker restarts" note states this
  honestly (same restart property the wizard + seed-boards probe already have — discovery doesn't
  hot-reload config).
- **Reuse the single writer; extend, don't fork.** Mirrors the Phase B "reuse the analytics
  read-model verbatim" discipline. Boards ARE part of `TargetingConfig`, so accepting the three
  board keys on `/onboarding/targeting` keeps ONE writer for wizard + dashboard (can't drift); a
  payload without a key never blanks it (field-by-field merge), so structured-only and board-only
  edits each leave the other half intact (tested both directions).
- **Board sanitization preserves case (`_clean_slug_list`).** Trim → drop empties → dedupe by
  EXACT value, order-preserved. Exact (not case-folded) is deliberate: **Ashby slugs are
  case-sensitive** ("Linear", "Notion", "OpenAI") so lowercasing would break the confirm-probe;
  `"Linear"` and `"linear"` are kept as distinct (tested). A non-list value → `[]` (clears, never
  corrupts) so a malformed submit can't write a string the next `load_settings()` would reject.
- **Never 500 the card.** A hand-edited/invalid saved `targeting` block (e.g. `salary_floor:
  "lots"`) falls back to pure `TargetingConfig()` defaults rather than raising — the dashboard's
  read must be robust to a power-user's hand edit of `master`/`user_config`.
- **Edit-mode poll guard.** The 5s `refreshAll` skips overwriting `this.targeting` while
  `goalsEditing` is true (the draft owns the view); `saveGoals` flips `goalsEditing=false`
  *before* `refreshAll()` so the post-save refresh isn't skipped. Verified live: edit, save,
  the read view repaints with the server's canonical values.
- **Alpine binding safety.** No `:disabled="<dict>[key]"` undefined-trap here (the Phase A durable
  gotcha) — `goalsBusy` is a plain bool, lists use `x-for`, flags use `x-show`. Browser-smoke
  confirmed card + editor render, sanitization round-trips, 0 console errors.

### Tests

- `tests/test_web_dashboard.py::TestTargetingEndpoint` (8): defaults when nothing saved
  (`using_default_boards` true, starter set counted), reflects saved overrides + `board_count`,
  malformed-config fallback (no 500), board sanitize+persist+roundtrip, Ashby case-preserving
  dedupe, structured-only doesn't blank boards & board-only doesn't blank structured, non-list
  board value clears. Plus the dashboard-render test now asserts the card + `startGoalsEdit`/
  `saveGoals`/`goalsBoardGroups` are wired.

### What's NOT in this slice (future)

- **No live config hot-reload.** Edits apply on the next worker restart (stated in the card) —
  same as the wizard; the discovery/seed workers read `settings.targeting` captured at startup.
  Hot-reloading would mean re-reading settings per cycle (a broader change), deferred.
- **No in-card "find companies" probe.** The seed-boards background probe lives in the onboarding
  wizard (linked as "Guided setup"); the dashboard card is hand-edit + the wizard for the guided/
  conversational + probe flows. Porting the probe into the card is a later nicety if wanted.
- **No goal-elicitation chat on the dashboard** — that's the wizard's job (Direction 1 Phase B);
  the card surfaces/edits the structured result.

---

## MVP first-run hardening — onboarding validation, email-connect step, done copy (2026-06-20)

Part of the four-area MVP pass (full analysis: `research/mvp-readiness.md`). The web-layer pieces:

### Guided email connect — wizard step 7 + `POST /api/onboarding/inbox` (Direction 4 Phase D)

The last Phase D gap: turning the inbox loop on used to mean hand-editing `user_config.json` +
`.env`. Now there's a guided **"Connect email (optional)"** wizard step.

| Surface | Purpose |
|---|---|
| `POST /api/onboarding/inbox` | `{user, password, host?, port?}` → **live IMAP login verify FIRST** (`_verify_imap`, returns a user-facing error string), then on success splits storage: secret `AV3_IMAP_PASSWORD` → `<data_dir>/.env` (via dotenv `set_key` + `os.environ` for the live process), non-secret `{enabled,user,host,port}` → `user_config.json`. 400 (saves nothing) on a bad/typo'd password. |
| `settings.load_settings` | Now ALSO `load_dotenv(<data_dir>/.env, override=False)` so a packaged install (no project checkout) picks up the wizard-written secret; an existing project-root `.env` (the owner's setup) stays authoritative. |
| `onboarding_status.to_dict` | Echoes `inbox` (non-secret only — the password is NEVER returned). |
| `doctor.check_inbox` | OFF→PASS (opt-in); enabled-but-incomplete (no user / no `AV3_IMAP_PASSWORD`)→WARN; complete→PASS. No live login in doctor (slow + lockout risk). |
| wizard step 7 (`onboarding.html`/`.js`) | Email + App Password + host/port form, App-Password instructions, "Connect & continue" (verifies) / "Skip for now". Optional — not in the `is_complete` gate (`STEPS`/`_firstIncomplete`/`isDone` treat `email` like `web-prefs`: `null` gate). |

**Load-bearing decisions:**
- **Verify-before-save.** The #1 app-password failure is a typo; a live login at setup time surfaces
  it in the wizard instead of as a silent no-op at the next scheduler cycle. The endpoint is
  testable because `_verify_imap` uses `imaplib.IMAP4_SSL`, which tests monkeypatch (a `_FakeIMAP`
  that simulates connect/login outcomes) — no network in CI.
- **Secret split holds.** Password ONLY to `.env` (the project's .env-only rule); a test asserts the
  password never appears in `user_config.json` nor in `/api/onboarding/state`.
- **Takes effect on next worker restart** — the inbox gather stage is built at scheduler start
  (`creds_from_settings` is read then); stated honestly via the endpoint's `note`.
- **⚠️ Alpine gotcha (re-hit):** `x-text` is evaluated even when its wrapping `x-show` is false. The
  "currently connected" line read `status.inbox.user` while `status` was still `null` on first paint
  → console TypeError. Fix: optional-chain the inner bindings (`status?.inbox?.user`). Rule: any
  `x-text` that dereferences an async-loaded object must use `?.`, regardless of an outer `x-show`.

### Inline required-field validation + rewritten "done" screen

- `saveContact`/`saveWorkHistory`/`saveSkills` now validate before POST (name+email, ≥1 role with
  company+title, ≥1 skill) and set a red `.validation-note` instead of silently "saving" empties
  that leave the gate false + the banner stuck. Cleared on `goto()`.
- The done screen no longer says "edit `data/user_config.json` to change things" (a dead end for a
  non-technical user). New copy: the worker starts automatically, edit goals on the dashboard's
  Goals card, optionally connect email + drop a `resume.pdf`.
- Browser-verified: empty contact save shows the message + stays on the step; email step renders +
  Skip advances; done copy correct; 0 console errors.

### NOT here (CLI, see mvp-readiness.md)
The scheduler `resume.pdf`-gate relax (`run_cmd`/`serve_cmd` → fact-bank-only), `av3 setup-llm`, the
`doctor.check_llm` embed-model check, and the `av3-launcher.{cmd,sh}` stale-module-path fix are CLI/
distribution changes documented in `research/observability-and-distribution.md` + `mvp-readiness.md`.
