"""Event system for decoupling orchestrator from GUI/CLI."""
import asyncio
from collections import defaultdict
from typing import Any, Callable


class EventEmitter:
    """Simple pub/sub event system with optional blocking events."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._pending_futures: dict[str, asyncio.Future] = {}

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
        """
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_futures[event] = future

        # Notify handlers
        self.emit(event, **data)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_futures.pop(event, None)

    def resolve_event(self, event: str, result: Any) -> None:
        """Resolve a pending emit_and_wait with a result."""
        future = self._pending_futures.get(event)
        if future and not future.done():
            future.set_result(result)


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
APPLICATION_STARTED = "application_started"
APPLICATION_COMPLETE = "application_complete"
PLATFORM_ERROR = "platform_error"
PLATFORM_FINISHED = "platform_finished"
EVOLUTION_TRIGGERS = "evolution_triggers"
RUN_FINISHED = "run_finished"
CAPTCHA_DETECTED = "captcha_detected"
