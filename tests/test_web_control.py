"""Phase 4 (3/M) — control handoff tests.

Coverage:
  * ControlState union: pause/resume/toggle, multi-source OR, reasons
  * SchedulerService legacy + (3/M) signatures + predicate liveness
  * /api/control/pause + /api/control/resume endpoint contracts
  * /api/status surfaces pause reasons
  * HotkeyWatcher cross-platform soft-fail + smoke against the toggle path
  * IdleWatcher state-machine via injected idle reader (no Win32 required)
  * Lifespan starts + stops watchers alongside the service

The hotkey + idle watchers are tested through their *injected* seams (a
test-only ``read_idle_seconds`` for IdleWatcher; the build_hotkey_toggle
helper unit-tested in isolation for the F6 path). End-to-end Win32 hooks
require a desktop session; that's covered by manual smoketests, not unit
tests.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from auto_applier.config import Settings, WebConfig
from auto_applier.web import SchedulerService, WebState, create_app
from auto_applier.web.control import (
    SOURCE_HOTKEY,
    SOURCE_IDLE,
    SOURCE_MANUAL,
    ControlState,
    ManualTakeover,
)
from auto_applier.web.hotkey import HotkeyWatcher, build_hotkey_toggle
from auto_applier.web.idle import IdleWatcher
from auto_applier.web.service import sync_factory


# --------------------------------------------------------------- ManualTakeover

class TestManualTakeover:
    def test_inactive_when_empty(self):
        assert ManualTakeover().is_active() is False

    def test_engage_then_release(self):
        t = ManualTakeover()
        token = t.engage()
        assert t.is_active() is True
        assert t.count == 1
        t.release(token)
        assert t.is_active() is False
        assert t.count == 0

    def test_release_is_idempotent(self):
        t = ManualTakeover()
        token = t.engage()
        t.release(token)
        t.release(token)  # second release is a no-op, never raises
        assert t.is_active() is False

    def test_multiple_takeovers_keep_apply_masked_until_last_releases(self):
        t = ManualTakeover()
        a, b = t.engage(), t.engage()
        assert t.count == 2
        t.release(a)
        assert t.is_active() is True   # b still open
        t.release(b)
        assert t.is_active() is False

    def test_safety_timeout_auto_releases_stale_takeover(self):
        clock = [1000.0]
        t = ManualTakeover(timeout_s=900.0, now=lambda: clock[0])
        t.engage()
        assert t.is_active() is True
        clock[0] += 901.0            # past the safety window
        assert t.is_active() is False
        assert t.count == 0          # pruned as a side effect


# --------------------------------------------------------------- ControlState

class TestControlState:
    """The pause-source union — every mutator + read path."""

    def test_starts_running(self):
        cs = ControlState()
        assert cs.is_paused() is False
        snap = cs.snapshot()
        assert snap.paused is False
        assert snap.reasons == {}

    def test_pause_single_source_with_default_reason(self):
        cs = ControlState()
        snap = cs.pause(SOURCE_MANUAL)
        # The default reason for `manual` is rendered to the dashboard.
        assert snap.paused is True
        assert snap.reasons == {"manual": "paused from dashboard"}
        assert cs.is_paused() is True

    def test_pause_carries_custom_reason(self):
        cs = ControlState()
        snap = cs.pause(SOURCE_HOTKEY, reason="F6 pressed at 12:34:56")
        assert snap.reasons["hotkey"] == "F6 pressed at 12:34:56"

    def test_resume_clears_just_one_source(self):
        cs = ControlState()
        cs.pause(SOURCE_MANUAL)
        cs.pause(SOURCE_HOTKEY)
        cs.resume(SOURCE_MANUAL)
        # The OR is still True because hotkey is still pausing.
        assert cs.is_paused() is True
        assert "hotkey" in cs.snapshot().reasons
        assert "manual" not in cs.snapshot().reasons

    def test_resume_is_idempotent(self):
        cs = ControlState()
        # Resuming a source that was never paused must not raise — common
        # in the IdleWatcher loop on the first tick.
        snap = cs.resume(SOURCE_IDLE)
        assert snap.paused is False
        # Repeated resume is fine.
        cs.resume(SOURCE_IDLE)

    def test_pause_is_idempotent_last_reason_wins(self):
        cs = ControlState()
        cs.pause(SOURCE_IDLE, reason="user active 1s ago")
        cs.pause(SOURCE_IDLE, reason="user active 2s ago")
        # Last reason wins so the dashboard always shows the freshest.
        assert cs.snapshot().reasons["idle"] == "user active 2s ago"

    def test_toggle_flips_source(self):
        cs = ControlState()
        snap = cs.toggle(SOURCE_HOTKEY)
        assert snap.paused is True
        assert "hotkey" in snap.reasons
        snap = cs.toggle(SOURCE_HOTKEY)
        assert snap.paused is False
        assert snap.reasons == {}

    def test_multi_source_union(self):
        """All three sources OR together — clearing only manual leaves the
        scheduler still paused by hotkey + idle."""
        cs = ControlState()
        cs.pause(SOURCE_MANUAL)
        cs.pause(SOURCE_HOTKEY)
        cs.pause(SOURCE_IDLE, reason="user active 0s ago")
        assert cs.is_paused() is True
        cs.resume(SOURCE_MANUAL)
        assert cs.is_paused() is True
        cs.resume(SOURCE_HOTKEY)
        assert cs.is_paused() is True
        cs.resume(SOURCE_IDLE)
        assert cs.is_paused() is False

    def test_unknown_source_rejected(self):
        cs = ControlState()
        with pytest.raises(ValueError):
            cs.pause("typo")  # not in VALID_SOURCES
        with pytest.raises(ValueError):
            cs.resume("typo")
        with pytest.raises(ValueError):
            cs.toggle("typo")

    def test_snapshot_is_immutable_copy(self):
        """Callers shouldn't be able to mutate ControlState by editing the
        snapshot's reasons dict — that would be a thread-safety footgun."""
        cs = ControlState()
        cs.pause(SOURCE_MANUAL)
        snap = cs.snapshot()
        snap.reasons["hotkey"] = "should not stick"
        assert "hotkey" not in cs.snapshot().reasons

    def test_thread_safety_smoke(self):
        """Hammer the union from N threads. The point isn't perfect
        ordering — it's that no exception leaks and the final state is
        consistent. The lock ensures atomic mutations even under
        contention."""
        cs = ControlState()

        def hammer(source: str):
            for _ in range(200):
                cs.toggle(source)

        # Three distinct threads per source — 9 total. Build them with a
        # comprehension so each is a fresh Thread instance.
        threads = [
            threading.Thread(target=hammer, args=(s,))
            for s in (SOURCE_MANUAL, SOURCE_HOTKEY, SOURCE_IDLE)
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        # The predicate must always agree with the dict — regardless of
        # which side won each race, the snapshot is internally consistent.
        snap = cs.snapshot()
        assert snap.paused == bool(snap.reasons)


# --------------------------------------------------------------- SchedulerService

class _StubScheduler:
    def __init__(self, pause_predicate):
        self.pause_predicate = pause_predicate
        self.run_called = False

    async def run(self, max_cycles=None):
        self.run_called = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


def _make_stub_service(control: ControlState | None = None):
    holder = {}

    def _build(pause_predicate):
        sched = _StubScheduler(pause_predicate)
        holder["scheduler"] = sched
        return sched

    return SchedulerService(sync_factory(_build), control=control), holder


class TestSchedulerServiceControl:
    """Service mutators flow through to the predicate + snapshot."""

    def test_legacy_zero_arg_pause_uses_manual_source(self):
        service, _ = _make_stub_service()
        service.pause()
        assert service.is_paused is True
        assert "manual" in service.snapshot()["pause_reasons"]
        service.resume()
        assert service.is_paused is False

    def test_pause_with_explicit_source(self):
        service, _ = _make_stub_service()
        service.pause(SOURCE_HOTKEY, reason="F6")
        snap = service.snapshot()
        assert snap["pause_reasons"] == {"hotkey": "F6"}
        # Resuming a different source doesn't clear hotkey.
        service.resume(SOURCE_MANUAL)
        assert service.is_paused is True

    def test_toggle_is_hotkey_default(self):
        service, _ = _make_stub_service()
        service.toggle()  # default source = hotkey
        assert "hotkey" in service.snapshot()["pause_reasons"]
        service.toggle()
        assert service.snapshot()["pause_reasons"] == {}

    def test_predicate_reads_union_live(self):
        """The predicate must read ControlState at CALL time, not at
        scheduler-build time — the F6 watcher fires from a different thread
        and the next cycle must see the new state."""
        async def _go():
            service, holder = _make_stub_service()
            await service.start()
            try:
                sched = holder["scheduler"]
                assert sched.pause_predicate() is False
                # Mutate from "another thread" (sync mutation from a coroutine
                # is fine — what matters is the predicate doesn't capture
                # the bool at build time).
                service.pause(SOURCE_HOTKEY)
                assert sched.pause_predicate() is True
                service.resume(SOURCE_HOTKEY)
                assert sched.pause_predicate() is False
            finally:
                await service.stop()
        asyncio.run(_go())

    def test_shared_control_state_visible_to_external_writer(self):
        """A watcher constructed against the service's ControlState must
        see its writes reflected in the service's snapshot."""
        cs = ControlState()
        service, _ = _make_stub_service(control=cs)
        # Watcher mutating directly through the shared state.
        cs.pause(SOURCE_IDLE, reason="user active 1s ago")
        snap = service.snapshot()
        assert snap["paused"] is True
        assert snap["pause_reasons"]["idle"] == "user active 1s ago"
        # And the service's predicate-facing read agrees.
        assert service.is_paused is True


# --------------------------------------------------------------- /api/control

@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


class TestControlEndpoints:
    """The pause / resume HTTP verbs that drive the dashboard button."""

    def _make_client_with_service(self, web_state: WebState):
        service, _ = _make_stub_service()
        app = create_app(state=web_state, service=service)
        return TestClient(app), service

    def test_pause_endpoint_flips_manual_source(self, web_state: WebState):
        client, service = self._make_client_with_service(web_state)
        with client:
            r = client.post("/api/control/pause", json={})
            assert r.status_code == 200
            body = r.json()
            assert body["paused"] is True
            assert "manual" in body["reasons"]
            # The service's own snapshot agrees.
            assert service.is_paused is True

    def test_pause_endpoint_accepts_custom_reason(self, web_state: WebState):
        client, _ = self._make_client_with_service(web_state)
        with client:
            r = client.post(
                "/api/control/pause",
                json={"reason": "stepping away"},
            )
            assert r.json()["reasons"]["manual"] == "stepping away"

    def test_pause_endpoint_accepts_empty_body(self, web_state: WebState):
        """Browser fetch w/o body shouldn't 400. Curl ``-X POST`` produces
        an empty body — also valid."""
        client, _ = self._make_client_with_service(web_state)
        with client:
            r = client.post("/api/control/pause")
            assert r.status_code == 200

    def test_resume_endpoint_clears_manual_source(self, web_state: WebState):
        client, service = self._make_client_with_service(web_state)
        with client:
            client.post("/api/control/pause", json={})
            assert service.is_paused is True
            r = client.post("/api/control/resume")
            assert r.status_code == 200
            assert r.json()["paused"] is False
            assert service.is_paused is False

    def test_resume_only_clears_manual_not_hotkey(self, web_state: WebState):
        """The dashboard resume button MUST NOT clear the hotkey pause —
        otherwise the user could un-do their own F6 press from the
        dashboard, which would surprise them when the next cycle still
        pauses (hotkey is still ON)."""
        client, service = self._make_client_with_service(web_state)
        # Simulate: F6 fired (hotkey paused) AND the user pressed Pause too.
        service.pause(SOURCE_HOTKEY)
        with client:
            client.post("/api/control/pause", json={})
            r = client.post("/api/control/resume")
            body = r.json()
            # Still paused because hotkey is still in the set.
            assert body["paused"] is True
            assert "hotkey" in body["reasons"]
            assert "manual" not in body["reasons"]

    def test_status_endpoint_surfaces_pause_reasons(self, web_state: WebState):
        client, service = self._make_client_with_service(web_state)
        service.pause(SOURCE_MANUAL, reason="testing")
        with client:
            r = client.get("/api/status")
            sched = r.json()["scheduler"]
            assert sched["paused"] is True
            assert sched["pause_reasons"]["manual"] == "testing"

    def test_endpoints_409_without_service(self, web_state: WebState):
        """Read-only diagnostics mode has no scheduler to pause."""
        app = create_app(state=web_state, service=None)
        with TestClient(app) as client:
            assert client.post("/api/control/pause", json={}).status_code == 409
            assert client.post("/api/control/resume").status_code == 409


# --------------------------------------------------------------- HotkeyWatcher

class TestHotkeyWatcher:
    """The Win32 path requires a desktop session; tests exercise the
    soft-fail surface + the toggle wiring helper."""

    def test_non_windows_soft_fail(self, monkeypatch):
        """Spec §7a explicitly allows non-Windows to skip the hotkey —
        ``start()`` must return False without raising."""
        monkeypatch.setattr(sys, "platform", "linux")
        toggled = []
        w = HotkeyWatcher(on_toggle=lambda: toggled.append(1), key="F6")
        assert w.start() is False
        assert "unsupported" in (w.last_error or "")
        w.stop()  # safe no-op when start() returned False

    def test_unknown_key_soft_fail(self, monkeypatch):
        """A typo'd key must surface as a clear last_error, not crash the
        whole app boot."""
        monkeypatch.setattr(sys, "platform", "win32")
        w = HotkeyWatcher(on_toggle=lambda: None, key="NOT_A_KEY")
        assert w.start() is False
        assert "unknown hotkey" in (w.last_error or "")

    def test_stop_without_start_is_safe(self):
        """The lifespan calls stop() even when start() failed — must not
        raise so we don't poison shutdown."""
        w = HotkeyWatcher(on_toggle=lambda: None, key="F6")
        w.stop()

    def test_build_hotkey_toggle_targets_hotkey_source(self):
        """``build_hotkey_toggle`` returns a zero-arg callable that toggles
        the SchedulerService via the ``hotkey`` source (NOT manual). The
        F6 watcher fires this directly."""
        service, _ = _make_stub_service()
        toggle = build_hotkey_toggle(service)
        toggle()
        assert "hotkey" in service.snapshot()["pause_reasons"]
        toggle()
        assert "hotkey" not in service.snapshot()["pause_reasons"]


# --------------------------------------------------------------- IdleWatcher

class TestIdleWatcher:
    """Drive the poll loop with an injected idle reader so no Win32 is
    needed. Each test waits a bounded time for the loop to act and then
    asserts the resulting pause state."""

    def _wait_for(self, predicate, *, timeout=1.5, interval=0.02):
        """Poll ``predicate`` until True or timeout. Returns the last
        observed value so the caller can assert on it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def test_pauses_when_user_recently_active(self):
        cs = ControlState()
        # User active 1s ago — well under the 5s threshold.
        w = IdleWatcher(
            cs,
            idle_threshold_s=5.0,
            poll_interval_s=0.02,
            read_idle_seconds=lambda: 1.0,
        )
        w.start()
        try:
            assert self._wait_for(lambda: cs.is_paused()), \
                "expected idle pause to engage when user is active"
            reasons = cs.snapshot().reasons
            assert "idle" in reasons
            assert "user active" in reasons["idle"]
        finally:
            w.stop()

    def test_resumes_when_user_goes_idle(self):
        cs = ControlState()
        # Idle reader returns >threshold → no pause should be held.
        # Start active, then make it report idle — the next poll clears.
        state = {"idle_s": 1.0}
        w = IdleWatcher(
            cs,
            idle_threshold_s=5.0,
            poll_interval_s=0.02,
            read_idle_seconds=lambda: state["idle_s"],
        )
        w.start()
        try:
            assert self._wait_for(lambda: cs.is_paused())
            # User went idle.
            state["idle_s"] = 60.0
            assert self._wait_for(lambda: not cs.is_paused()), \
                "expected idle pause to release after threshold"
        finally:
            w.stop()

    def test_stop_clears_lingering_pause(self):
        """Stopping the watcher with a held pause must release it — a
        stuck-paused scheduler after teardown would be a footgun."""
        cs = ControlState()
        w = IdleWatcher(
            cs,
            idle_threshold_s=5.0,
            poll_interval_s=0.02,
            read_idle_seconds=lambda: 0.5,
        )
        w.start()
        try:
            assert self._wait_for(lambda: cs.is_paused())
        finally:
            w.stop()
        assert cs.is_paused() is False

    def test_read_error_does_not_kill_loop(self):
        """A transient read error (e.g. an OS API throwing) must mark
        last_error and try again next tick, not crash the daemon thread."""
        cs = ControlState()
        calls = {"n": 0}

        def _reader():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("simulated transient failure")
            return 0.5  # then "user active"

        w = IdleWatcher(
            cs,
            idle_threshold_s=5.0,
            poll_interval_s=0.02,
            read_idle_seconds=_reader,
        )
        w.start()
        try:
            # The loop keeps ticking past the failure → eventually pauses.
            assert self._wait_for(lambda: cs.is_paused(), timeout=2.0)
            assert "simulated transient failure" in (w.last_error or "")
        finally:
            w.stop()


# --------------------------------------------------------------- lifespan

class _FakeWatcher:
    """Drop-in for HotkeyWatcher / IdleWatcher — records start/stop calls
    so the lifespan integration test can verify both hook in cleanly."""

    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self) -> None:
        self.stopped = True


class TestLifespanWatchers:

    def test_lifespan_starts_and_stops_watchers(self, web_state: WebState):
        service, _ = _make_stub_service()
        w1 = _FakeWatcher()
        w2 = _FakeWatcher()
        app = create_app(state=web_state, service=service, watchers=[w1, w2])
        with TestClient(app):
            assert w1.started is True
            assert w2.started is True
        # Exiting the context teardown-stops both.
        assert w1.stopped is True
        assert w2.stopped is True

    def test_lifespan_swallows_watcher_start_errors(self, web_state: WebState):
        """A watcher that raises on start() must not prevent the app from
        booting — the dashboard is more important than F6."""
        service, _ = _make_stub_service()

        class _BoomWatcher:
            def start(self):
                raise RuntimeError("boom")

            def stop(self):
                pass

        good = _FakeWatcher()
        app = create_app(
            state=web_state, service=service,
            watchers=[_BoomWatcher(), good],
        )
        # The lifespan opens cleanly + boots the good watcher despite the
        # bad one raising.
        with TestClient(app) as client:
            assert good.started is True
            assert client.get("/api/status").status_code == 200


# --------------------------------------------------------------- settings

class TestWebConfig:

    def test_default_hotkey_is_f6(self):
        cfg = WebConfig()
        assert cfg.hotkey == "F6"
        assert cfg.hotkey_enabled is True

    def test_idle_detect_default_off(self):
        """Spec §7a calls idle-detect *optional* — default OFF so it's an
        opt-in behavior. Many users would rather their bot NOT pause on a
        stray keystroke."""
        cfg = WebConfig()
        assert cfg.idle_detect_enabled is False

    def test_idle_threshold_validated(self):
        with pytest.raises(Exception):
            WebConfig(idle_threshold_s=0)
        with pytest.raises(Exception):
            WebConfig(idle_poll_s=-1)
