"""SchedulerService — lifecycle wrapper for the staged-worker scheduler.

The web app's FastAPI lifespan needs to:

  1. **Start** the staged-worker scheduler as a background ``asyncio.Task``.
  2. **Stop** it cleanly on shutdown (task.cancel + await).
  3. Expose a **pause-source union** that the scheduler's ``pause_predicate``
     reads every cycle. Phase 4 (3/M) replaces the (1/M) bare bool with a
     :class:`auto_applier.web.control.ControlState` — three independent sources
     (``manual`` / ``hotkey`` / ``idle``) OR together behind a thread-safe
     lock so the F6 watcher, idle poll loop, and HTTP handlers all flip the
     same union without races.

This module never builds the scheduler itself. The CLI knows how to construct
workers + session + LLM clients; it hands us a factory that takes our pause
predicate and returns a configured :class:`auto_applier.pipeline.Scheduler`. Tests pass
a no-op factory so the service can be instantiated without touching the real
pipeline.

**Why an async factory:** real construction needs an event loop — the
:class:`auto_applier.sources.browser.session.BrowserSession` start is awaitable, the
LLM clients may health-probe, etc. Forcing a sync factory would push that
setup out to the CLI before uvicorn owns the loop, fragmenting lifecycle.
Tests can pass a synchronous lambda wrapped in :func:`sync_factory`.

**Why a factory at all (instead of a pre-built scheduler):** the Scheduler
constructor takes its ``pause_predicate`` immutably (one closure capture at
build time). The service needs to *inject* a predicate that reads the
:class:`ControlState`, which only exists after ``SchedulerService`` is
instantiated. The factory closure resolves the circular ownership.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from auto_applier.pipeline import Scheduler, SchedulerRunSummary
from auto_applier.web.control import (
    SOURCE_HOTKEY,
    SOURCE_MANUAL,
    ControlState,
    PauseSnapshot,
)


class AsyncSchedulerFactory(Protocol):
    """Async callable that builds a Scheduler given a pause-predicate.

    The CLI's ``av3 serve`` provides this; the closure starts the
    BrowserSession and constructs workers inside the running event loop.
    Tests use :func:`sync_factory` to wrap a synchronous builder.
    """

    async def __call__(
        self, pause_predicate: Callable[[], bool]
    ) -> Scheduler: ...


def sync_factory(
    build: Callable[[Callable[[], bool]], Scheduler],
) -> AsyncSchedulerFactory:
    """Wrap a synchronous Scheduler builder into the async factory contract.

    Tests typically pass ``sync_factory(lambda p: StubScheduler(p))`` so the
    service can be exercised without an event loop's worth of ceremony.
    """

    async def _async(pause_predicate: Callable[[], bool]) -> Scheduler:
        return build(pause_predicate)

    return _async


class SchedulerService:
    """Manages the scheduler's lifecycle behind the FastAPI lifespan.

    Phase 4 (3/M) surface:
      * ``start()`` / ``stop()`` for the lifespan hooks (1/M).
      * ``pause(source=...)`` / ``resume(source=...)`` / ``toggle(source=...)``
        — drive the :class:`ControlState` union (3/M). The legacy zero-arg
        ``pause()`` / ``resume()`` default to the ``manual`` source so (1/M)
        tests still pass.
      * ``snapshot()`` for the read-only dashboard endpoints — now includes
        the active pause-reason set.
    """

    def __init__(
        self,
        factory: AsyncSchedulerFactory,
        *,
        teardown: Callable[[], Awaitable[None]] | None = None,
        control: ControlState | None = None,
    ):
        """``factory`` builds the Scheduler inside the running event loop;
        ``teardown`` runs after ``stop()`` cancels the task, for tearing down
        resources the factory created (BrowserSession, etc.). Both are
        optional — a test service can pass ``factory=sync_factory(...)`` and
        no teardown.

        ``control`` lets callers inject a shared :class:`ControlState` so the
        hotkey watcher + idle watcher + HTTP handlers all flip the same
        union. When omitted, the service builds its own — fine for tests
        and for ``--no-scheduler`` mode where no watcher exists.
        """
        self._factory = factory
        self._teardown = teardown
        self._scheduler: Scheduler | None = None
        self._task: asyncio.Task[SchedulerRunSummary] | None = None
        # The (1/M) bare bool became a ControlState in (3/M) — the predicate
        # below reads ``control.is_paused()``, which OR-unions ``manual`` +
        # ``hotkey`` + ``idle`` sources behind a thread-safe lock.
        self._control: ControlState = control if control is not None else ControlState()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Build the scheduler and spawn its run loop. Idempotent — calling
        twice is a no-op (the second call sees a live task and returns)."""
        if self._task is not None and not self._task.done():
            return
        self._scheduler = await self._factory(self._pause_predicate)
        # ``max_cycles=None`` runs forever; lifespan shutdown cancels the task.
        self._task = asyncio.create_task(
            self._scheduler.run(),
            name="av3-scheduler",
        )

    async def stop(self) -> None:
        """Cancel the scheduler task and wait for it to settle.

        The scheduler's ``run()`` sleeps between cycles, so cancellation
        propagates within one ``cycle_interval_s`` worst case. We swallow
        ``CancelledError`` because that IS the expected shutdown path; any
        other exception bubbles via ``task.result()`` semantics, which we
        suppress here to keep lifespan-shutdown clean (the exception is
        already in events.db via the @stage decorators if it mattered).

        After the task is reaped, run the optional teardown (BrowserSession
        stop, etc.) so resources the factory acquired are released.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                # @stage already wrote any per-stage failure to events.db.
                # Lifespan shutdown must not raise.
                pass
            self._task = None
            self._scheduler = None
        if self._teardown is not None:
            try:
                await self._teardown()
            except Exception:
                # Same posture as the task await — don't mask shutdown.
                pass

    # ---------------------------------------------------------------- pause

    def pause(self, source: str = SOURCE_MANUAL, *, reason: str = "") -> PauseSnapshot:
        """Cooperative pause. The scheduler reads ``pause_predicate()`` once
        per cycle; the next cycle after this call returns without running
        any stage.

        ``source`` defaults to ``manual`` so the legacy zero-arg signature
        from (1/M) still works (it represents "the user pressed pause from
        the dashboard"). The hotkey + idle watchers pass their own source.
        """
        return self._control.pause(source, reason=reason)

    def resume(self, source: str = SOURCE_MANUAL) -> PauseSnapshot:
        """Clear ``source`` from the pause set. The next cycle resumes
        normal stage drain unless another source still holds a pause."""
        return self._control.resume(source)

    def toggle(self, source: str = SOURCE_HOTKEY, *, reason: str = "") -> PauseSnapshot:
        """Atomic toggle for the hotkey watcher — F6 is conceptually a
        switch, not a directional command. Default source is ``hotkey`` so
        callers pass nothing in the common case."""
        return self._control.toggle(source, reason=reason)

    @property
    def control(self) -> ControlState:
        """Expose the underlying ControlState so the hotkey + idle watchers
        + HTTP handlers all flip the same union. Read-only access via the
        property keeps test code that pokes the predicate cleanly."""
        return self._control

    @property
    def is_paused(self) -> bool:
        return self._control.is_paused()

    @property
    def is_running(self) -> bool:
        """True iff the scheduler task is alive. ``False`` before start() or
        after stop() / a crash that escaped the run loop."""
        return self._task is not None and not self._task.done()

    # ---------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        """Lightweight status surface for ``/api/status``. Read-only and
        cheap to call on every request — no DB queries here."""
        pause_snap = self._control.snapshot()
        return {
            "running": self.is_running,
            "paused": pause_snap.paused,
            # New in (3/M): the dashboard's status bar renders the set of
            # active reasons so the user sees *why* the scheduler is paused
            # (e.g. "F6 control-handoff + user active").
            "pause_reasons": pause_snap.reasons,
        }

    # ---------------------------------------------------------------- internals

    def _pause_predicate(self) -> bool:
        """The closure handed to the Scheduler constructor. Reads the current
        pause-union flag at call time so toggles from any thread take effect
        on the next cycle."""
        return self._control.is_paused()
