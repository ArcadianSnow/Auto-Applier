"""Job + application state machines (spec §5).

All lifecycle transitions live HERE, in one allowed-transitions table — so dedup,
continuous-run resumption, and retries become queries on ``state`` rather than bespoke
logic scattered across the pipeline (the v2 root cause this fixes).

Invariants encoded here:
  * ``APPLIED`` is terminal and requires a positive submit confirmation (set by the
    apply worker, never inferred from a click).
  * A mid-form break fails fast: ``APPLYING → FAILED → REVIEW`` (no retry loop).
  * A crashed run leaves jobs in ``APPLYING``; a restart sweep re-queues or fails them.
"""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    """Lifecycle of a single job (spec §5 diagram)."""

    DISCOVERED = "DISCOVERED"
    SKIPPED = "SKIPPED"        # dedup / ghost / below comp floor / decided-skip (terminal)
    FILTERED = "FILTERED"      # lost the embedding pre-filter (terminal, ephemeral)
    DESCRIBED = "DESCRIBED"    # full JD scraped — score on full text, never a snippet
    SCORED = "SCORED"
    DECIDED = "DECIDED"
    QUEUED_APPLY = "QUEUED_APPLY"  # passed the optimize+Strict gate (résumé+CL+guard)
    REVIEW = "REVIEW"          # needs a human (guard flag, novel question, FAILED apply)
    APPLYING = "APPLYING"      # apply in flight (a crash leaves jobs here)
    APPLIED = "APPLIED"        # positive confirmation only (terminal; dedup source of truth)
    FAILED = "FAILED"          # no confirmation / mid-form break → routes to REVIEW


#: Terminal states — no outgoing transitions in normal flow.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.APPLIED, JobState.SKIPPED, JobState.FILTERED}
)

#: Ephemeral states eligible for retention pruning (spec §4). APPLIED is kept forever.
EPHEMERAL_STATES: frozenset[JobState] = frozenset(
    {JobState.SKIPPED, JobState.FILTERED}
)

#: Allowed forward transitions. Anything not listed raises in ``transition()``.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED: frozenset(
        {JobState.SKIPPED, JobState.FILTERED, JobState.DESCRIBED}
    ),
    JobState.DESCRIBED: frozenset({JobState.SCORED}),
    JobState.SCORED: frozenset({JobState.DECIDED}),
    # optimize+Strict gate sits on the DECIDED edge: pass→QUEUED_APPLY, fail→REVIEW.
    JobState.DECIDED: frozenset(
        {JobState.QUEUED_APPLY, JobState.REVIEW, JobState.SKIPPED}
    ),
    JobState.QUEUED_APPLY: frozenset({JobState.APPLYING, JobState.REVIEW}),
    # confirmation→APPLIED; no-confirm/break→FAILED; crash-sweep→QUEUED_APPLY re-queue.
    JobState.APPLYING: frozenset(
        {JobState.APPLIED, JobState.FAILED, JobState.QUEUED_APPLY}
    ),
    JobState.FAILED: frozenset({JobState.REVIEW, JobState.QUEUED_APPLY}),
    # REVIEW is human-driven; a human may queue it for (assisted) apply or skip it.
    JobState.REVIEW: frozenset({JobState.QUEUED_APPLY, JobState.SKIPPED}),
    JobState.APPLIED: frozenset(),
    JobState.SKIPPED: frozenset(),
    JobState.FILTERED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a state transition is not in the allowed table."""


def can_transition(src: JobState, dst: JobState) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def transition(src: JobState, dst: JobState) -> JobState:
    """Validate and return ``dst``. Raises :class:`InvalidTransition` if disallowed.

    This is the single chokepoint every state change must go through.
    """
    if not can_transition(src, dst):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS.get(src, frozenset()))
        raise InvalidTransition(
            f"{src.value} → {dst.value} is not allowed (allowed from {src.value}: {allowed or 'none — terminal'})"
        )
    return dst


class ApplyMode(str, Enum):
    """How an application is submitted (spec §6, §8)."""

    BROWSER_AUTO = "browser_auto"        # bot fills + submits on a clean ATS form
    BROWSER_ASSISTED = "browser_assisted"  # bot pre-fills, human clicks submit


class ApplicationStatus(str, Enum):
    """Status of a single apply attempt (spec §4 / §8b).

    Distinct from :class:`JobState`: a job may have multiple attempts (an UNCONFIRMED
    attempt is safely retryable because dedup keys only off the APPLIED *job* state).
    """

    APPLYING = "APPLYING"
    APPLIED = "APPLIED"            # positive on-page confirmation detected
    UNCONFIRMED = "UNCONFIRMED"    # submitted but no positive signal → REVIEW, retry-safe
    FAILED = "FAILED"             # mid-form break / validation error → REVIEW
    ASSISTED_PENDING = "ASSISTED_PENDING"  # pre-filled, awaiting human submit click
