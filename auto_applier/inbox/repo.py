"""Inbox-side persistence for the email outcome loop (email-outcome-loop Phase B).

Mirrors the :mod:`auto_applier.db.repositories` style (one class, ``conn`` injected,
typed row mapping). Two small tables back this:

  * ``inbox_messages`` — one row per processed ``message_id``, recording how the message
    was routed (``outcome`` | ``review`` | ``ignored``). This is the **idempotency key**:
    :meth:`is_processed` lets the worker skip a message it already handled, so re-running
    a fetch never records a duplicate outcome (the APPLIED-invariant-adjacent guarantee
    for outcomes — dedup keys off the inbox message, not the email's content).
  * ``inbox_state`` — a per-folder ``last_uid`` cursor so a live IMAP fetch (Phase C) only
    pulls new mail. The offline ``--eml`` path never touches it.

The actual recorded :class:`~auto_applier.domain.models.Outcome` lives in the ``outcomes``
table via :class:`~auto_applier.db.repositories.OutcomeRepo` — this repo only tracks that
a message was seen and what the worker decided to do with it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from auto_applier.domain.models import utcnow_iso

__all__ = ["InboxMessage", "InboxMessageRepo"]

#: Valid ``action`` values — the worker's routing decision per message.
_ACTIONS = frozenset({"outcome", "review", "ignored"})


@dataclass(frozen=True)
class InboxMessage:
    """One processed inbox message (an ``inbox_messages`` row)."""

    message_id: str
    action: str                      # "outcome" | "review" | "ignored"
    matched_job_id: str | None = None
    kind: str | None = None          # OutcomeKind value, or None
    noted_at: str = ""


class InboxMessageRepo:
    """Idempotency + review-queue store for processed inbox messages."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _row(row: sqlite3.Row) -> InboxMessage:
        return InboxMessage(
            message_id=row["message_id"],
            action=row["action"],
            matched_job_id=row["matched_job_id"],
            kind=row["kind"],
            noted_at=row["noted_at"],
        )

    # -- per-message idempotency ------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        """True if this ``message_id`` was already handled (any action)."""
        row = self.conn.execute(
            "SELECT 1 FROM inbox_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        message_id: str,
        *,
        matched_job_id: str | None = None,
        kind: str | None = None,
        action: str,
    ) -> InboxMessage:
        """Record that ``message_id`` was processed with ``action``.

        ``action`` must be one of ``outcome`` | ``review`` | ``ignored``. Idempotent on
        the message_id: a re-mark (shouldn't happen — the worker checks ``is_processed``
        first) updates the existing row rather than raising on the PK conflict.
        """
        if action not in _ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ACTIONS)} (got {action!r})")
        now = utcnow_iso()
        self.conn.execute(
            """INSERT INTO inbox_messages (message_id, matched_job_id, kind, action, noted_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET
                   matched_job_id=excluded.matched_job_id, kind=excluded.kind,
                   action=excluded.action, noted_at=excluded.noted_at""",
            (message_id, matched_job_id, kind, action, now),
        )
        return InboxMessage(
            message_id=message_id, action=action,
            matched_job_id=matched_job_id, kind=kind, noted_at=now,
        )

    def list_for_review(self) -> list[InboxMessage]:
        """Messages routed to review (action='review') — backs ``av3 inbox --review``.

        Newest first so the most recent ambiguous mail surfaces at the top.
        """
        rows = self.conn.execute(
            "SELECT * FROM inbox_messages WHERE action = 'review' "
            "ORDER BY noted_at DESC, message_id"
        ).fetchall()
        return [self._row(r) for r in rows]

    # -- per-folder fetch cursor (Phase C) --------------------------------

    def last_uid(self, folder: str) -> int | None:
        """The last IMAP UID fetched for ``folder``, or None if never fetched."""
        row = self.conn.execute(
            "SELECT last_uid FROM inbox_state WHERE folder = ?", (folder,)
        ).fetchone()
        return int(row["last_uid"]) if row is not None else None

    def set_last_uid(self, folder: str, uid: int) -> None:
        """Persist the fetch cursor for ``folder`` (UPSERT-keyed by folder)."""
        self.conn.execute(
            """INSERT INTO inbox_state (folder, last_uid) VALUES (?, ?)
               ON CONFLICT(folder) DO UPDATE SET last_uid=excluded.last_uid""",
            (folder, int(uid)),
        )
