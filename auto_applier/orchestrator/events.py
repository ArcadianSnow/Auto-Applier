"""Event system for decoupling orchestrator from GUI/CLI."""
import asyncio
from collections import defaultdict
from typing import Any, Callable


class EventEmitter:
    """Simple pub/sub event system with optional blocking events."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._pending_futures: dict[str, asyncio.Future] = {}
        # Track which asyncio loop each pending future belongs to so
        # cross-thread resolution can schedule set_result via
        # call_soon_threadsafe on the owning loop.
        self._pending_loops: dict[str, asyncio.AbstractEventLoop] = {}

    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to an event."""
        self._handlers[event].append(handler)

    def off(self, event: str, handler: Callable) -> None:
        """Unsubscribe from an event."""
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    def emit(self, event: str, **data) -> None:
        """Emit an event to all subscribers (non-blocking)."""
        for handler in self._handlers.get(event, []):
            try:
                handler(**data)
            except Exception:
                pass

    async def emit_and_wait(self, event: str, timeout: float = 300.0, **data) -> Any:
        """Emit an event and wait for a response (blocking).

        Used when the orchestrator needs user input (e.g., job review decision).
        The GUI handler should call resolve_event() with the response.
        Times out after ``timeout`` seconds (default 5 minutes).

        The current running asyncio loop is captured here so
        :meth:`resolve_event` can schedule the future's set_result
        back ONTO that loop via call_soon_threadsafe. Without this,
        GUI threads that resolve events live-lock the awaiter
        because future.set_result is not thread-safe.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[event] = future
        self._pending_loops[event] = loop

        # Notify handlers
        self.emit(event, **data)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_futures.pop(event, None)
            self._pending_loops.pop(event, None)

    def resolve_event(self, event: str, result: Any) -> None:
        """Resolve a pending emit_and_wait with a result.

        Thread-safe: the future's set_result is scheduled via
        call_soon_threadsafe on the loop that owned the future, so
        GUI callbacks running on the Tk main thread can resolve
        asyncio futures without undefined behavior or hangs.
        """
        future = self._pending_futures.get(event)
        loop = self._pending_loops.get(event)
        if future is None or future.done():
            return
        if loop is None or loop.is_closed():
            # Fall back to a direct set — not thread-safe but better
            # than losing the resolution entirely.
            try:
                future.set_result(result)
            except Exception:
                pass
            return
        loop.call_soon_threadsafe(
            lambda: future.set_result(result) if not future.done() else None
        )


# ---------------------------------------------------------------------------
# Standard event names
# ---------------------------------------------------------------------------
RUN_STARTED = "run_started"
RESUME_PARSED = "resume_parsed"
PLATFORM_STARTED = "platform_started"
PLATFORM_LOGIN_NEEDED = "platform_login_needed"
PLATFORM_LOGIN_FAILED = "platform_login_failed"
SEARCH_STARTED = "search_started"
JOBS_FOUND = "jobs_found"
JOB_SCORED = "job_scored"
USER_REVIEW_NEEDED = "user_review_needed"
# Fired after ALL platforms finish if the pending_review queue has
# items. Carries the full list so the dashboard can open a batch
# review panel instead of blocking mid-pipeline.
REVIEW_QUEUE_READY = "review_queue_ready"
APPLICATION_STARTED = "application_started"
APPLICATION_COMPLETE = "application_complete"
PLATFORM_ERROR = "platform_error"
PLATFORM_FINISHED = "platform_finished"
EVOLUTION_TRIGGERS = "evolution_triggers"
RUN_FINISHED = "run_finished"
CAPTCHA_DETECTED = "captcha_detected"

# Continuous-run mode — emitted by ApplicationEngine.run_continuous.
# CYCLE_STARTED fires at the top of each cycle with cycle_number and
# (optionally) total_cycles. CYCLE_IDLE fires after a cycle completes
# with seconds_until_next so UIs can show a countdown. CYCLE_RESUMING
# fires just before run() is called again.
CYCLE_STARTED = "cycle_started"
CYCLE_IDLE = "cycle_idle"
CYCLE_RESUMING = "cycle_resuming"
CONTINUOUS_FINISHED = "continuous_finished"

# Anti-detect cooldown between applications. Pipeline emits this
# right before sleeping so dashboards can render a live countdown
# instead of going silent for 60-180s. Payload: seconds=<float>.
COOLDOWN_STARTED = "cooldown_started"
