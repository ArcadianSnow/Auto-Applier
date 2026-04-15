"""Tests for ApplicationEngine.run_continuous().

The continuous loop is stubbed — we don't actually spin up a browser
or LLM. Instead we patch ``run()`` and ``start()/stop()`` to count
calls and verify the loop's control flow: max-cycles, stop flags,
active-hours gating, event emission.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from auto_applier.orchestrator.engine import ApplicationEngine
from auto_applier.orchestrator.events import (
    CONTINUOUS_FINISHED,
    CYCLE_IDLE,
    CYCLE_STARTED,
    EventEmitter,
)


def _engine(cfg: dict | None = None) -> ApplicationEngine:
    base = {
        "enabled_platforms": [],
        "search_keywords": [],
        "location": "",
        "personal_info": {},
        "continuous_cycle_delay_min": 60,
        "continuous_cycle_delay_max": 60,
        "continuous_active_hours": "",  # always-on
        "continuous_max_cycles": 2,
    }
    if cfg:
        base.update(cfg)
    return ApplicationEngine(base, EventEmitter(), cli_mode=True)


def _record_events(engine: ApplicationEngine, names: list[str]) -> list[tuple]:
    collected: list[tuple] = []
    for name in names:
        engine.events.on(
            name,
            lambda _name=name, **kw: collected.append((_name, kw)),
        )
    return collected


def _patched_engine(engine: ApplicationEngine):
    """Patch start/stop/run so we don't launch a real browser."""
    engine.start = AsyncMock()
    engine.stop = AsyncMock()
    engine.run = AsyncMock()
    # Make sleep a no-op so cycle delays don't hang the test.
    engine._sleep_with_stop_check = AsyncMock(return_value=True)


class TestMaxCycles:
    def test_stops_after_max_cycles(self):
        engine = _engine({"continuous_max_cycles": 3})
        _patched_engine(engine)
        events = _record_events(engine, [CYCLE_STARTED, CONTINUOUS_FINISHED])

        asyncio.run(engine.run_continuous())

        starts = [e for e in events if e[0] == CYCLE_STARTED]
        assert len(starts) == 3
        finished = [e for e in events if e[0] == CONTINUOUS_FINISHED]
        assert len(finished) == 1
        assert finished[0][1]["total_cycles"] == 3
        assert "max_cycles" in finished[0][1]["reason"]

    def test_zero_max_cycles_is_unlimited(self):
        """Sanity check: max_cycles=0 doesn't short-circuit immediately."""
        engine = _engine({"continuous_max_cycles": 0})
        _patched_engine(engine)
        # Stop the loop after 2 cycles via the stop flag in a handler.
        count = {"n": 0}

        def _stop_after_two(**_kw):
            count["n"] += 1
            if count["n"] >= 2:
                engine.request_stop()

        engine.events.on(CYCLE_STARTED, _stop_after_two)
        asyncio.run(engine.run_continuous())
        assert count["n"] == 2


class TestStopFlags:
    def test_request_stop_breaks_loop(self):
        engine = _engine({"continuous_max_cycles": 10})
        _patched_engine(engine)

        def _stop_on_first(**_kw):
            engine.request_stop()

        engine.events.on(CYCLE_STARTED, _stop_on_first)
        events = _record_events(engine, [CONTINUOUS_FINISHED])
        asyncio.run(engine.run_continuous())
        assert events[0][1]["total_cycles"] == 1
        assert "Stopped" in events[0][1]["reason"]

    def test_stop_after_cycle_finishes_current(self):
        engine = _engine({"continuous_max_cycles": 10})
        _patched_engine(engine)

        def _soft_stop(**_kw):
            engine.request_stop_after_cycle()

        engine.events.on(CYCLE_STARTED, _soft_stop)
        events = _record_events(engine, [CONTINUOUS_FINISHED])
        asyncio.run(engine.run_continuous())
        # The cycle that was running when soft-stop fired still counts.
        assert events[0][1]["total_cycles"] == 1


class TestActiveHoursGate:
    def test_outside_window_emits_idle_with_refinement_flag(self):
        """Outside the active-hours window, the loop must emit
        CYCLE_IDLE(refinement_only=True) instead of starting a cycle.

        We stub parse_active_hours so the window is always "closed"
        regardless of when the test actually runs.
        """
        engine = _engine({"continuous_max_cycles": 1})
        _patched_engine(engine)
        events = _record_events(engine, [CYCLE_IDLE, CYCLE_STARTED])

        class _AlwaysClosed:
            raw = "test-closed"
            def is_active(self, _now): return False
            def seconds_until_open(self, _now): return 3600

        def _stop_after_idle(**_kw):
            engine.request_stop()
        engine.events.on(CYCLE_IDLE, _stop_after_idle)

        import auto_applier.orchestrator.active_hours as ah_mod
        original = ah_mod.parse_active_hours
        ah_mod.parse_active_hours = lambda _raw: _AlwaysClosed()
        try:
            asyncio.run(engine.run_continuous())
        finally:
            ah_mod.parse_active_hours = original

        idle_events = [e for e in events if e[0] == CYCLE_IDLE]
        start_events = [e for e in events if e[0] == CYCLE_STARTED]
        assert idle_events, "expected at least one CYCLE_IDLE"
        assert idle_events[0][1]["refinement_only"] is True
        assert idle_events[0][1]["seconds_until_next"] > 0
        assert not start_events, "no cycle should start outside the window"


class TestBrowserLifecycle:
    def test_start_called_once_stop_called_once(self):
        """Continuous mode must reuse the browser across cycles, so
        start() fires once at the top and stop() fires once at the end."""
        engine = _engine({"continuous_max_cycles": 3})
        _patched_engine(engine)

        asyncio.run(engine.run_continuous())

        assert engine.start.call_count == 1
        assert engine.stop.call_count == 1
        # run() is called per cycle
        assert engine.run.call_count == 3
