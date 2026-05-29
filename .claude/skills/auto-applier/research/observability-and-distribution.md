# Observability & distribution — Phase 5 (spec §9, §11b)

Knowledge base for Phase 5 of the v3 build: the local triage CLI surface,
the opt-in remote telemetry mirror + relay, the doctor expansion, the
bundled installer + auto-update feed, and a fresh CLAUDE.md for v3.

Earlier phases shipped the **observability spine** — `@stage` writes
start/ok/error/skip rows into `events.db` automatically — and a
local-only **PII scrubber**. Phase 5 turns that spine into a usable
operator surface and a privacy-preserving remote channel.

Each sub-phase appends a `## Phase 5 (X/M) — <slug>` section here as
the work lands.

---

## Phase 5 (1/M) — `av3 errors` + `av3 stats` landed

The two local-only triage commands that read directly from `events.db`.
The spec's "any future Claude session debug straight from SQL — no log
files" surface from §9, satisfied without touching the network. 34 new
tests (full v3 suite 529 green, 11 deselected by design). **Spec §9 +
§11b Phase 5 bullet 1 (partial: `cli errors|stats`) satisfied; the
`cli telemetry` / `cli export-diagnostics` halves land in (3/M).**

### New surface

| Surface | Purpose |
|---|---|
| `av3 errors` | Filtered per-row error view (replaces `tail -f errors.log` from v2) |
| `av3 stats` | Per-stage aggregate (ok/error/skip/avg_ms) — "is the pipeline broken?" |
| `EventSink.query_errors(...)` | Composable-filter query helper (since/stage/platform/run_id/limit) |
| `EventSink.query_stats(...)` | Composable-filter aggregate (since/platform/run_id) |
| `_parse_since` (CLI helper) | `30m|2h|7d` → ISO cutoff matching `EventSink.emit` ts shape |

### Flag matrix

Both commands share the same composable filter set; `--limit` is errors-only,
`--stage` is errors-only (stats *breaks down by* stage, never *filters* on it).

```
av3 errors  [--limit N] [--stage X] [--platform X] [--since 30m|2h|7d]
            [--run-id ID] [--json]
av3 stats   [--platform X] [--since 30m|2h|7d] [--run-id ID] [--json]
```

### Why the CLI owns `--since` parsing (not the sink)

`_parse_since` lives in `av3/cli/main.py`, not in the sink. Three reasons:

1. **UI-level errors.** `click.BadParameter` is the only way to make
   `--since x` surface as a clean "usage error" instead of a Python
   stack trace. That's a CLI concept; pushing it into the sink would
   couple the data layer to Click.
2. **The sink stays pure-DB.** `query_errors` / `query_stats` take an
   `since_iso: str | None` — same ISO-8601 UTC shape `EventSink.emit`
   writes. Any future caller (the web dashboard, a notebook script) can
   compute that cutoff however it wants.
3. **One format, lexicographic compare.** SQLite text comparison only
   works as a date filter when both sides share the format. The CLI
   formatter calls `strftime("%Y-%m-%dT%H:%M:%S")` so it never sees a
   trailing `Z` / fractional seconds / `+00:00` discrepancy.

### Why both commands always exit 0

Asking for errors is **not** itself an error condition. The temptation
is to exit 1 when error events exist (so cron can alert on it) — but:

* That breaks `av3 errors --json | jq` pipelines. Click sets the exit
  code from the function's return value; an alert script can compute
  alert-worthiness from the JSON itself (`jq '. | length'`).
* The scheduler's own exit code already non-zeros on `total_errors`
  (see `av3 run`'s wrap-up). The triage command is for *reading*
  events, not for *gating* on them.
* `--since` parse failures still exit 2 (Click's "usage error") so the
  user sees a hard fail when they typo a flag.

If a future "alert on errors" UX is needed, it should be a separate
`av3 errors --check-empty` flag, not a default-on behavior change.

### Why a hand-rolled ASCII table (and not `rich` / `tabulate`)

* **Zero new dependency.** Both `rich` and `tabulate` are wonderful and
  unnecessary here — five columns of plain text don't justify pulling
  in 100 KB of formatter code with its own ANSI handling.
* **cp1252 console safe.** The CLI module-top reconfigures stdout to
  utf-8-with-replacement, but other shells in the v3 path (PyInstaller's
  bundled console, Windows Terminal pre-cp65001) still surprise us.
  ASCII glyphs and a trailing `~` truncation marker render the same
  everywhere.
* **`--json` is the structured interface.** Operators who want pretty
  output already pipe through `jq` or `column -t`. The table is
  for the eyeballs case; the JSON is for everything else.

### Why `--json` round-trips the full payload (no in-table truncation)

`av3 errors` truncates `error_msg` to ~60 chars in the table view so
one row fits on a 120-col terminal. But `--json` ships the *full*
payload, untruncated. That's the whole reason for `--json`: when the
table elides too much, the user falls back to JSON.

Concretely:

```bash
av3 errors --json | jq '.[] | select(.error_type == "TimeoutError")'
```

…shows the full message. `error_msg` in `events.db` is already capped
implicitly by the scrubber's `_MAX_LEN=500` *for mirrored* rows, but
local rows carry whatever the worker raised.

### Why `query_errors` / `query_stats` defence-in-depth tests parameterized SQL

`test_query_errors_uses_parameterized_sql` feeds
`"apply'; DROP TABLE events;--"` as a stage filter. The point is **not**
that a malicious caller could ever reach this code (the CLI is the only
caller and Click validates the string upstream) — the point is to lock
in the contract. If a future change refactors the WHERE-clause
construction into f-string concatenation, the test fails before
anything ships.

### Edge cases covered (see `tests_v3/test_cli_observability.py`)

| Concern | Test |
|---|---|
| Empty sink → friendly message, exit 0 | `test_errors_no_events_friendly_message`, `test_stats_no_events_friendly_message` |
| Error rows render, ok rows don't | `test_errors_renders_table_for_error_rows` |
| Stage / platform / run_id filters | `test_errors_filters_by_*` |
| Relative `--since` window | `test_errors_since_excludes_older_rows`, `test_stats_since_window_filters_aggregate` |
| Limit honored | `test_errors_respects_limit` |
| JSON round-trips full payload, no truncation | `test_errors_json_output_parses`, `test_errors_long_msg_truncated_in_table` |
| `--since` parser rejects garbage incl. embedded whitespace | `test_parse_since_rejects_garbage` |
| Bad `--since` → exit 2 (usage error) | `test_errors_bad_since_surfaces_as_usage_error`, `test_stats_bad_since_*` |
| Per-stage aggregate shape | `test_stats_per_stage_counts`, `test_stats_json_output_shape` |
| Sink-level filter composition + safety | `test_query_errors_composes_filters`, `test_query_stats_composes_filters`, `test_query_errors_uses_parameterized_sql` |

### What's NOT in this sub-phase

* **`cli telemetry on|off|status`.** Opt-in manager for the remote
  mirror — needs the mirror plumbing first. Lands in (3/M).
* **`cli export-diagnostics`.** Bundles the data dir's diagnostics
  (events.db copy, last N error rows, settings dump, doctor results,
  app version) into a single tarball for support. Lands in (3/M).
* **Remote scrubbed mirror.** Scrubber rules per §9 (error/critical +
  inferred/novel-answer-event categories), client-side queue + retry,
  opt-in gating via `settings.telemetry`. Lands in (2/M).
* **Owner-hosted relay.** Cloudflare Worker template, Turso write token
  scoped to the relay. Lands in (4/M).
* **Doctor relay-reachability check.** Adds `relay_reachable` to
  `run_doctor()`. Lands in (4/M).
* **Bundled installer + auto-update feed.** Lands in (5/M).
* **Fresh CLAUDE.md for v3.** Lands in (6/M).

### Carry-over: by-source aggregation

`av3 stats` aggregates by `stage` only. A future "what's failing per
source?" view would group by `platform` (or `(stage, platform)`).
Sketch:

```sql
SELECT stage, platform, SUM(status='ok') AS ok, SUM(status='error') AS error
FROM events WHERE platform IS NOT NULL GROUP BY stage, platform ORDER BY stage;
```

Not worth shipping until a real operator workflow asks for it; one of
those things to remember exists, not to build proactively.
