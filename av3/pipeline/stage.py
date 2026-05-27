"""The ``@stage`` instrumentation wrapper (spec §7).

> "Each unit of work runs inside a ``@stage("name")`` wrapper that emits
> start/ok/error events with timing automatically. That wrapper is the entire
> observability story — no scattered logging calls."

Works for both sync and async callables (workers are async, spec §3). It pulls
``run_id`` from a contextvar and ``job_id``/``platform`` from the wrapped call's
kwargs (or a ``job`` kwarg). Raising :class:`StageSkip` records a ``skip`` (a
legitimate early-exit like dedup/ghost/filter), not an ``error``.
"""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from contextvars import ContextVar

from av3.telemetry import get_sink

_current_run_id: ContextVar[str | None] = ContextVar("av3_run_id", default=None)


def new_run_id() -> str:
    """Generate and install a fresh run id for the current context."""
    rid = uuid.uuid4().hex[:12]
    _current_run_id.set(rid)
    return rid


def set_run_id(run_id: str | None) -> None:
    _current_run_id.set(run_id)


def get_run_id() -> str | None:
    return _current_run_id.get()


class StageSkip(Exception):
    """Raise inside a ``@stage`` body to record a ``skip`` with a reason and return
    ``None`` to the caller (dedup, ghost, below-floor, lost-the-prefilter)."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


def _extract_context(kwargs: dict) -> tuple[str | None, str | None]:
    """Best-effort (job_id, platform) for the event row, from call kwargs."""
    job_id = kwargs.get("job_id")
    platform = kwargs.get("platform")
    job = kwargs.get("job")
    if job is not None:
        job_id = job_id or getattr(job, "id", None)
        platform = platform or getattr(job, "source", None)
    return job_id, platform


def stage(name: str, *, platform: str | None = None):
    """Decorator factory. ``name`` is the stage label (e.g. ``"discover"``)."""

    def decorator(fn):
        is_async = inspect.iscoroutinefunction(fn)

        def _emit(status: str, **extra) -> None:
            sink = get_sink()
            if sink is None:  # telemetry not configured (unit tests) → drop silently
                return
            sink.emit(stage=name, status=status, run_id=get_run_id(), **extra)

        if is_async:

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                job_id, plat = _extract_context(kwargs)
                plat = platform or plat
                _emit("start", platform=plat, job_id=job_id)
                t0 = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                except StageSkip as skip:
                    _emit(
                        "skip", platform=plat, job_id=job_id,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        context={"reason": skip.reason} if skip.reason else None,
                    )
                    return None
                except Exception as exc:
                    _emit(
                        "error", platform=plat, job_id=job_id,
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        error_type=type(exc).__name__, error_msg=str(exc),
                    )
                    raise
                _emit(
                    "ok", platform=plat, job_id=job_id,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                return result

            return awrapper

        @functools.wraps(fn)
        def swrapper(*args, **kwargs):
            job_id, plat = _extract_context(kwargs)
            plat = platform or plat
            _emit("start", platform=plat, job_id=job_id)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except StageSkip as skip:
                _emit(
                    "skip", platform=plat, job_id=job_id,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    context={"reason": skip.reason} if skip.reason else None,
                )
                return None
            except Exception as exc:
                _emit(
                    "error", platform=plat, job_id=job_id,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    error_type=type(exc).__name__, error_msg=str(exc),
                )
                raise
            _emit(
                "ok", platform=plat, job_id=job_id,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            return result

        return swrapper

    return decorator
