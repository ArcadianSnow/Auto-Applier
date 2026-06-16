"""Local-first inbox outcome loop (email-outcome-loop, research/future-directions.md Direction 4).

Phase A is 100% offline: parse raw email bytes → classify into the existing
:class:`~auto_applier.domain.state.OutcomeKind` ladder (deterministic-first, bounded-LLM,
fail-safe) → match back to an applied job by url/company signals.

Phase B (added) adds persistence + the worker + CLI, still offline: :class:`InboxMessageRepo`
(message_id idempotency + review queue), :class:`InboxWorker` (drives the EXISTING
``OutcomeRepo`` → analytics path via an injectable ``(uid, raw_bytes)`` source — a stub in
tests, ``.eml`` files in the CLI, real IMAP in Phase C). No real IMAP / creds yet.
"""

from __future__ import annotations

from auto_applier.inbox.classify import (
    EmailClass,
    classify,
    classify_deterministic,
)
from auto_applier.inbox.match import MatchResult, match_email
from auto_applier.inbox.parse import FetchedEmail, parse_message
from auto_applier.inbox.repo import InboxMessage, InboxMessageRepo
from auto_applier.inbox.worker import (
    CLASS_MIN,
    MATCH_MIN,
    InboxRunSummary,
    InboxWorker,
    eml_file_source,
)

__all__ = [
    "FetchedEmail",
    "parse_message",
    "EmailClass",
    "classify",
    "classify_deterministic",
    "MatchResult",
    "match_email",
    "InboxMessage",
    "InboxMessageRepo",
    "InboxWorker",
    "InboxRunSummary",
    "eml_file_source",
    "MATCH_MIN",
    "CLASS_MIN",
]
