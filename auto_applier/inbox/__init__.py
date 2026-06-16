"""Local-first inbox outcome loop (email-outcome-loop, research/future-directions.md Direction 4).

Phase A (this slice) is 100% offline: parse raw email bytes → classify into the existing
:class:`~auto_applier.domain.state.OutcomeKind` ladder (deterministic-first, bounded-LLM,
fail-safe) → match back to an applied job by url/company signals. No IMAP, no DB writes,
no CLI — those are Phase B/C.
"""

from __future__ import annotations

from auto_applier.inbox.classify import (
    EmailClass,
    classify,
    classify_deterministic,
)
from auto_applier.inbox.match import MatchResult, match_email
from auto_applier.inbox.parse import FetchedEmail, parse_message

__all__ = [
    "FetchedEmail",
    "parse_message",
    "EmailClass",
    "classify",
    "classify_deterministic",
    "MatchResult",
    "match_email",
]
