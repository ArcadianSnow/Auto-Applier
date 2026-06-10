"""Staged-worker scheduler — the production entry for v3.0 (spec §7a).

Where this sits in the architecture:

  filter --once   ┐
  score  --once   ├── were the only entry points pre-(5/M); good for testing
  optimize --once │   one stage in isolation but not for "leave it running."
  apply  --once   ┘

  scheduler.run() ── loops all four workers on a per-cycle cadence, applying
                     quiet-hours masking to the apply stage, with cooperative
                     pause + per-worker isolation. This is what ``av3 run``
                     calls; the --once flags stay as testing / doctor entries.

> "The workers just keep running — 24/7 by default, with optional user-set
>  quiet hours, within rate limits. The app behaves like a quiet background
>  assistant, not a tool you launch per session. v2's separate 'continuous
>  mode' disappears — there is only the always-on loop (a one-shot
>  ``cli run --once`` retained for testing)."

Each cycle drains stages in pipeline order — filter → score → optimize → apply
— so a freshly DISCOVERED job can flow through to QUEUED_APPLY (and, if
acceptable timing, APPLIED) within ONE cycle when the queues are mostly idle.
Order matches the spec §7 pipeline; running stages in any other order would
just delay throughput by a cycle per misordering.

**The apply stage is the only one gated by quiet hours.** Gather stages
(filter / score / optimize) keep running — being wrong in gather is cheap,
doesn't compound, and the user might want results ready when they wake up.
Apply is "act" work per Rule 2.6 (state-changing, blast radius beyond local)
and the user-visible posture during quiet hours is "do not drive my browser
while I sleep."

**Per-worker isolation** is the load-bearing reliability move: a crash in the
filter worker must not stop the score / optimize / apply workers from doing
their job. Each `await worker.run_once()` is wrapped in try/except inside the
cycle so a per-stage failure logs + continues. The CLI's exit code is still
driven by errors observed during cycles (so monitoring catches it), but the
loop itself doesn't die.

**Cooperative pause** is just an injectable predicate. v3.0 ships with a
no-op default; Phase 4 wires F6 / idle-detect to it. Tests use a custom
predicate to verify the loop respects the flag between stages.

What's NOT in this sub-phase:

  * **Per-stage cadences.** Every cycle drains every stage in v3.0 — the
    "filter every 10s, score every minute, apply every 5 min" knobs are a
    v3.1 strategy-profile concern (spec §8a). v3.0 ships one ``cycle_interval_s``
    knob and stops.
  * **Discovery worker.** Discovery still lives in the source adapters and
    runs separately (CLI / cron / Phase 4 dashboard button). Wiring it as
    a scheduler stage is its own slice — it has per-source rate limits, ATS
    rotation policy, etc. that the gather-only workers don't need.
  * **Describe worker for browser boards.** For ATS sources (Greenhouse/Lever/
    Ashby) the JD is populated at discovery; for JobSpy boards (Indeed/Zip)
    it isn't. The score worker fail-closes on empty JD which routes those
    rows to terminal SKIPPED — safe, just lossy. A real describe stage
    arrives alongside browser-board apply wiring (post-Phase 3).
"""

from __future__ import annotations

import asyncio
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from auto_applier.pipeline.apply_worker import ApplyRunSummary, ApplyWorker
from auto_applier.pipeline.discover_worker import DiscoverRunSummary, DiscoverWorker
from auto_applier.pipeline.filter_worker import FilterRunSummary, FilterWorker
from auto_applier.pipeline.optimize_worker import OptimizeRunSummary, OptimizeWorker
from auto_applier.pipeline.quiet_hours import QuietHours, parse_quiet_hours
from auto_applier.pipeline.score_worker import ScoreRunSummary, ScoreWorker
from auto_applier.telemetry import get_sink

__all__ = ["CycleSummary", "MaintenanceHook", "Scheduler", "SchedulerRunSummary"]


class MaintenanceHook(Protocol):
    """Optional scheduler-driven maintenance callback (spec §4 retention +
    backups). Called at most every ``maintenance_interval_s`` seconds at the
    end of a cycle. The implementation is whatever the caller wants — prune,
    backup, or both. Tests inject a recording stub; production wires it to
    :mod:`auto_applier.pipeline.retention`."""

    async def __call__(self) -> None: ...


# --------------------------------------------------------------- protocols

class _Sleep(Protocol):
    """Injectable sleep so tests don't actually wait between cycles."""
    async def __call__(self, seconds: float) -> None: ...


class _Now(Protocol):
    """Injectable wall-clock so tests can drive quiet-hours behavior
    deterministically without monkeypatching datetime."""
    def __call__(self) -> datetime: ...


# --------------------------------------------------------------- per-cycle summary

@dataclass
class CycleSummary:
    """One cycle's outcomes — observable, persisted in the spine via telemetry.

    Each per-stage summary is the worker's own dataclass; the scheduler doesn't
    reshape them. ``apply_skipped_quiet_hours`` indicates the apply stage was
    skipped because of the quiet window (distinguishing it from "apply ran and
    found no work")."""

    cycle: int
    started_at: str
    discover_summary: DiscoverRunSummary | None = None
    filter_summary: FilterRunSummary | None = None
    score_summary: ScoreRunSummary | None = None
    optimize_summary: OptimizeRunSummary | None = None
    apply_summary: ApplyRunSummary | None = None
    apply_skipped_quiet_hours: bool = False
    paused: bool = False
    maintenance_ran: bool = False
    stage_errors: dict[str, str] = field(default_factory=dict)  # stage -> error str
    elapsed_s: float = 0.0


@dataclass
class SchedulerRunSummary:
    """One ``run()`` invocation — N cycles. Aggregates per-cycle errors so the
    CLI can decide its exit code without inspecting every CycleSummary."""

    cycles: list[CycleSummary] = field(default_factory=list)
    total_errors: int = 0
    elapsed_s: float = 0.0


# --------------------------------------------------------------- the scheduler

class Scheduler:
    """The production always-on loop.

    Constructor takes pre-built workers (the CLI builds them once and shares
    the LLM / embed clients across workers). The scheduler doesn't own the
    BrowserSession that the apply worker reads — that lifecycle is the CLI's
    responsibility, same as ``av3 apply``.

    Tests inject:
      * ``sleep`` → ``asyncio.sleep`` replacement (skip real time)
      * ``now`` → wall-clock for quiet-hours assertions
      * ``pause_predicate`` → cooperative pause for "F6-pressed-mid-cycle" tests
    """

    def __init__(
        self,
        *,
        filter_worker: FilterWorker,
        score_worker: ScoreWorker,
        optimize_worker: OptimizeWorker,
        apply_worker: ApplyWorker,
        discover_worker: DiscoverWorker | None = None,
        cycle_interval_s: float = 60.0,
        quiet_hours: QuietHours | None = None,
        pause_predicate: Callable[[], bool] | None = None,
        maintenance: MaintenanceHook | None = None,
        maintenance_interval_s: float = 3600.0,
        sleep: _Sleep | None = None,
        now: _Now | None = None,
    ):
        # Discovery is the optional HEAD of the pipeline. When present it runs FIRST
        # each cycle (a gather stage — never quiet-gated), so freshly discovered jobs
        # can flow filter→score→optimize→apply within the same cycle. When None the
        # scheduler drains whatever discovery seeded out-of-band (back-compat).
        self._discover = discover_worker
        self._filter = filter_worker
        self._score = score_worker
        self._optimize = optimize_worker
        self._apply = apply_worker
        self._cycle_interval_s = cycle_interval_s
        self._quiet_hours = quiet_hours or parse_quiet_hours(None)
        self._pause_predicate = pause_predicate or (lambda: False)
        self._maintenance = maintenance
        self._maintenance_interval_s = maintenance_interval_s
        # Monotonic time stamp of last maintenance run. None means "never";
        # the first eligible cycle after startup triggers maintenance.
        self._last_maintenance_at: float | None = None
        self._sleep: _Sleep = sleep or asyncio.sleep
        self._now: _Now = now or datetime.now

    # -- public -----------------------------------------------------------

    async def run(self, *, max_cycles: int | None = None) -> SchedulerRunSummary:
        """Drive the staged loop. ``max_cycles=None`` runs forever (cancel via
        ``KeyboardInterrupt`` or task cancellation); ``max_cycles=N`` exits after
        N cycles (used for testing + bounded cron usage)."""
        summary = SchedulerRunSummary()
        t0 = time_mod.perf_counter()
        cycle_idx = 0

        try:
            while max_cycles is None or cycle_idx < max_cycles:
                cycle_idx += 1
                cycle_summary = await self._run_cycle(cycle_idx)
                summary.cycles.append(cycle_summary)
                summary.total_errors += len(cycle_summary.stage_errors)
                # Sleep between cycles. A test sleep replacement makes this a no-op.
                # We sleep even on the last cycle for backpressure consistency if
                # max_cycles is None / a large N; tests don't pay this cost.
                await self._sleep(self._cycle_interval_s)
        finally:
            summary.elapsed_s = time_mod.perf_counter() - t0
        return summary

    # -- per-cycle --------------------------------------------------------

    async def _run_cycle(self, cycle_idx: int) -> CycleSummary:
        from auto_applier.domain.models import utcnow_iso

        cs = CycleSummary(cycle=cycle_idx, started_at=utcnow_iso())
        t0 = time_mod.perf_counter()

        # Emit a 'scheduler' start event so the spine records cycle boundaries
        # (useful for "what happened between 03:00 and 03:01?" queries against
        # events.db). Per-stage @stage events still fire inside each worker.
        self._emit_cycle("start", cycle_idx, context={"interval_s": self._cycle_interval_s})

        # Cooperative pause — check ONCE per cycle. Phase 4 wires this to F6/idle;
        # v3.0 just exposes the predicate. We don't check between stages because
        # one cycle of gather work is cheap and a mid-cycle pause complicates the
        # event log without adding meaningful responsiveness.
        if self._pause_predicate():
            cs.paused = True
            self._emit_cycle("skip", cycle_idx, context={"reason": "paused"})
            cs.elapsed_s = time_mod.perf_counter() - t0
            return cs

        # Stages run in pipeline order. Each stage is isolated — a crash in one
        # stage logs + continues so the rest of the cycle still makes progress.
        # Discovery (when wired) leads: it's a gather stage, never quiet-gated.
        if self._discover is not None:
            await self._run_stage(cs, "discover", self._discover)
        await self._run_stage(cs, "filter", self._filter)
        await self._run_stage(cs, "score", self._score)
        await self._run_stage(cs, "optimize", self._optimize)

        # Apply stage is the only one gated by quiet hours.
        now_local = self._now()
        if self._quiet_hours.is_quiet(now_local):
            cs.apply_skipped_quiet_hours = True
            self._emit_cycle(
                "skip", cycle_idx,
                context={
                    "stage": "apply",
                    "reason": "quiet_hours",
                    "window": self._quiet_hours.raw,
                },
            )
        else:
            await self._run_stage(cs, "apply", self._apply)

        # Maintenance hook (spec §4 retention + backups). Runs at most every
        # maintenance_interval_s seconds — checked at the END of each cycle so
        # apply / score have already drained, minimizing contention with the
        # backup snapshot (which uses the SQLite online backup API and is safe
        # but still pays a write-lock briefly).
        if self._maintenance is not None and self._is_maintenance_due():
            try:
                await self._maintenance()
                cs.maintenance_ran = True
                self._last_maintenance_at = time_mod.monotonic()
                self._emit_cycle("ok", cycle_idx, context={"event": "maintenance"})
            except Exception as exc:  # noqa: BLE001
                cs.stage_errors["maintenance"] = f"{type(exc).__name__}: {exc}"
                self._emit_cycle(
                    "error", cycle_idx,
                    context={"event": "maintenance", "error": str(exc)},
                )

        cs.elapsed_s = time_mod.perf_counter() - t0
        status = "error" if cs.stage_errors else "ok"
        self._emit_cycle(
            status, cycle_idx,
            duration_ms=int(cs.elapsed_s * 1000),
            context={"errors": list(cs.stage_errors.keys())} if cs.stage_errors else None,
        )
        return cs

    def _is_maintenance_due(self) -> bool:
        """True iff the maintenance hook hasn't run for
        ``maintenance_interval_s`` seconds. First call after startup is
        always due so cold starts don't silently skip prune+backup if the
        process restarts inside the interval."""
        if self._last_maintenance_at is None:
            return True
        elapsed = time_mod.monotonic() - self._last_maintenance_at
        return elapsed >= self._maintenance_interval_s

    async def _run_stage(
        self,
        cycle: CycleSummary,
        stage: str,
        worker: DiscoverWorker | FilterWorker | ScoreWorker | OptimizeWorker | ApplyWorker,
    ) -> None:
        """Invoke one worker's ``run_once``, isolating any exception. The
        worker's own ``@stage`` decorator already emits per-job events; we
        only catch the *outer* call so a constructor-time / pre-loop failure
        in the worker doesn't kill the cycle."""
        try:
            result = await worker.run_once()
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            cycle.stage_errors[stage] = f"{type(exc).__name__}: {exc}"
            return

        # Stash the per-stage summary into the cycle for the dashboard / CLI line.
        if stage == "discover":
            cycle.discover_summary = result  # type: ignore[assignment]
        elif stage == "filter":
            cycle.filter_summary = result  # type: ignore[assignment]
        elif stage == "score":
            cycle.score_summary = result  # type: ignore[assignment]
        elif stage == "optimize":
            cycle.optimize_summary = result  # type: ignore[assignment]
        elif stage == "apply":
            cycle.apply_summary = result  # type: ignore[assignment]

    # -- telemetry --------------------------------------------------------

    def _emit_cycle(
        self,
        status: str,
        cycle_idx: int,
        *,
        duration_ms: int | None = None,
        context: dict | None = None,
    ) -> None:
        """Emit a ``scheduler`` event marking the cycle boundary. Drops
        silently when no sink is configured (unit tests that don't construct
        one). Per-stage events are still emitted by the workers themselves."""
        sink = get_sink()
        if sink is None:
            return
        sink.emit(
            stage="scheduler",
            status=status,
            duration_ms=duration_ms,
            context={"cycle": cycle_idx, **(context or {})},
        )
