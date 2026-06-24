"""ReviewBatch — the batch barrier + per-job disposition for batched assisted review (Phases 2 + 4).

Design: ``.claude/skills/auto-applier/research/batched-assisted-review.md``.

The apply stage prepares a batch of N jobs (default 5), then HOLDS so the owner can verify /
correct / submit each on the "In Progress" page before the next N prepare. The hold is expressed
through the scheduler's existing `apply_gate`: while a batch is full and **not yet fully
dispositioned**, :meth:`is_holding` returns True → the scheduler skips ONLY the apply stage (the
gather stages keep running, exactly like a manual takeover or quiet hours).

Phase 4 adds per-job **disposition** tracking. Each member starts ``pending``; the owner marks it
``applied`` / ``skipped`` / ``needs_work`` (:meth:`dispose`, driven by the web disposition
endpoints). Once every member is dispositioned the hold lifts (:meth:`all_dispositioned`), and the
apply worker **auto-advances** — it releases the spent batch and prepares the next N (Phase 3's
manual "Release batch" button does the same thing eagerly). ``needs_work`` is a side-lane: it
unblocks the batch but leaves the job in REVIEW for the owner to revisit (it does not requeue).

Process-level shared state, mirroring :class:`auto_applier.web.control.ManualTakeover` — three
components touch it from different threads:

  * the **apply worker** registers each prepared job (:meth:`add`), auto-advances a spent batch,
    and stops preparing once full;
  * the **scheduler** reads the hold once per cycle (the composed `apply_gate`);
  * the **web layer** dispositions members + releases.

So a plain :class:`threading.Lock` guards every access.

Scope: the batch is IN MEMORY — a runtime barrier, not durable state. The per-job proposed SET
already survives a refresh as a file (Phase 1). On a process restart the barrier starts empty; jobs
already prepared sit in REVIEW for the owner (visible in the review queue), and the next batch fills
from whatever remains in QUEUED_APPLY. Durable/DB batch grouping is a future enhancement.
"""

from __future__ import annotations

import threading

from auto_applier.domain.models import new_id

__all__ = ["DISPOSITIONS", "PENDING", "ReviewBatch"]

#: A member that the owner hasn't acted on yet — it still blocks the batch from advancing.
PENDING = "pending"
#: Terminal/owner dispositions. ``applied`` / ``skipped`` mirror the job's state transition (the web
#: endpoints walk the state machine); ``needs_work`` is a side-lane (job stays in REVIEW). All three
#: count as "dealt with" for advancing the batch.
DISPOSITIONS = frozenset({"applied", "skipped", "needs_work"})


class ReviewBatch:
    """The current batch of prepared jobs, their per-job disposition, and the barrier predicate.

    A batch is "full" once :attr:`count` reaches :attr:`size`; a full batch with any ``pending``
    member *holds* (the apply stage pauses). When every member is dispositioned the hold lifts and
    the apply worker releases the batch (:meth:`release`) and prepares the next N.
    """

    def __init__(self, *, size: int = 5) -> None:
        # A batch of < 1 is meaningless (apply could never run); clamp so a bad config can't wedge.
        self._size = max(1, int(size))
        self._lock = threading.Lock()
        self._members: dict[str, str] = {}   # job_id -> disposition (PENDING by default)
        self._batch_id = new_id()

    # ---- mutate ----------------------------------------------------------

    def add(self, job_id: str) -> bool:
        """Register a just-prepared job into the current batch as ``pending``. Idempotent — a job
        counts once and a re-add never resets a disposition already recorded for it. Returns whether
        the batch is now full, so a caller can stop preparing more this run."""
        with self._lock:
            if job_id and job_id not in self._members:
                self._members[job_id] = PENDING
            return len(self._members) >= self._size

    def dispose(self, job_id: str, disposition: str) -> bool:
        """Record the owner's disposition for a member (applied / skipped / needs_work). No-op when
        the job isn't in the current batch (e.g. a review-queue job that was never batched). Returns
        whether the batch is NOW fully dispositioned (so a caller could advance eagerly).

        Raises :class:`ValueError` for an unknown disposition so a typo surfaces as a 400 in the
        HTTP layer rather than silently corrupting batch state."""
        if disposition not in DISPOSITIONS:
            raise ValueError(
                f"unknown disposition {disposition!r}; valid: {sorted(DISPOSITIONS)}"
            )
        with self._lock:
            if job_id in self._members:
                self._members[job_id] = disposition
            return bool(self._members) and all(
                d != PENDING for d in self._members.values()
            )

    def release(self) -> str:
        """Close the current batch and open a fresh empty one. Returns the new batch id. Lifts the
        hold (an empty batch is never full), so the apply stage resumes on the next cycle. Used both
        by the manual "Release batch" button and by the worker's auto-advance of a spent batch."""
        with self._lock:
            self._members.clear()
            self._batch_id = new_id()
            return self._batch_id

    # ---- read ------------------------------------------------------------

    def is_full(self) -> bool:
        with self._lock:
            return len(self._members) >= self._size

    def all_dispositioned(self) -> bool:
        """True iff the batch is non-empty and every member has been dispositioned (none pending).
        This is the auto-advance signal: a spent batch no longer holds and is released to make room
        for the next N."""
        with self._lock:
            return bool(self._members) and all(
                d != PENDING for d in self._members.values()
            )

    def is_holding(self) -> bool:
        """The barrier predicate composed into the scheduler's `apply_gate`. True iff the current
        batch is full AND still has a pending member — apply pauses, gather stages keep running.
        Once every member is dispositioned the hold lifts (the worker then advances)."""
        with self._lock:
            full = len(self._members) >= self._size
            settled = bool(self._members) and all(
                d != PENDING for d in self._members.values()
            )
            return full and not settled

    @property
    def size(self) -> int:
        return self._size

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._members)

    @property
    def pending(self) -> int:
        with self._lock:
            return sum(1 for d in self._members.values() if d == PENDING)

    @property
    def batch_id(self) -> str:
        with self._lock:
            return self._batch_id

    def snapshot(self) -> dict:
        """Stable view for the dashboard / status. ``members`` is the sorted job-id list (the feed
        iterates it); ``dispositions`` maps each to its disposition; ``pending`` / ``all_dispositioned``
        drive the page's progress + the auto-advance hint. Copies so callers can't mutate state."""
        with self._lock:
            pending = sum(1 for d in self._members.values() if d == PENDING)
            full = len(self._members) >= self._size
            settled = bool(self._members) and pending == 0
            return {
                "batch_id": self._batch_id,
                "size": self._size,
                "count": len(self._members),
                "members": sorted(self._members),
                "dispositions": dict(self._members),
                "pending": pending,
                "all_dispositioned": settled,
                "holding": full and not settled,
            }
