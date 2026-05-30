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

---

## Phase 5 (3/M) — `av3 telemetry on|off|status` + `av3 export-diagnostics` landed

The user-facing opt-in manager for the §9 mirror, plus the support-bundle
surface. The mirror plumbing (queue + scrubbers) shipped in (2/M); this turns
the single network-egress switch into a CLI the user actually flips, and gives
the owner a "send me a tarball" workflow. 18 new tests; full v3 suite **579
green** (11 deselected by design).

### New surface

| Surface | Purpose |
|---|---|
| `av3 telemetry on [--handle X] [--relay-url U]` | Opt IN. Shows the §9 disclosure, captures/keeps a local handle, flips `telemetry.enabled=True` |
| `av3 telemetry off` | Opt OUT. Flips the flag; keeps the handle + any queued rows |
| `av3 telemetry status` | enabled?, `user_id` (sha256[:10]), relay_url, mirror queue depth / last enqueue / last failure |
| `av3 export-diagnostics [--raw] [--error-limit N]` | Single tarball for support: settings (secrets stripped), doctor, scrubbed error+inferred rows, stats, mirror status, manifest |
| `MirrorQueue.summary()` (`av3/telemetry/mirror.py`) | One-shot {pending, delivered, last_enqueued_at, last_error, …} read for status + diagnostics |
| `av3/telemetry/diagnostics.py` | `build_diagnostics()` / `collect_diagnostics()` — the tarball builder, split so tests assert on contents without untarring |
| `_prompt_for_handle()` (`av3/cli/main.py`) | Interactive handle prompt; loops until non-empty so we never store a blank that hashes to `anonymous` |

### Load-bearing design decisions to remember

1. **Config R/W reuses the web onboarding helpers.** `telemetry on|off` read/write
   `user_config.json` via `av3.web.onboarding.load_user_config` /
   `save_user_config` — the SAME atomic dict-merge helpers the web onboarding
   `POST /onboarding/telemetry` route uses, writing the SAME
   `{enabled, handle, relay_url}` shape. The open question "keep both opt-in
   paths in sync" is resolved structurally: there is one config writer, two
   front-ends. No second persistence path to drift.

2. **Diagnostics PII strategy = SCRUB by default, `--raw` escape hatch** (the
   (3/M) open question, resolved). Default routes error rows through
   `scrub_error_event` and inferred rows through `scrub_inferred_answer_event`
   — the bundle is safe to email anywhere. `--raw` adds a verbatim `events.db`
   copy + un-scrubbed error messages for in-group deep debug, and the CLI warns
   loudly. **The one asymmetry: the inferred-answer *value* is scrubbed even in
   `--raw` mode** — §9's "the answer never leaves" is a hard line that `--raw`
   does NOT relax (it lives in `context_json`; `_inferred_rows` always routes
   through the category scrubber regardless of mode). Test:
   `test_export_diagnostics_raw_includes_db_and_unscrubbed` asserts the DB is
   present AND the answer value still absent.

3. **Settings are ALWAYS secret-stripped, both modes.** `_strip_secrets` drops
   `llm.gemini_api_key` and `telemetry.handle` from the settings dump. A raw
   handle or an API key in a support bundle is never acceptable — the manifest
   carries the hashed `user_id` instead, which is exactly what would be mirrored
   anyway.

4. **The §9 disclosure prints BEFORE the prompt, only when needed.** If
   `--handle` is given or a handle is already stored, `telemetry on` is silent
   and non-interactive (scriptable). Only a first-time opt-in with no stored
   handle shows the full "what leaves your machine" text and prompts. So
   re-enabling after an `off` never re-nags.

5. **`off` keeps queued rows + warns.** Already-enqueued rows are already
   scrubbed and harmless; deleting them on opt-out would be surprising. `off`
   reports the pending count so the backlog isn't silently forgotten; the user
   drains (after re-enabling) or prunes.

6. **`build_diagnostics` vs `collect_diagnostics` split.** `collect_*` returns
   the in-memory `{filename: content}` dict (pure read, own short-lived
   connections); `build_*` tars it. Tests assert on the dict / on extracted
   members without needing a real tar round-trip for the content checks.

### Edge cases covered (see `tests_v3/test_cli_telemetry.py`)

| Concern | Test |
|---|---|
| `on --handle` persists enabled + handle; prints hashed id not raw name | `test_telemetry_on_with_handle_flag_persists_enabled` |
| `on` with no handle shows §9 disclosure incl. "NEVER the answer value" + prompts | `test_telemetry_on_prompts_when_no_handle` |
| `on` after a prior `on/off` reuses stored handle, no re-prompt | `test_telemetry_on_reuses_stored_handle_without_prompt` |
| `on --relay-url` persists the endpoint | `test_telemetry_on_sets_relay_url` |
| `off` disables but keeps handle | `test_telemetry_off_disables_but_keeps_handle` |
| `off` warns about pending queued rows | `test_telemetry_off_warns_about_pending_rows` |
| `status` default = disabled / anonymous / empty queue | `test_telemetry_status_default_disabled` |
| `status` shows user_id + pending count | `test_telemetry_status_shows_user_id_and_pending` |
| diagnostics scrubbed: no email/path in errors; answer value & EEO row absent | `test_export_diagnostics_scrubbed_default` |
| diagnostics strips GEMINI_API_KEY + raw handle from settings; manifest has hashed id | `test_export_diagnostics_strips_secrets` |
| `--raw` includes events.db + un-scrubbed errors BUT still no answer value | `test_export_diagnostics_raw_includes_db_and_unscrubbed` |

### What's NOT in this sub-phase

* **HTTP relay client / `av3 mirror drain`.** The queue still only fills; nothing
  drains it yet. `telemetry on --relay-url` stores the endpoint but no POST
  happens until (4/M).
* **Owner-hosted Cloudflare Worker relay + Turso write token.** (4/M).
* **Doctor relay-reachability check.** (4/M).

---

## Phase 5 (4/M) — relay client + drainer + Cloudflare Worker template + doctor check landed

Closes the telemetry loop: the queue from (2/M) now has a way OUT. The
owner-hosted relay (template) holds the Turso write token; the client drainer
POSTs scrubbed rows to it out-of-band; doctor pings its `/health`. 13 new tests;
full v3 suite **592 green** (11 deselected by design).

### New surface

| Surface | Purpose |
|---|---|
| `MirrorClient` (`av3/telemetry/client.py`) | Drains a `MirrorQueue`: `next_due → POST → mark_delivered/mark_failed`. Pluggable POST transport (httpx default, mockable) |
| `DrainResult` | `{attempted, delivered, failed}` + `all_delivered` |
| `av3 mirror drain [--limit N] [--timeout-s S]` | One-shot out-of-band drain. Gated on telemetry.enabled + relay_url |
| `doctor.check_relay_reachable` | `GET {relay}/health` when telemetry on; PASS off, WARN if no url / unreachable |
| `relay/worker.js` + `wrangler.toml` + `schema.sql` + `README.md` | Deployable Cloudflare Worker relay template (Turso token in env, re-scrub, KV rate-limit) |

### Load-bearing design decisions to remember

1. **The drainer is a standalone one-shot, NOT a synchronous pipeline step.**
   Spec §9: "out-of-band so a slow relay never blocks the pipeline." Wiring HTTP
   into the scheduler's synchronous `_maintenance` hook would put relay latency
   on the apply loop's critical path. So `av3 mirror drain` is a separate command
   the user crons (same cheap-rerun shape as `av3 backup` / `av3 prune`). An
   integrated async drain *tick* (own cancellable task + hard time budget) is a
   deliberate later refinement — the queue is durable and backoff is bounded, so
   nothing is lost draining on an external cadence. **This was the (4/M) open
   choice ("separate tick or one-shot CLI"); resolved to the CLI for v3.0 because
   it's the only option that *structurally* can't block the loop.**

2. **`mirror drain` always exits 0 on transient relay failure.** A failed POST is
   a remote condition (redeploy, network blip), not a local error; the backoff
   ladder retries it. Exiting non-zero would flap the user's cron alert on every
   brief outage. Only a genuine *config* problem (no relay_url) exits 2. Mirrors
   the (1/M) "asking for errors isn't an error" exit-code philosophy.

3. **Pluggable POST transport = no network in tests.** `MirrorClient(post=...)`
   injects the transport; the httpx default is built lazily (`_httpx_post`) so the
   telemetry package never imports httpx unless a real drain runs. Tests pass a
   closure returning a status code or raising — full coverage of 2xx / 4xx-5xx /
   transport-exception / per-row-isolation without a server.

4. **The relay re-scrubs as a SECOND line of defence and never trusts the wire.**
   `worker.js`'s `rescrub()` re-applies the §9 (a)/(b) allow-lists — same
   invariants as `av3/telemetry/scrub.py`: **no `answer` field exists** (the value
   can't reach Turso even from a hostile client) and **EEO rows drop** (returns
   `202 dropped` so the client stops retrying a row we deliberately discard).
   Keep `ERROR_FIELDS`/`INFERRED_FIELDS` in `worker.js` in sync with the Python
   allow-lists if the §9 schema changes.

5. **The app holds no Turso token — ever.** It lives only as a Worker secret
   (`wrangler secret put TURSO_AUTH_TOKEN`). The client POSTs unauthenticated
   scrubbed JSON; the relay rate-limits by `user_id` (KV, fixed window, soft-fails
   open if the KV binding is omitted) and inserts via the libSQL HTTP pipeline
   API. Compromised client ⇒ no credential to steal (spec §9 threat model).

6. **`doctor` relay check WARNs, never FAILs.** Telemetry is additive + opt-in; a
   down relay must not fail CI for users who don't use telemetry, and even for
   opted-in users the local pipeline is unaffected (the queue just backs up). Off
   → PASS, on-without-url → WARN, on-and-unreachable → WARN.

### Edge cases covered (`tests_v3/test_mirror_client.py`)

| Concern | Test |
|---|---|
| `_ingest_url` appends `/ingest`, handles trailing slash | `test_ingest_url_appends_path` |
| 2xx → all delivered, queue empty, correct POST shape | `test_drain_delivers_all_on_2xx` |
| HTTP 5xx → mark_failed, rows stay pending, attempts bumped | `test_drain_marks_failed_on_http_error` |
| transport exception → mark_failed | `test_drain_marks_failed_on_transport_exception` |
| `--limit` bounds the pass | `test_drain_respects_limit` |
| one row's failure doesn't abort the others | `test_drain_per_row_isolation` |
| doctor: off→PASS / no-url→WARN / healthy→PASS / unreachable→WARN | `test_relay_check_*` |
| CLI gating: disabled→noop, no-url→exit 2, happy→delivered | `test_mirror_drain_*` |

### What's NOT in this sub-phase

* **Live relay deploy.** `relay/` is a template; deploying it (Cloudflare account
  + Turso DB) is an owner one-time op documented in `relay/README.md`, not part
  of the client build or test suite.
* **Integrated async drain tick** in the scheduler — deferred (decision #1 above).
* **Bundled installer + auto-update feed.** (5/M).
* **Fresh CLAUDE.md for v3.** (6/M).

---

## Phase 5 (5/M) — bundled installer + auto-update feed landed

The distribution half of §11a: a PyInstaller build of the lean app, a first-run
Chromium fetch, and a GitHub-Releases update check that prompts (not
auto-replaces). 20 new tests; full v3 suite **612 green** (11 deselected).

### New surface

| Surface | Purpose |
|---|---|
| `av3/update.py` | `check_for_update()` / `parse_release_feed()` / `compare_versions()` — PEP 440 compare vs GitHub Releases, injectable fetch |
| `av3 update [--repo R] [--exit-code]` | Check + prompt; exit 10 w/ `--exit-code` when newer exists; exit 0 on offline |
| `av3 install-browser [--backend ...]` | First-run/installer Chromium fetch (patchright→playwright fallback) |
| `build_v3.py` + `run_v3.py` | PyInstaller build script + frozen entry (no-arg → `av3 launch`) |

### Load-bearing design decisions to remember

1. **Chromium is NOT bundled — fetched on first run (the "lean installer"
   decision; the (5/M) open question resolved).** Playwright/patchright resolve
   browser binaries through their own cache, NOT PyInstaller's `_MEIPASS` temp
   dir, so bundling a ~150 MB Chromium into the onefile is fragile and against
   §11a "lean." The installer ships only the Python app; first launch (or the
   installer post-step) runs `av3 install-browser`. **And most applies use the
   user's REAL Chrome via `channel` (spec §8c)** — this Chromium is only the
   stealth driver + the busy-Chrome fallback (`session.py` falls back to a
   `_chromium` profile when real Chrome is busy), so many users barely touch it.
   Two-step install: `AutoApplierV3.exe`, then `AutoApplierV3.exe install-browser`.

2. **Frozen-exe launcher bug fixed.** `launch_cmd` spawned
   `[sys.executable, "-m", "av3.cli.main", "serve", …]` — which is correct from
   source but **broken in a PyInstaller onefile** (there `sys.executable` is the
   bundled app and `-m av3.cli.main` is meaningless; the bootloader runs
   `run_v3.py`). Now `getattr(sys, "frozen", False)` switches to
   `[sys.executable, "serve", …]`, and `run_v3.py` forwards argv to the Click
   group so `<exe> serve` works. The single binary is BOTH the one-click
   launcher (no args → `av3 launch`) AND the full CLI (`<exe> doctor`, etc.).

3. **Update is check + prompt, never auto-replace (v3.0).** Replacing a running,
   browser-driving service in place is its own risk surface; a human running the
   installer is the safe path. `av3 update` prints where to get the build.
   Auto-download/replace is a later refinement if it ever earns its keep.

4. **`allow_prerelease=True` by default.** Current version is `3.0.0a0` and the
   group runs alpha builds; skipping prereleases would hide alpha updates from
   exactly the testers. The flag exists for a future stable channel. The feed
   parser accepts BOTH the `/releases` list shape (pick newest non-draft) and the
   `/releases/latest` dict shape.

5. **An update check NEVER raises.** `check_for_update` returns `None` on offline
   / HTTP-error / malformed-feed, and the CLI treats that as "couldn't check,
   carry on" (exit 0). A launcher or doctor run must never die on a missed check.

6. **`packaging` added to v3 deps** (was transitively present; now explicit since
   `update.py` imports `packaging.version` directly).

### Edge cases covered (`tests_v3/test_update.py`)

| Concern | Test |
|---|---|
| PEP 440 compare incl. alpha + leading-v + unparseable→safe | `test_compare_versions` |
| feed dict / list-newest-nondraft / prerelease-gate / empty→None | `test_parse_feed_*` |
| `check_for_update` newer / HTTP-error→None / exception→None | `test_check_for_update_*` |
| `av3 update` reports / `--exit-code`=10 / offline exit 0 | `test_update_cmd_*` |
| `install-browser` success / playwright fallback / all-fail exit 1 | `test_install_browser_*` |

### What's NOT in this sub-phase / not unit-tested

* **The PyInstaller build itself** (`build_v3.py`) — shells out to a multi-minute
  native build; PyInstaller is a build-host tool, not a runtime dep. Not run in
  CI. The logic it leans on (update check, install-browser) IS unit-tested.
* **Auto-download/replace of updates** — deferred (decision #3).
* **A dashboard update-available badge** — the `av3 update` check is the v3.0
  surface; wiring it into the web UI is a nicety, not required for §11a.
* **Fresh CLAUDE.md for v3.** (6/M).

---

## Phase 5 (6/M) — fresh v3 CLAUDE.md + Phase 5 wrap-up

The documentation close-out. No new code surface; the deliverables are docs the
next session reads.

### What landed

* **`CLAUDE.md` rewritten v3-first.** The working-discipline preamble (invoke
  `auto-applier` + `unstuck`, document research before "done") is preserved
  verbatim — it's the most load-bearing part. The body now describes the `av3/`
  package: module layout, the job state machine, the staged pipeline, the
  reliability invariants, the telemetry exception, distribution, and the data
  layout. v2 is demoted to a one-paragraph "reference only, do not extend"
  footer. The old "sections below describe v2" note is gone.
* **Spec §11b Phase 5 → ✅ DONE**, with a one-line summary of all six sub-phases
  and the final 612-green count. The "→ Ship v3.0" line is marked complete
  (phases 0–5 done; remaining work is v3.1 / Phase 6).
* **This research doc** now carries a per-sub-phase section (1/M…6/M) — the
  authoritative decision record for everything Phase 5.

### Phase 5 — complete picture (the whole arc, for fast recall)

| Sub-phase | Shipped | Key invariant locked in |
|---|---|---|
| 1/M | `cli errors` / `cli stats` (local triage from events.db) | both always exit 0; CLI owns `--since` parsing |
| 2/M | mirror queue + category scrubbers | answer value & EEO can't be mirrored — schema has no field for them |
| 3/M | `cli telemetry on/off/status` + `export-diagnostics` | one config writer (web+CLI share it); diagnostics scrub-by-default, `--raw` never relaxes the answer-value line |
| 4/M | relay template + `MirrorClient` + `av3 mirror drain` + doctor relay check | drainer is standalone (can't block the loop); Turso token only in the relay |
| 5/M | PyInstaller installer + `av3 update` + `av3 install-browser` | Chromium fetched not bundled; frozen-launcher spawns `<exe> serve` |
| 6/M | v3-first CLAUDE.md + wrap-up | — |

**Net for Phase 5:** 90 new tests; full v3 suite went 495 → 612 green (the 117
delta spans 1/M…5/M). 11 deselected by design (live smoke/eval/integration).
v3.0-core (phases 0–5) is complete; Phase 6 = v3.1.

### Carry-over / not done in Phase 5 (for the v3.1 or next-session backlog)

* **Live relay deploy + a real opted-in mirror round-trip.** `relay/` is a
  deployable template; nobody has run `wrangler deploy` + a live `av3 mirror
  drain` against it yet. First real opt-in is the smoke test.
* **Integrated async drain tick** (vs the standalone `av3 mirror drain` cron).
* **Dashboard surfaces** for telemetry status / update-available / mirror
  pending — all have CLI/programmatic surfaces; the web badges are niceties.
* **The PyInstaller build is unverified in CI** (PyInstaller isn't a runtime
  dep / wasn't installed in the build session). `build_v3.py` is written to the
  documented flags; first real `python build_v3.py` on a build host is pending.
* **Carry-overs from earlier phases still open:** per-job résumé-path rewire
  (apply worker still reads a single `artifacts/resume.pdf` fallback — Phase 3
  carry-over); live Greenhouse auto-pass-rate gated submit (Phase 1 decision
  gate). Neither is Phase 5 scope.
