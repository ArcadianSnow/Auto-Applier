"""ControlState — the union of all pause sources (spec §7a).

Phase 4 (3/M) replaces the (1/M) bare ``bool`` pause flag on the
:class:`auto_applier.web.service.SchedulerService` with this object. Three sources
can pause the scheduler independently; the predicate is paused iff *any*
source is paused:

  * ``manual`` — dashboard pause button or ``/api/control/pause``.
  * ``hotkey`` — F6 (or configured key) toggled the bot off.
  * ``idle``   — idle-detector says the user is currently active.

Each source carries a short reason string surfaced in ``/api/status`` so
the UI can render *why* the scheduler is paused (the dashboard's status
bar shows "Paused — manual + hotkey" when both fire).

**Why a single union object** (vs. three separate booleans on the
service):

* The Scheduler's ``pause_predicate`` is a single ``Callable[[], bool]``
  — the union is the most natural shape behind that contract.
* Each source flips independently from a different thread (hotkey from a
  Win32 msg loop, idle from a poll loop, manual from a request handler).
  Centralizing the lock here keeps the service code free of locking
  concerns and prevents the "two-flag race" where one source clears and
  another sets in the same predicate call.
* Reasons aggregate cleanly — the snapshot returns a stable dict the
  dashboard renders without per-source special-casing.

The lock is a plain :class:`threading.Lock`. ControlState is touched by:
  * The asyncio event loop (web routes, predicate calls) — runs on one
    thread but interleaves with the daemon threads' touches.
  * The hotkey watcher's Win32 msg-loop thread.
  * The idle watcher's poll thread.
Three writers; one reader (the predicate). A regular ``Lock`` is
sufficient — RLock isn't needed because no method calls another locked
method.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


# Canonical source identifiers. The HTTP API accepts only these — anything
# else is a 400 — so a future "idle" rename doesn't silently shadow the old
# name in stale dashboards.
SOURCE_MANUAL = "manual"
SOURCE_HOTKEY = "hotkey"
SOURCE_IDLE = "idle"

VALID_SOURCES: frozenset[str] = frozenset({
    SOURCE_MANUAL, SOURCE_HOTKEY, SOURCE_IDLE,
})


@dataclass(frozen=True)
class PauseSnapshot:
    """Immutable view of the control state for the dashboard JSON.

    ``paused`` is the OR-union of every source. ``reasons`` is a stable
    ``{source: reason_string}`` mapping for active sources only; absent
    sources are not pausing the scheduler. Empty dict + ``paused=False``
    is the running steady state.
    """

    paused: bool
    reasons: dict[str, str]

    def to_dict(self) -> dict:
        return {"paused": self.paused, "reasons": dict(self.reasons)}


class ControlState:
    """Pause-source union with thread-safe mutators.

    The scheduler's pause predicate calls :meth:`is_paused` once per
    cycle — cheap and lock-protected. Mutators (``pause``, ``resume``,
    ``toggle``) take the same lock and return the resulting
    :class:`PauseSnapshot` so callers can echo the new state back to the
    user without a second method call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Source -> reason string. Presence == paused by that source;
        # absence == not paused by that source. Reason is informational
        # only (the predicate doesn't care about content).
        self._reasons: dict[str, str] = {}

    # ---------------------------------------------------------------- mutate

    def pause(self, source: str, *, reason: str = "") -> PauseSnapshot:
        """Mark ``source`` as pausing the scheduler with an optional reason
        string. Idempotent — calling twice with different reasons replaces
        the reason for that source (last write wins; common case is the
        idle watcher refreshing its "user active 3s ago" reason).

        Raises :class:`ValueError` for unknown sources so a typo in the
        HTTP layer surfaces as a 400 rather than silently dropping the
        request.
        """
        self._check_source(source)
        with self._lock:
            self._reasons[source] = reason or self._default_reason(source)
            return self._snapshot_locked()

    def resume(self, source: str) -> PauseSnapshot:
        """Clear ``source`` from the pause set. Idempotent — clearing a
        source that wasn't paused is a no-op (still returns the snapshot
        so the caller can echo state)."""
        self._check_source(source)
        with self._lock:
            self._reasons.pop(source, None)
            return self._snapshot_locked()

    def toggle(self, source: str, *, reason: str = "") -> PauseSnapshot:
        """If ``source`` is paused, resume it; else pause it with the given
        reason. The hotkey watcher uses this — F6 is conceptually a toggle,
        not a directional command. Atomic so two rapid F6 presses can't
        race the lock and end up in an inconsistent state."""
        self._check_source(source)
        with self._lock:
            if source in self._reasons:
                self._reasons.pop(source, None)
            else:
                self._reasons[source] = reason or self._default_reason(source)
            return self._snapshot_locked()

    # ---------------------------------------------------------------- read

    def is_paused(self) -> bool:
        """The predicate handed to the Scheduler. Returns True iff *any*
        source is currently pausing. Fast path — no allocation, just a
        len() check under the lock."""
        with self._lock:
            return bool(self._reasons)

    def snapshot(self) -> PauseSnapshot:
        """Stable read of the current pause state for the dashboard. Always
        a copy of the internal dict so callers can't mutate state by
        accident."""
        with self._lock:
            return self._snapshot_locked()

    # ---------------------------------------------------------------- internals

    def _snapshot_locked(self) -> PauseSnapshot:
        return PauseSnapshot(
            paused=bool(self._reasons),
            reasons=dict(self._reasons),
        )

    @staticmethod
    def _check_source(source: str) -> None:
        if source not in VALID_SOURCES:
            raise ValueError(
                f"unknown pause source {source!r}; "
                f"valid: {sorted(VALID_SOURCES)}"
            )

    @staticmethod
    def _default_reason(source: str) -> str:
        # Surfaced to the dashboard when the caller didn't pass one.
        # Short + plain — the UI renders these verbatim.
        return {
            SOURCE_MANUAL: "paused from dashboard",
            SOURCE_HOTKEY: "F6 control-handoff",
            SOURCE_IDLE: "user active",
        }.get(source, source)


# Default safety window: a takeover whose tab-close we never observe (the user wandered
# off, the browser crashed) auto-releases after this long so the apply stage can never be
# stuck-paused forever. 15 minutes is comfortably longer than finishing one application.
_TAKEOVER_TIMEOUT_S = 900.0


class ManualTakeover:
    """Tracks active *manual* browser takeovers so the scheduler can mask ONLY the apply
    stage while the user is hands-on in the shared Chrome window.

    The apply worker drives its own tabs in the bot's persistent Chrome profile; when the
    user opens a job in that *same* window (login-on-demand, assisted submit, "Open in
    browser") the apply worker keeps opening/navigating tabs and steals focus. This is the
    apply-only analog of quiet hours: while a takeover is active the scheduler skips the
    apply stage but keeps every gather stage (discover/filter/score/optimize) running, so
    discovery never stalls just because the user is finishing one application.

    Lifecycle: :meth:`engage` on open (returns a token), :meth:`release` on that tab's
    ``close`` event. A safety timeout auto-releases a takeover whose close we never saw, so
    apply can never wedge. Each engage is independent (counted by token) — N open takeovers
    keep apply masked until the last one releases or times out.
    """

    def __init__(
        self,
        *,
        timeout_s: float = _TAKEOVER_TIMEOUT_S,
        now: Callable[[], float] | None = None,
    ):
        self._timeout_s = timeout_s
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._active: dict[int, float] = {}  # token -> engaged-at (monotonic)
        self._next_token = 0

    def engage(self) -> int:
        """Begin a takeover; returns a token to pass back to :meth:`release`."""
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._active[token] = self._now()
            return token

    def release(self, token: int) -> None:
        """End the takeover for ``token`` (idempotent — releasing twice is a no-op)."""
        with self._lock:
            self._active.pop(token, None)

    def is_active(self) -> bool:
        """The apply-gate predicate handed to the Scheduler. True iff at least one
        non-timed-out takeover is in progress. Prunes timed-out takeovers as a side
        effect so a forgotten-open tab self-clears."""
        with self._lock:
            if not self._active:
                return False
            cutoff = self._now() - self._timeout_s
            for token in [t for t, at in self._active.items() if at < cutoff]:
                self._active.pop(token, None)
            return bool(self._active)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._active)
