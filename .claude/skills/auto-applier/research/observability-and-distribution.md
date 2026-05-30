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

---

## Phase 5 (2/M) — telemetry mirror queue + categorized scrubbers landed

Client-side spool for the opt-in remote mirror (spec §9). When telemetry
is **opted in**, `EventSink.emit` now also enqueues a category-scrubbed
payload into a `mirror_queue` table inside `events.db`, for later
out-of-band drainage by the Phase 5 (4/M) relay client. The HTTP POST
itself is **not** in this sub-phase. 39 new tests; full v3 suite **568
green**.

### New surface

| Surface | Purpose |
|---|---|
| `MirrorQueue` (`av3/telemetry/mirror.py`) | The spool. enqueue / next_due / mark_delivered / mark_failed / pending_count / prune_delivered |
| `MirrorPolicy` (`av3/telemetry/mirror.py`) | Frozen dataclass holding identity (user_id) + opt-in state. Attached to the sink at startup |
| `user_id_from_handle(handle)` | `sha256(handle)[:10]` — the §9 attribution mechanism |
| `scrub_error_event(payload)` | Category scrubber for §9 (a) error/critical events |
| `scrub_inferred_answer_event(payload)` | Category scrubber for §9 (b) inferred-answer events |
| `EventSink.attach_mirror(policy)` / `detach_mirror()` | Sink-side opt-in hook |
| `attach_mirror_from_settings(sink, settings)` | The CLI one-liner; called via `_install_sink(settings)` everywhere |

### Load-bearing design decisions to remember

1. **Single `mirror_queue` table inside `events.db` — not a column on
   `events`, not a JSONL spool.**
   * Same DB as the event row → atomic enqueue on the same connection /
     WAL, no second persistence layer to corrupt independently.
   * Separate table (not `mirror_state` on `events`) because `events` is
     high-write append-mostly and a column update would have the drainer
     constantly UPDATEing old rows, fighting WAL page reuse. A small hot
     queue table keeps the drainer's scan tiny.
   * Not JSONL because (a) composability with future `cli stats`
     `mirror_pending`, (b) the relay (4/M) only needs one SQLite
     connection, and (c) JSONL would need its own append-lock and reader.

2. **Category scrubbers are at the *structured-field* level, not just
   text.** §9 (a) and (b) have different allow-lists. The error scrubber
   re-keys `error_msg` → `scrubbed_error_msg` after running the text
   `scrub()`; the inferred-answer scrubber has **no `answer` field at
   all** — even if a regression accidentally passes one, the scrubber's
   allow-list drops it. That "the schema cannot carry the answer" is the
   most load-bearing test in `test_mirror_queue.py`
   (`test_answer_value_is_dropped_even_if_passed`).

3. **EEO inferred-answer rows do not mirror at all (§8d defence in
   depth).** The apply worker already filters them upstream
   (`_mirror_inferred_resolutions`), but `scrub_inferred_answer_event`
   returns `{}` if `category == "eeo"` and `MirrorQueue.enqueue` returns
   `None` on empty scrubbed payload, so a regression upstream still cannot
   leak an EEO row to the queue.

4. **Identity computed once, at policy construction.**
   `MirrorPolicy.from_settings()` calls `user_id_from_handle(handle)` so
   the raw handle is referenced exactly once at startup and otherwise lives
   only in `TelemetryConfig.handle` on disk. The queue stores the
   short-hex `user_id`, never the raw name.

5. **Opt-in gating is single-point.** `EventSink.emit` mirrors only when
   `mirror_policy is not None and mirror_policy.enabled` is True.
   `attach_mirror_from_settings` builds the policy from
   `settings.telemetry` so the default (`enabled=False`) keeps the
   mirror cold. The data layer (`MirrorQueue`) is oblivious to settings —
   the caller decides when to attach.

6. **`enabled=False` policy is still attached.** This lets a future
   `cli telemetry status` introspect identity ("you'd send as
   `a3f9c1d204`") without flipping sending on. The sink simply returns
   early in `_maybe_mirror`.

7. **Backoff ladder is hardcoded (0/30s/2m/10m/1h/6h), top step reused
   indefinitely.** No exponential overflow risk. Pending rows are
   retried forever (bounded only by `prune_delivered`); delivered rows
   are kept long enough for one audit window then pruned.

8. **The HTTP relay is (4/M).** This sub-phase ships ONLY the spool +
   the drainage API (`next_due` / `mark_delivered` / `mark_failed`). The
   relay client and `cli telemetry on|off|status` UX land in subsequent
   sub-phases. No outbound network call is reachable from this sub-phase's
   code.

### Edge cases covered (see `tests_v3/test_mirror_queue.py`)

| Concern | Test |
|---|---|
| Error scrubber matches the §9 (a) shape | `test_shape_matches_spec` |
| Error msg is text-scrubbed (PII, paths) | `test_error_msg_is_pii_scrubbed` |
| Unknown keys dropped from error payload | `test_unknown_keys_are_dropped` |
| `None`-valued fields stripped from wire payload | `test_none_values_stripped` |
| Overlong `error_type` truncated | `test_long_error_type_truncated` |
| Inferred-answer scrubber matches the §9 (b) shape | `test_shape_matches_spec` |
| **Answer value never appears even if caller passes one** | `test_answer_value_is_dropped_even_if_passed` |
| EEO inferred-answer row drops entirely | `test_eeo_category_drops_row_entirely` |
| Question text is PII-scrubbed | `test_question_text_is_pii_scrubbed` |
| Unknown keys dropped from answer payload | `test_unknown_keys_are_dropped` |
| `user_id_from_handle` stable + 10-char + whitespace-normalized | `TestUserIdFromHandle::*` |
| Enqueue persists scrubbed payload + delivered_at NULL | `test_enqueue_error_persists_scrubbed_payload` |
| EEO inferred-answer enqueue returns None, writes nothing | `test_enqueue_eeo_inferred_answer_returns_none` |
| Unknown category raises ValueError | `test_enqueue_unknown_category_raises` |
| `next_due` returns oldest first (chronological drain) | `test_next_due_returns_oldest_first` |
| `next_due` honors limit | `test_next_due_respects_limit` |
| `next_due` skips delivered rows | `test_next_due_skips_delivered` |
| `next_due` skips not-yet-due rows | `test_next_due_skips_not_yet_due` |
| `mark_failed` bumps attempts + reschedules | `test_mark_failed_bumps_attempts_and_pushes_next_retry` |
| `mark_failed` backoff caps at top step (no overflow) | `test_mark_failed_caps_backoff_at_top_step` |
| `mark_delivered` clears `last_error` | `test_mark_delivered_clears_last_error` |
| `mark_failed` truncates long reason | `test_mark_failed_truncates_long_reason` |
| `pending_count` / `delivered_count` | `test_pending_and_delivered_counts` |
| `prune_delivered` deletes delivered, keeps pending | `test_prune_delivered_only` |
| **No policy → no mirror** (gating) | `test_no_policy_attached_writes_locally_only` |
| **Disabled policy → no mirror** (gating) | `test_policy_disabled_writes_locally_only` |
| Enabled policy mirrors errors with user_id + app_version | `test_policy_enabled_mirrors_error` |
| Enabled policy mirrors resolver_inferred → answer absent | `test_policy_enabled_mirrors_resolver_inferred` |
| status=ok non-resolver events do NOT mirror | `test_status_ok_non_resolver_does_not_mirror` |
| status=skip events do NOT mirror | `test_status_skip_does_not_mirror` |
| `detach_mirror` silences subsequent emits | `test_detach_mirror_silences_subsequent_emits` |
| EEO resolver_inferred → dropped even via emit path | `test_resolver_inferred_with_eeo_category_does_not_mirror` |
| `attach_mirror_from_settings` with disabled telemetry | `test_disabled_telemetry_attaches_disabled_policy` |
| `attach_mirror_from_settings` with enabled+handle | `test_enabled_telemetry_with_handle_attaches_user_id` |
| Enabled without handle → `user_id="anonymous"` | `test_enabled_without_handle_falls_back_to_anonymous` |

### Why `_next_retry_iso` uses `isoformat(timespec="seconds")` (not `strftime`)

First version used `strftime("%Y-%m-%dT%H:%M:%S")` and broke immediately
because `utcnow_iso()` emits the `+00:00` offset (Python's
`isoformat` always includes the tz on a tz-aware datetime). The fix is
to use `isoformat(timespec="seconds")` so `next_retry_at` has the SAME
shape as `enqueued_at` (and as every other timestamp `EventSink.emit`
writes), so SQL lexicographic compare in
`WHERE next_retry_at <= ?` works without a format mismatch.

The `--since` parser in `av3 cli main` uses `strftime` without the
offset — that's intentional and safe there: lexicographically a string
without `+00:00` sorts *before* the same time with `+00:00`, so
`ts >= cutoff_no_offset` matches all real events with the offset
suffix. That asymmetry is a (1/M) decision; we don't need to fix it,
just remember it.

### What's NOT in this sub-phase

* **HTTP relay client.** Drainage iterator that walks `next_due()`,
  POSTs to the relay, calls `mark_delivered` / `mark_failed`. Phase 5 (4/M).
* **`cli telemetry on|off|status`.** The user-facing opt-in toggle. (3/M).
* **`cli export-diagnostics`.** Bundles diagnostics tarball. (3/M).
* **`cli stats` showing `mirror_pending`.** The plumbing exists
  (`MirrorQueue.pending_count()`) but the CLI surface doesn't add a
  column yet — no point until (4/M) makes "pending" meaningful.
* **Owner-hosted Cloudflare Worker relay + Turso write token.** (4/M).
* **Doctor relay-reachability check.** (4/M).
