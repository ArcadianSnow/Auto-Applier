"""Contract tests for ``av3 errors`` and ``av3 stats`` (Phase 5 1/M).

Spec section 9 + section 11b Phase 5 bullet 1. The two commands surface the
local ``events.db`` for triage — they do NOT depend on the (2/M) opt-in
remote mirror, so the tests run with a fully local sink.

What is intentionally NOT tested here:
  * The ``@stage`` wrapper — owned by ``test_events.py``.
  * Scrubber semantics — owned by ``test_events.py`` (and to land in 2/M).
  * Anything Phase 5 (2/M+) (mirror queue, ``cli telemetry``, relay).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from av3.cli.main import _parse_since, cli
from av3.config import load_settings
from av3.telemetry import EventSink, reset_sink


# ----------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolate_sink():
    """Each test gets a clean global sink — the CLI opens its own EventSink so
    the autouse here just guards against leakage from prior modules."""
    reset_sink()
    yield
    reset_sink()


def _seed_events(events_db: Path, rows: list[dict]) -> None:
    """Write events directly via a short-lived sink. Each ``rows`` dict is a
    kwargs bag for :meth:`EventSink.emit`."""
    sink = EventSink(events_db)
    try:
        for kwargs in rows:
            # Allow per-row ts override (parking older events for the --since tests)
            override_ts = kwargs.pop("ts", None)
            row_id = sink.emit(**kwargs)
            if override_ts is not None:
                sink.conn.execute(
                    "UPDATE events SET ts = ? WHERE id = ?",
                    (override_ts, row_id),
                )
    finally:
        sink.close()


def _iso(delta: timedelta) -> str:
    """Build a ts in the same shape ``EventSink.emit`` writes."""
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%S")


def _run(monkeypatch, data_dir: Path, *args: str):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    runner = CliRunner()
    return runner.invoke(cli, list(args))


# ----------------------------------------------------------------- _parse_since

def test_parse_since_none_returns_none():
    assert _parse_since(None) is None


@pytest.mark.parametrize("value", ["30s", "5m", "2h", "7d"])
def test_parse_since_accepts_valid_units(value):
    out = _parse_since(value)
    assert isinstance(out, str)
    # Sink filter compares lexicographically; matches the EventSink.emit ts format.
    datetime.strptime(out, "%Y-%m-%dT%H:%M:%S")


@pytest.mark.parametrize("bad", ["", "x", "5", "0h", "-1m", "5y", "abc", "30 m"])
def test_parse_since_rejects_garbage(bad):
    # click.BadParameter is a UsageError subclass — surfaces to the user as a
    # CLI error rather than a stack trace.
    from click import BadParameter
    with pytest.raises(BadParameter):
        _parse_since(bad)


def test_parse_since_window_is_in_the_past():
    iso = _parse_since("1h")
    cutoff = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (now - cutoff).total_seconds()
    # ~3600s ago, give or take test overhead
    assert 3500 < delta < 3700


# ----------------------------------------------------------------- av3 errors

def test_errors_no_events_friendly_message(tmp_path, monkeypatch):
    result = _run(monkeypatch, tmp_path, "errors")
    assert result.exit_code == 0
    assert "No matching error events." in result.output


def test_errors_renders_table_for_error_rows(tmp_path, monkeypatch):
    settings_path = tmp_path  # AV3_DATA_DIR
    monkeypatch.setenv("AV3_DATA_DIR", str(settings_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "platform": "greenhouse",
         "job_id": "abc123", "error_type": "TimeoutError",
         "error_msg": "selector did not appear in 30s", "run_id": "run-aaa"},
        {"stage": "score", "status": "error", "platform": "lever",
         "job_id": "def456", "error_type": "ValueError",
         "error_msg": "bad score JSON", "run_id": "run-bbb"},
        # An OK row that must NOT appear (errors-only).
        {"stage": "apply", "status": "ok", "platform": "greenhouse",
         "job_id": "ok-1", "duration_ms": 100},
    ])

    result = CliRunner().invoke(cli, ["errors"])
    assert result.exit_code == 0
    out = result.output
    assert "TimeoutError" in out
    assert "ValueError" in out
    assert "selector did not appear in 30s" in out
    assert "ok-1" not in out, "ok rows must not appear in `av3 errors`"
    assert "2 row(s)." in out


def test_errors_filters_by_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    # Distinctive type names — short labels like "T1" collide with ISO "T<HH>" in the
    # ts column and produce false matches.
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "error_type": "ApplyErr", "error_msg": "a"},
        {"stage": "score", "status": "error", "error_type": "ScoreErr", "error_msg": "b"},
    ])

    result = CliRunner().invoke(cli, ["errors", "--stage", "apply"])
    assert result.exit_code == 0
    assert "ApplyErr" in result.output
    assert "ScoreErr" not in result.output


def test_errors_filters_by_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "platform": "greenhouse",
         "error_type": "GhErr", "error_msg": "a"},
        {"stage": "apply", "status": "error", "platform": "lever",
         "error_type": "LeverErr", "error_msg": "b"},
    ])

    result = CliRunner().invoke(cli, ["errors", "--platform", "lever"])
    assert result.exit_code == 0
    assert "LeverErr" in result.output
    assert "GhErr" not in result.output


def test_errors_filters_by_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "run_id": "run-aaa",
         "error_type": "AaaErr", "error_msg": "a"},
        {"stage": "apply", "status": "error", "run_id": "run-bbb",
         "error_type": "BbbErr", "error_msg": "b"},
    ])

    result = CliRunner().invoke(cli, ["errors", "--run-id", "run-aaa"])
    assert result.exit_code == 0
    assert "AaaErr" in result.output
    assert "BbbErr" not in result.output


def test_errors_since_excludes_older_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    old_ts = _iso(-timedelta(days=10))
    fresh_ts = _iso(-timedelta(minutes=5))
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "error_type": "OLD",
         "error_msg": "stale", "ts": old_ts},
        {"stage": "apply", "status": "error", "error_type": "FRESH",
         "error_msg": "recent", "ts": fresh_ts},
    ])

    result = CliRunner().invoke(cli, ["errors", "--since", "1h"])
    assert result.exit_code == 0
    assert "FRESH" in result.output
    assert "OLD" not in result.output


def test_errors_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "error_type": f"E{i}",
         "error_msg": f"err{i}"}
        for i in range(10)
    ])

    result = CliRunner().invoke(cli, ["errors", "--limit", "3"])
    assert result.exit_code == 0
    assert "3 row(s)." in result.output


def test_errors_json_output_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "platform": "greenhouse",
         "job_id": "j1", "error_type": "TimeoutError",
         "error_msg": "no selector", "run_id": "run-1"},
    ])

    result = CliRunner().invoke(cli, ["errors", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert row["stage"] == "apply"
    assert row["error_type"] == "TimeoutError"
    assert row["error_msg"] == "no selector"
    assert row["job_id"] == "j1"
    assert row["platform"] == "greenhouse"
    assert row["run_id"] == "run-1"


def test_errors_bad_since_surfaces_as_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["errors", "--since", "abc"])
    # Click's BadParameter surfaces as exit code 2 ("usage error").
    assert result.exit_code == 2
    assert "unrecognized --since value" in result.output


def test_errors_long_msg_truncated_in_table(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    long_msg = "x" * 500
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "error", "error_type": "T", "error_msg": long_msg},
    ])
    result = CliRunner().invoke(cli, ["errors"])
    assert result.exit_code == 0
    # The text columns are truncated to ~60 chars in the table view; '~' marks it.
    assert "~" in result.output
    # JSON should still carry the full payload — that's the whole point of --json.
    result = CliRunner().invoke(cli, ["errors", "--json"])
    data = json.loads(result.output)
    assert data[0]["error_msg"] == long_msg


# ----------------------------------------------------------------- av3 stats

def test_stats_no_events_friendly_message(tmp_path, monkeypatch):
    result = _run(monkeypatch, tmp_path, "stats")
    assert result.exit_code == 0
    assert "No events in window." in result.output


def test_stats_per_stage_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "ok", "duration_ms": 100},
        {"stage": "apply", "status": "ok", "duration_ms": 300},
        {"stage": "apply", "status": "error", "error_type": "T", "error_msg": "x",
         "duration_ms": 50},
        {"stage": "score", "status": "skip", "duration_ms": 10},
    ])

    result = CliRunner().invoke(cli, ["stats"])
    assert result.exit_code == 0
    out = result.output
    assert "apply" in out
    assert "score" in out
    # Per-stage count quick-check: apply has 2 ok + 1 error; score has 1 skip.
    # The columns are right-aligned ints so we just check the digits land in the line.
    assert "2" in out  # apply.ok
    assert "1" in out  # error / skip


def test_stats_json_output_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "ok", "duration_ms": 100},
        {"stage": "apply", "status": "ok", "duration_ms": 300},
        {"stage": "apply", "status": "error", "error_type": "T", "error_msg": "x",
         "duration_ms": 200},
    ])

    result = CliRunner().invoke(cli, ["stats", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    apply_row = next(r for r in data if r["stage"] == "apply")
    assert apply_row["ok"] == 2
    assert apply_row["error"] == 1
    assert apply_row["skip"] == 0
    # avg_ms is rounded to 1 decimal so consumers don't get a float tail.
    assert apply_row["avg_ms"] == 200.0


def test_stats_since_window_filters_aggregate(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    old_ts = _iso(-timedelta(days=10))
    fresh_ts = _iso(-timedelta(minutes=5))
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "ok", "duration_ms": 100, "ts": old_ts},
        {"stage": "apply", "status": "ok", "duration_ms": 200, "ts": fresh_ts},
    ])

    result = CliRunner().invoke(cli, ["stats", "--since", "1h", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    apply_row = next(r for r in data if r["stage"] == "apply")
    # Only the fresh row counted; aggregate must reflect that.
    assert apply_row["ok"] == 1
    assert apply_row["avg_ms"] == 200.0


def test_stats_filters_by_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "ok", "platform": "greenhouse",
         "duration_ms": 100},
        {"stage": "apply", "status": "ok", "platform": "lever",
         "duration_ms": 200},
    ])

    result = CliRunner().invoke(cli, ["stats", "--platform", "lever", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    apply_row = next(r for r in data if r["stage"] == "apply")
    assert apply_row["ok"] == 1
    assert apply_row["avg_ms"] == 200.0


def test_stats_filters_by_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    settings = load_settings()
    _seed_events(settings.events_db_path, [
        {"stage": "apply", "status": "ok", "run_id": "run-aaa", "duration_ms": 50},
        {"stage": "apply", "status": "ok", "run_id": "run-bbb", "duration_ms": 500},
    ])

    result = CliRunner().invoke(cli, ["stats", "--run-id", "run-aaa", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    apply_row = next(r for r in data if r["stage"] == "apply")
    assert apply_row["ok"] == 1
    assert apply_row["avg_ms"] == 50.0


def test_stats_bad_since_surfaces_as_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["stats", "--since", "1y"])
    assert result.exit_code == 2
    assert "unrecognized --since value" in result.output


# ----------------------------------------------------------------- sink layer

def test_query_errors_composes_filters(tmp_path):
    db_path = tmp_path / "events.db"
    sink = EventSink(db_path)
    try:
        sink.emit(stage="apply", status="error", platform="greenhouse",
                  run_id="r1", error_type="T1", error_msg="a")
        sink.emit(stage="apply", status="error", platform="lever",
                  run_id="r1", error_type="T2", error_msg="b")
        sink.emit(stage="score", status="error", platform="greenhouse",
                  run_id="r2", error_type="T3", error_msg="c")
        # ok row — must not appear in errors-only query
        sink.emit(stage="apply", status="ok", platform="greenhouse",
                  run_id="r1")

        # Single filter
        rows = sink.query_errors(stage="apply")
        assert {r["error_type"] for r in rows} == {"T1", "T2"}

        # Composed
        rows = sink.query_errors(stage="apply", platform="greenhouse", run_id="r1")
        assert {r["error_type"] for r in rows} == {"T1"}

        # No filters
        rows = sink.query_errors()
        assert {r["error_type"] for r in rows} == {"T1", "T2", "T3"}
    finally:
        sink.close()


def test_query_stats_composes_filters(tmp_path):
    db_path = tmp_path / "events.db"
    sink = EventSink(db_path)
    try:
        sink.emit(stage="apply", status="ok", platform="greenhouse",
                  run_id="r1", duration_ms=100)
        sink.emit(stage="apply", status="error", platform="greenhouse",
                  run_id="r1", duration_ms=300, error_type="T", error_msg="x")
        sink.emit(stage="apply", status="ok", platform="lever",
                  run_id="r2", duration_ms=500)

        # No filters — all three counted
        rows = {r["stage"]: r for r in sink.query_stats()}
        assert rows["apply"]["ok"] == 2
        assert rows["apply"]["error"] == 1

        # Platform filter excludes lever
        rows = {r["stage"]: r for r in sink.query_stats(platform="greenhouse")}
        assert rows["apply"]["ok"] == 1
        assert rows["apply"]["error"] == 1

        # Run filter trims to one row
        rows = {r["stage"]: r for r in sink.query_stats(run_id="r2")}
        assert rows["apply"]["ok"] == 1
        assert rows["apply"]["error"] == 0
    finally:
        sink.close()


def test_query_errors_uses_parameterized_sql(tmp_path):
    """Defence-in-depth: a stage value containing SQL fragments must NOT execute."""
    db_path = tmp_path / "events.db"
    sink = EventSink(db_path)
    try:
        sink.emit(stage="apply", status="error", error_type="T", error_msg="a")
        # If string concatenation slipped in, this would either crash or return
        # rows. Parameterized queries return zero matches.
        rows = sink.query_errors(stage="apply'; DROP TABLE events;--")
        assert rows == []
        # And the events table is still around with our row in it.
        remaining = sink.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert remaining == 1
    finally:
        sink.close()
