"""Scheduler (spec section 7a) — contract tests for the staged-worker loop.

Fakes the four workers entirely (each is a tiny class with an ``async run_once``
that records its kwargs and increments a counter). Lets the tests stay focused
on the scheduler's contract: cycle order, max_cycles bound, quiet-hours
gating, per-worker isolation, cooperative pause, telemetry.

Coverage:
  * One cycle drains filter -> score -> optimize -> apply in pipeline order.
  * ``max_cycles=N`` exits after N cycles.
  * ``cycle_interval_s`` is honored (injected sleep records each call).
  * Quiet hours: apply is skipped; gather stages still run; cycle records the flag.
  * Outside quiet hours: apply runs normally.
  * Cooperative pause: predicate returning True skips the cycle entirely.
  * Per-worker isolation: a filter exception doesn't block score/optimize/apply.
  * Per-worker error rolls into total_errors + cycle.stage_errors.
  * Telemetry: a 'scheduler' event is emitted on each cycle boundary.
  * Sleep is called once per cycle regardless of pause / quiet hours.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from auto_applier.inbox.worker import InboxRunSummary
from auto_applier.pipeline.apply_worker import ApplyRunSummary
from auto_applier.pipeline.filter_worker import FilterRunSummary
from auto_applier.pipeline.optimize_worker import OptimizeRunSummary
from auto_applier.pipeline.quiet_hours import parse_quiet_hours
from auto_applier.pipeline.scheduler import Scheduler
from auto_applier.pipeline.score_worker import ScoreRunSummary


# --------------------------------------------------------------- fakes

class _FakeWorker:
    """One stand-in for every worker. ``call_log`` is the SHARED order log so
    tests can assert pipeline order across all four workers. Each instance has
    a ``label`` for the log and an optional ``raise_with`` to simulate crashes."""

    def __init__(self, label: str, summary_factory, call_log: list[str]):
        self.label = label
        self._summary_factory = summary_factory
        self._call_log = call_log
        self.call_count = 0
        self.raise_with: Exception | None = None

    async def run_once(self) -> Any:
        self.call_count += 1
        self._call_log.append(self.label)
        if self.raise_with is not None:
            raise self.raise_with
        return self._summary_factory(self.call_count)


def _make_fakes() -> tuple[list[str], _FakeWorker, _FakeWorker, _FakeWorker, _FakeWorker]:
    """Build the four fakes + the shared call log."""
    call_log: list[str] = []
    f = _FakeWorker("filter", lambda i: FilterRunSummary(run_id=f"f-{i}"), call_log)
    s = _FakeWorker("score", lambda i: ScoreRunSummary(run_id=f"s-{i}"), call_log)
    o = _FakeWorker("optimize", lambda i: OptimizeRunSummary(run_id=f"o-{i}"), call_log)
    a = _FakeWorker("apply", lambda i: ApplyRunSummary(run_id=f"a-{i}"), call_log)
    return call_log, f, s, o, a


@dataclass
class _SleepRecorder:
    """Captures the sleep durations the scheduler requested without actually
    sleeping. Tests use this to assert "scheduler asked for sleep N times at
    interval X" without dragging real time into the suite."""

    calls: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _build_scheduler(
    *,
    cycle_interval_s: float = 1.0,
    quiet_hours_raw: str | None = None,
    pause_predicate=None,
    now_value: datetime | None = None,
):
    """Helper to wire a Scheduler with fakes + injected sleep/now."""
    call_log, f, s, o, a = _make_fakes()
    sleep = _SleepRecorder()
    now = (lambda: now_value or datetime(2026, 5, 29, 12, 0))
    scheduler = Scheduler(
        filter_worker=f,                        # type: ignore[arg-type]
        score_worker=s,                         # type: ignore[arg-type]
        optimize_worker=o,                      # type: ignore[arg-type]
        apply_worker=a,                         # type: ignore[arg-type]
        cycle_interval_s=cycle_interval_s,
        quiet_hours=parse_quiet_hours(quiet_hours_raw),
        pause_predicate=pause_predicate,
        sleep=sleep,
        now=now,
    )
    return scheduler, call_log, f, s, o, a, sleep


# --------------------------------------------------------------- order + bounding

def test_one_cycle_drains_in_pipeline_order():
    scheduler, call_log, f, s, o, a, _ = _build_scheduler()
    asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == ["filter", "score", "optimize", "apply"]
    assert f.call_count == 1
    assert s.call_count == 1
    assert o.call_count == 1
    assert a.call_count == 1


def test_max_cycles_bounds_the_loop():
    scheduler, call_log, f, s, o, a, _ = _build_scheduler()
    summary = asyncio.run(scheduler.run(max_cycles=3))
    assert len(summary.cycles) == 3
    # Each cycle drains all four workers.
    assert f.call_count == 3
    assert s.call_count == 3
    assert o.call_count == 3
    assert a.call_count == 3
    # Order across cycles preserves pipeline order.
    assert call_log == ["filter", "score", "optimize", "apply"] * 3


def test_cycle_interval_passed_to_sleep_each_iteration():
    scheduler, _, _, _, _, _, sleep = _build_scheduler(cycle_interval_s=2.5)
    asyncio.run(scheduler.run(max_cycles=3))
    # One sleep call per cycle, every call gets the configured interval.
    assert sleep.calls == [2.5, 2.5, 2.5]


# --------------------------------------------------------------- quiet hours

def test_quiet_hours_skip_apply_only():
    """Apply is gated by quiet hours; filter/score/optimize keep running."""
    scheduler, call_log, _, _, _, a, _ = _build_scheduler(
        quiet_hours_raw="12:00-14:00",
        now_value=datetime(2026, 5, 29, 13, 0),  # inside the window
    )
    summary = asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == ["filter", "score", "optimize"]
    assert a.call_count == 0
    assert summary.cycles[0].apply_skipped_quiet_hours is True


def test_outside_quiet_hours_apply_runs_normally():
    scheduler, call_log, _, _, _, a, _ = _build_scheduler(
        quiet_hours_raw="22:00-08:00",
        now_value=datetime(2026, 5, 29, 12, 0),  # outside the window
    )
    summary = asyncio.run(scheduler.run(max_cycles=1))
    assert "apply" in call_log
    assert a.call_count == 1
    assert summary.cycles[0].apply_skipped_quiet_hours is False


def test_no_quiet_hours_apply_always_runs():
    """No window configured -> apply runs every cycle regardless of wall time."""
    scheduler, call_log, _, _, _, a, _ = _build_scheduler(quiet_hours_raw=None)
    asyncio.run(scheduler.run(max_cycles=2))
    assert call_log.count("apply") == 2
    assert a.call_count == 2


# --------------------------------------------------------------- cooperative pause

def test_pause_predicate_skips_cycle_entirely():
    """A pause predicate returning True causes the whole cycle to skip — no
    workers run, but the cycle is recorded (and sleep still fires for backpressure)."""
    pause_flag = {"on": False}
    scheduler, call_log, f, s, o, a, sleep = _build_scheduler(
        pause_predicate=lambda: pause_flag["on"],
    )

    # First cycle: paused.
    pause_flag["on"] = True
    asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == []
    assert f.call_count == s.call_count == o.call_count == a.call_count == 0
    assert len(sleep.calls) == 1  # sleep STILL fires (backpressure invariant)


def test_pause_predicate_off_runs_cycle_normally():
    scheduler, call_log, _, _, _, _, _ = _build_scheduler(
        pause_predicate=lambda: False,
    )
    asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == ["filter", "score", "optimize", "apply"]


# --------------------------------------------------------------- isolation

def test_filter_exception_isolated_other_stages_still_run():
    """A crash in the filter worker MUST NOT block score/optimize/apply.
    Filter's error is recorded; the other three still execute."""
    scheduler, call_log, f, s, o, a, _ = _build_scheduler()
    f.raise_with = RuntimeError("simulated filter crash")

    summary = asyncio.run(scheduler.run(max_cycles=1))

    assert f.call_count == 1
    assert s.call_count == 1
    assert o.call_count == 1
    assert a.call_count == 1
    assert call_log == ["filter", "score", "optimize", "apply"]
    assert "filter" in summary.cycles[0].stage_errors
    assert "simulated filter crash" in summary.cycles[0].stage_errors["filter"]
    assert summary.total_errors == 1


def test_apply_exception_does_not_kill_loop():
    """An apply crash on cycle 1 must not prevent cycle 2 from running."""
    scheduler, call_log, _, _, _, a, _ = _build_scheduler()
    a.raise_with = RuntimeError("simulated apply crash")

    summary = asyncio.run(scheduler.run(max_cycles=2))

    assert a.call_count == 2  # still tried in cycle 2
    assert summary.total_errors == 2  # one per cycle


def test_multiple_stage_errors_all_recorded_per_cycle():
    scheduler, _, f, _, o, _, _ = _build_scheduler()
    f.raise_with = RuntimeError("f-crash")
    o.raise_with = RuntimeError("o-crash")

    summary = asyncio.run(scheduler.run(max_cycles=1))

    assert set(summary.cycles[0].stage_errors.keys()) == {"filter", "optimize"}
    assert summary.total_errors == 2


# --------------------------------------------------------------- per-stage summaries

def test_cycle_records_each_worker_summary():
    """Successful runs stash the worker's own summary on the cycle for the
    dashboard. Tests that the dashboard's data flow works end-to-end."""
    scheduler, _, _, _, _, _, _ = _build_scheduler()
    summary = asyncio.run(scheduler.run(max_cycles=1))

    cs = summary.cycles[0]
    assert cs.filter_summary is not None
    assert cs.score_summary is not None
    assert cs.optimize_summary is not None
    assert cs.apply_summary is not None
    assert cs.filter_summary.run_id == "f-1"


def test_cycle_summary_no_apply_when_quiet():
    """Quiet hours leave apply_summary as None — the dashboard can render the
    quiet badge from the apply_skipped_quiet_hours flag without confusing it
    with 'apply ran and found nothing'."""
    scheduler, _, _, _, _, _, _ = _build_scheduler(
        quiet_hours_raw="12:00-14:00",
        now_value=datetime(2026, 5, 29, 13, 0),
    )
    summary = asyncio.run(scheduler.run(max_cycles=1))
    cs = summary.cycles[0]
    assert cs.apply_summary is None
    assert cs.apply_skipped_quiet_hours is True


# --------------------------------------------------------------- telemetry

def test_telemetry_emits_scheduler_event_per_cycle(sink):
    """Each cycle emits a 'scheduler' start + end event so the spine records
    cycle boundaries. Useful for 'what happened between 03:00 and 03:01?'
    queries against events.db."""
    scheduler, _, _, _, _, _, _ = _build_scheduler()
    asyncio.run(scheduler.run(max_cycles=2))

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "scheduler"]
    # At minimum: start + ok for each of 2 cycles.
    statuses = [r["status"] for r in rows]
    assert statuses.count("start") == 2
    assert statuses.count("ok") == 2


def test_telemetry_records_quiet_skip(sink):
    """A quiet-hours skip emits a 'skip' event so the dashboard can show
    'apply was paused 04:00-05:00 last night'."""
    scheduler, _, _, _, _, _, _ = _build_scheduler(
        quiet_hours_raw="12:00-14:00",
        now_value=datetime(2026, 5, 29, 13, 0),
    )
    asyncio.run(scheduler.run(max_cycles=1))

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "scheduler"]
    statuses = [r["status"] for r in rows]
    assert "skip" in statuses


def test_telemetry_records_paused(sink):
    """Pause-predicate cycles emit a 'skip' with reason='paused'."""
    import json as _json

    scheduler, _, _, _, _, _, _ = _build_scheduler(pause_predicate=lambda: True)
    asyncio.run(scheduler.run(max_cycles=1))

    rows = [r for r in sink.recent(limit=20) if r["stage"] == "scheduler"]
    skip_rows = [r for r in rows if r["status"] == "skip"]
    assert len(skip_rows) >= 1
    ctx = _json.loads(skip_rows[0]["context_json"] or "{}")
    assert ctx.get("reason") == "paused"


# --------------------------------------------------------------- inbox stage (Direction 4)

def _build_scheduler_with_inbox(*, quiet_hours_raw=None, now_value=None):
    """A scheduler wired with the four fakes PLUS an inbox fake (shared call log)."""
    call_log, f, s, o, a = _make_fakes()
    inbox = _FakeWorker("inbox", lambda i: InboxRunSummary(run_id=f"i-{i}"), call_log)
    sleep = _SleepRecorder()
    now = (lambda: now_value or datetime(2026, 5, 29, 12, 0))
    scheduler = Scheduler(
        filter_worker=f,                        # type: ignore[arg-type]
        score_worker=s,                         # type: ignore[arg-type]
        optimize_worker=o,                      # type: ignore[arg-type]
        apply_worker=a,                         # type: ignore[arg-type]
        inbox_worker=inbox,                     # type: ignore[arg-type]
        cycle_interval_s=1.0,
        quiet_hours=parse_quiet_hours(quiet_hours_raw),
        sleep=sleep,
        now=now,
    )
    return scheduler, call_log, inbox, a


def test_inbox_stage_runs_after_optimize_before_apply():
    scheduler, call_log, inbox, _ = _build_scheduler_with_inbox()
    summary = asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == ["filter", "score", "optimize", "inbox", "apply"]
    assert inbox.call_count == 1
    cs = summary.cycles[0]
    assert cs.inbox_summary is not None
    assert cs.inbox_summary.run_id == "i-1"


def test_inbox_stage_runs_during_quiet_hours():
    """Inbox is GATHER (reading mail doesn't drive the browser): quiet hours pause
    apply but never the inbox poll."""
    scheduler, call_log, inbox, a = _build_scheduler_with_inbox(
        quiet_hours_raw="12:00-14:00", now_value=datetime(2026, 5, 29, 13, 0),
    )
    asyncio.run(scheduler.run(max_cycles=1))
    assert call_log == ["filter", "score", "optimize", "inbox"]  # apply skipped, inbox not
    assert inbox.call_count == 1
    assert a.call_count == 0


def test_no_inbox_worker_means_no_inbox_stage():
    """Absent inbox config -> the stage simply doesn't appear (back-compat)."""
    scheduler, call_log, _, _, _, a, _ = _build_scheduler()
    asyncio.run(scheduler.run(max_cycles=1))
    assert "inbox" not in call_log
    assert a.call_count == 1


def test_inbox_stage_exception_isolated():
    """An inbox-poll crash must not block apply; the error is recorded per cycle."""
    scheduler, call_log, inbox, a = _build_scheduler_with_inbox()
    inbox.raise_with = RuntimeError("imap blew up")
    summary = asyncio.run(scheduler.run(max_cycles=1))
    assert "apply" in call_log
    assert a.call_count == 1
    assert "inbox" in summary.cycles[0].stage_errors
    assert summary.total_errors == 1
