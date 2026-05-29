"""Per-source health state — session-expiry graceful degradation (spec §8b).

> "Session expiry = graceful degradation. Manual-login-only means the bot can't
>  re-authenticate. When a platform's session dies mid-run, that platform is
>  paused with a 'login needed' flag (surfaced as a dashboard badge); all other
>  sources keep running. One dead session never stalls the whole bot."

This module owns the in-memory per-source flag. The apply worker checks it before
processing each job and silently skips paused sources; the (Phase 3 (5/M))
scheduler will read the same flag to skip paused per-source workers. The (Phase 4)
web dashboard will render the "login needed" badge by polling
:func:`snapshot`.

**In-memory, process-level.** Health is *not* persisted: a restart should re-probe
auth via the next live request (manual-login state already lives in the
browser-profile dir, which IS persistent). Persisting health to DB would just
risk a stale "needs login" badge after the user logs back in — easier to recover
naturally.

**Emits to telemetry on state change.** Flipping a source from HEALTHY → paused
emits a ``session_expiry`` event with ``status='auth_required'`` so the event
spine captures the moment (useful for "when did source X die?" queries against
``events.db``). The reverse transition emits ``status='healthy'``. No event when
state doesn't change — avoids noise on every per-job pause check.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum

from av3.telemetry import get_sink

__all__ = [
    "SourceHealthState",
    "SourceHealthRecord",
    "is_paused",
    "mark_auth_required",
    "mark_healthy",
    "paused_sources",
    "reset_health",
    "snapshot",
]


class SourceHealthState(str, Enum):
    """Per-source operational state. ``HEALTHY`` is the implicit default for any
    source not present in the registry — no record means "we haven't seen it yet
    and have no reason to pause it."
    """

    HEALTHY = "HEALTHY"
    AUTH_REQUIRED = "AUTH_REQUIRED"


@dataclass
class SourceHealthRecord:
    """One source's current state + the reason it's in that state. The reason
    is what the dashboard shows next to the "login needed" badge so the user
    knows what to do."""

    source: str
    state: SourceHealthState
    reason: str = ""


# Process-local state. Guarded by a lock so the (Phase 3 (5/M)) scheduler's
# multi-worker async tasks don't race on the dict's mutation; reads are also
# under the lock so a snapshot is consistent.
_lock = threading.Lock()
_records: dict[str, SourceHealthRecord] = {}


def mark_auth_required(source: str, *, reason: str = "session expired") -> None:
    """Pause this source until manually marked healthy again.

    Emits a ``session_expiry`` event on transition into AUTH_REQUIRED. Repeated
    calls with the same source are no-ops on telemetry (so a polling check doesn't
    flood the spine), but DO refresh the ``reason`` (the latest cause wins).
    """
    if not source:
        return
    with _lock:
        existing = _records.get(source)
        was_healthy = existing is None or existing.state is SourceHealthState.HEALTHY
        _records[source] = SourceHealthRecord(
            source=source,
            state=SourceHealthState.AUTH_REQUIRED,
            reason=reason,
        )
    if was_healthy:
        _emit("auth_required", source=source, reason=reason)


def mark_healthy(source: str) -> None:
    """Mark this source healthy. The dashboard 'login needed' badge clears the
    next time the UI polls :func:`snapshot`. Repeated calls are no-ops on
    telemetry.
    """
    if not source:
        return
    with _lock:
        existing = _records.get(source)
        was_paused = existing is not None and existing.state is SourceHealthState.AUTH_REQUIRED
        _records[source] = SourceHealthRecord(
            source=source,
            state=SourceHealthState.HEALTHY,
        )
    if was_paused:
        _emit("healthy", source=source, reason="")


def is_paused(source: str) -> bool:
    """True iff this source is currently AUTH_REQUIRED. Implicit default is
    HEALTHY for unseen sources, so a fresh process treats every source as
    runnable until proven otherwise."""
    if not source:
        return False
    with _lock:
        rec = _records.get(source)
        return rec is not None and rec.state is SourceHealthState.AUTH_REQUIRED


def paused_sources() -> set[str]:
    """All sources currently flagged AUTH_REQUIRED. Used by the scheduler to
    skip per-source workers without dropping their queues."""
    with _lock:
        return {
            r.source for r in _records.values()
            if r.state is SourceHealthState.AUTH_REQUIRED
        }


def snapshot() -> dict[str, SourceHealthRecord]:
    """Copy of all known source health records. The dashboard polls this for
    its login-needed badge; tests assert against this for state propagation.
    Safe to call without the registry being populated — returns ``{}`` when
    no source has ever been touched."""
    with _lock:
        # Shallow copy of the dict — records are frozen-ish dataclasses we own,
        # so callers can't mutate registry state by tweaking returned objects.
        return {s: SourceHealthRecord(r.source, r.state, r.reason)
                for s, r in _records.items()}


def reset_health() -> None:
    """Clear the registry. Tests use this in their setup/teardown to avoid
    bleed between cases (process is shared across the test session). Not
    callable from production code paths — there's no legitimate reason to
    flip every source back to healthy at once."""
    with _lock:
        _records.clear()


# --------------------------------------------------------------- telemetry

def _emit(transition: str, *, source: str, reason: str) -> None:
    """Emit one ``session_expiry`` event marking the state-change moment.

    ``transition`` is the destination state (``"auth_required"`` or
    ``"healthy"``). Drops silently if no sink is configured (unit tests that
    don't construct one)."""
    sink = get_sink()
    if sink is None:
        return
    sink.emit(
        stage="session_expiry",
        status=transition,
        platform=source,
        context={"reason": reason} if reason else None,
    )
