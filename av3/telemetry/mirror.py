"""Mirror queue: the client-side spool for the opt-in remote telemetry mirror
(spec §9, Phase 5 2/M).

Every status='error' event row and every stage='resolver_inferred' / status='ok'
event row is — *only when telemetry is opted in* — scrubbed and enqueued here
for later out-of-band drainage by the relay client (Phase 5 4/M). The local
``events.db`` still keeps the full unscrubbed detail.

## Design decisions (recorded so future-me / Phase 5 4/M doesn't re-derive)

1. **Single ``mirror_queue`` table inside ``events.db`` — not a column on
   ``events``, not a JSONL spool.**
   * Same DB as the events table → atomic enqueue in the same connection /
     WAL, no second persistence layer to corrupt independently.
   * Separate table (not a ``mirror_state`` column on ``events``) because
     ``events`` is high-write append-mostly and the drainer would otherwise
     UPDATE old rows constantly, fighting WAL page reuse. A small hot queue
     table keeps the drainer's scan tiny.
   * Not JSONL: composability with future ``cli stats`` exposing
     ``mirror_pending`` counts, and the relay (4/M) only needs one SQLite
     connection. JSONL would require its own append-lock and reader.

2. **Schema is intentionally tiny.** ``category`` distinguishes the two §9
   payload types; ``payload_json`` is the *already-scrubbed* dict ready for
   POST. The scrubber owns the schema per category — see
   :mod:`av3.telemetry.scrub`.

3. **Identity at enqueue time.** Spec §9 says we send
   ``user_id = sha256(handle)[:10]`` and never the raw handle. The hash is
   computed when the queue is attached, not when the relay POSTs, so the
   raw handle is referenced exactly once at startup and otherwise lives
   only in ``TelemetryConfig.handle`` on disk.

4. **Drainage API only; no HTTP here.** ``next_due()`` / ``mark_delivered()``
   / ``mark_failed()`` are the interface the relay client (4/M) will use.
   This sub-phase ships the spool + tests; the relay itself is owner-hosted
   infra and ships separately per spec §11a.

5. **Opt-in gating is single-point.** Enqueue is a no-op when no
   :class:`MirrorPolicy` is attached, and *attaching* is what the
   ``cli telemetry on|status`` command (3/M) will toggle. The data layer
   does not read settings; the caller does. Keeps the queue oblivious to
   config and easy to test directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from av3.domain.models import utcnow_iso
from av3.telemetry.scrub import scrub_error_event, scrub_inferred_answer_event

__all__ = [
    "MIRROR_QUEUE_DDL",
    "MirrorPolicy",
    "MirrorQueue",
    "QueuedMirrorRow",
    "user_id_from_handle",
]


# DDL extends the sink's existing ``events.db`` connection — see ``EventSink.__init__``.
MIRROR_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS mirror_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL,        -- 'error' | 'inferred_answer'
    payload_json  TEXT    NOT NULL,        -- scrubbed dict, ready to POST
    enqueued_at   TEXT    NOT NULL,        -- ISO-8601 UTC, matches EventSink.emit shape
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,                    -- last relay failure reason, NULL on success
    next_retry_at TEXT    NOT NULL,        -- ISO-8601 UTC; due when <= now
    delivered_at  TEXT                     -- ISO-8601 UTC on success, NULL otherwise
);
CREATE INDEX IF NOT EXISTS ix_mirror_pending
    ON mirror_queue (delivered_at, next_retry_at);
"""

# Categories the queue knows how to scrub. The dispatch happens in
# :meth:`MirrorQueue.enqueue`; adding a new category is a one-line addition
# here + a scrubber in :mod:`av3.telemetry.scrub`.
_SCRUBBERS = {
    "error": scrub_error_event,
    "inferred_answer": scrub_inferred_answer_event,
}


# Backoff schedule (seconds) — applied as a hard ceiling per attempt index.
# Generous tail because (a) the relay is owner-hosted and may be briefly
# down for redeploys, and (b) the queue is bounded by retention pruning
# anyway. Past the last entry, the final value is reused (effectively
# permanent retry until pruned).
_BACKOFF_SECONDS = (0, 30, 120, 600, 3600, 21600)


def user_id_from_handle(handle: str) -> str:
    """Compute ``sha256(handle)[:10]`` for the telemetry identity (spec §9).

    Idempotent and pure so the same handle always yields the same user_id —
    that is the whole attribution mechanism. Whitespace is stripped because
    onboarding text inputs commonly include a trailing newline.
    """
    digest = hashlib.sha256(handle.strip().encode("utf-8")).hexdigest()
    return digest[:10]


@dataclass(frozen=True)
class MirrorPolicy:
    """Identity + opt-in state attached to the sink.

    ``enabled=False`` is the default for safety: the sink calls
    :meth:`MirrorQueue.enqueue` only when the policy is attached, so an
    attached-but-disabled policy is a no-op at the call site. Constructed via
    :meth:`from_settings` so all code paths route the raw handle through the
    sha256 truncation in :func:`user_id_from_handle`.
    """

    enabled: bool
    user_id: str
    app_version: str

    @classmethod
    def from_settings(cls, telemetry_cfg, app_version: str) -> "MirrorPolicy":
        """Build from a :class:`av3.config.TelemetryConfig` + app version string.

        ``handle`` may be ``None`` when telemetry is disabled (the onboarding
        wizard hasn't asked yet). In that case the policy is enabled=False and
        the user_id is a placeholder ``"anonymous"`` — enqueue() would refuse
        to fire anyway, but a structured value is friendlier than ``None`` if
        something inspects the policy object directly.
        """
        handle = (telemetry_cfg.handle or "").strip()
        user_id = user_id_from_handle(handle) if handle else "anonymous"
        return cls(
            enabled=bool(telemetry_cfg.enabled),
            user_id=user_id,
            app_version=app_version,
        )


@dataclass(frozen=True)
class QueuedMirrorRow:
    """A row read out of the queue by :meth:`MirrorQueue.next_due`."""

    id: int
    category: str
    payload: dict[str, Any]
    attempts: int
    enqueued_at: str
    next_retry_at: str
    last_error: str | None


class MirrorQueue:
    """Spool for scrubbed mirror payloads.

    Constructed from the sink's existing ``sqlite3.Connection`` so enqueue is
    atomic with the event row's INSERT — both happen on the same connection
    in WAL mode. The queue does not own the connection's lifecycle.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(MIRROR_QUEUE_DDL)

    # ---- enqueue --------------------------------------------------------

    def enqueue(self, category: str, payload: dict[str, Any]) -> int | None:
        """Scrub ``payload`` per ``category`` and append to the queue.

        Returns the new row id, or ``None`` if the category-scrubber returned
        an empty dict (e.g. an EEO inferred-answer row, which §8d says never
        mirrors at all). Unknown categories raise ``ValueError`` — the call
        site should not be guessing.
        """
        scrubber = _SCRUBBERS.get(category)
        if scrubber is None:
            raise ValueError(f"unknown mirror category: {category!r}")
        scrubbed = scrubber(dict(payload))
        if not scrubbed:
            return None
        now = utcnow_iso()
        cur = self.conn.execute(
            """INSERT INTO mirror_queue
                   (category, payload_json, enqueued_at, attempts,
                    last_error, next_retry_at, delivered_at)
               VALUES (?, ?, ?, 0, NULL, ?, NULL)""",
            (category, json.dumps(scrubbed, sort_keys=True), now, now),
        )
        return cur.lastrowid

    # ---- drainage -------------------------------------------------------

    def next_due(self, limit: int = 50, now_iso: str | None = None) -> list[QueuedMirrorRow]:
        """Return up to ``limit`` undelivered rows whose ``next_retry_at`` is
        at or before ``now_iso`` (default: actual now). Oldest first so the
        relay drains in enqueue order — useful for chronological triage.

        Pure read; does NOT touch ``attempts`` (the relay calls
        :meth:`mark_failed` to bump that on a failed POST).
        """
        cutoff = now_iso or utcnow_iso()
        rows = self.conn.execute(
            """SELECT id, category, payload_json, attempts,
                      enqueued_at, next_retry_at, last_error
               FROM mirror_queue
               WHERE delivered_at IS NULL AND next_retry_at <= ?
               ORDER BY id ASC
               LIMIT ?""",
            (cutoff, int(limit)),
        ).fetchall()
        return [
            QueuedMirrorRow(
                id=r["id"],
                category=r["category"],
                payload=json.loads(r["payload_json"]),
                attempts=r["attempts"],
                enqueued_at=r["enqueued_at"],
                next_retry_at=r["next_retry_at"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def mark_delivered(self, row_id: int) -> None:
        """Stamp ``delivered_at`` so the row no longer surfaces in
        :meth:`next_due`. Kept (not deleted) so a future audit can answer
        "did we mirror X?" — the row will be pruned by the retention worker."""
        self.conn.execute(
            "UPDATE mirror_queue SET delivered_at = ?, last_error = NULL WHERE id = ?",
            (utcnow_iso(), row_id),
        )

    def mark_failed(self, row_id: int, reason: str) -> None:
        """Bump ``attempts`` and reschedule via :data:`_BACKOFF_SECONDS`. The
        reason string is truncated to keep this column from being a PII vector
        (relay errors usually echo back the payload shape).
        """
        cur = self.conn.execute(
            "SELECT attempts FROM mirror_queue WHERE id = ?", (row_id,)
        ).fetchone()
        if cur is None:
            return  # row was pruned mid-flight; drop the failure silently
        attempts = int(cur["attempts"]) + 1
        idx = min(attempts, len(_BACKOFF_SECONDS) - 1)
        next_at = _next_retry_iso(_BACKOFF_SECONDS[idx])
        self.conn.execute(
            """UPDATE mirror_queue
               SET attempts = ?,
                   last_error = ?,
                   next_retry_at = ?
               WHERE id = ?""",
            (attempts, _truncate_reason(reason), next_at, row_id),
        )

    # ---- introspection / pruning ---------------------------------------

    def pending_count(self) -> int:
        """For ``cli stats`` 's future ``mirror_pending`` column."""
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM mirror_queue WHERE delivered_at IS NULL"
        ).fetchone()["n"]

    def delivered_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM mirror_queue WHERE delivered_at IS NOT NULL"
        ).fetchone()["n"]

    def summary(self) -> dict[str, Any]:
        """One-shot snapshot for ``cli telemetry status`` and the diagnostics
        bundle: pending / delivered counts plus the most recent enqueue and the
        most recent failure (with its reason). All read-only; no PII (the
        ``last_error`` reason is already truncated by :meth:`mark_failed`).
        """
        pending = self.pending_count()
        delivered = self.delivered_count()
        last_enq = self.conn.execute(
            "SELECT MAX(enqueued_at) AS ts FROM mirror_queue"
        ).fetchone()["ts"]
        # Most recent still-pending failure: the relay client surfaces this so the
        # user knows *why* the queue isn't draining.
        fail = self.conn.execute(
            """SELECT last_error, next_retry_at, attempts
               FROM mirror_queue
               WHERE last_error IS NOT NULL AND delivered_at IS NULL
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return {
            "pending": pending,
            "delivered": delivered,
            "last_enqueued_at": last_enq,
            "last_error": fail["last_error"] if fail else None,
            "last_error_attempts": fail["attempts"] if fail else None,
            "next_retry_at": fail["next_retry_at"] if fail else None,
        }

    def prune_delivered(self, keep_days: int) -> int:
        """Drop delivered rows older than ``keep_days``. Pending rows are NEVER
        pruned by this method — they're retried indefinitely (the backoff
        ladder tops out at 6h). The retention worker (spec §4) calls this on
        its maintenance interval.
        """
        cur = self.conn.execute(
            """DELETE FROM mirror_queue
               WHERE delivered_at IS NOT NULL
                 AND delivered_at < datetime('now', ?)""",
            (f"-{int(keep_days)} days",),
        )
        return cur.rowcount


# ---- helpers ---------------------------------------------------------------

def _next_retry_iso(seconds: int) -> str:
    """Compute ``utcnow + seconds`` in the exact ISO shape :func:`utcnow_iso`
    produces (with the ``+00:00`` offset suffix), so SQL lexicographic compare
    against ``next_retry_at`` works without a format mismatch.
    """
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))
    ).isoformat(timespec="seconds")


_MAX_REASON_LEN = 200


def _truncate_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    s = str(reason)
    if len(s) > _MAX_REASON_LEN:
        return s[:_MAX_REASON_LEN] + "…"
    return s
