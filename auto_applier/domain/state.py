"""Job + application state machines (spec §5).

All lifecycle transitions live HERE, in one allowed-transitions table — so dedup,
continuous-run resumption, and retries become queries on ``state`` rather than bespoke
logic scattered across the pipeline (the v2 root cause this fixes).

Invariants encoded here:
  * ``APPLIED`` is terminal and requires a positive confirmation — either the apply
    worker's on-page submit confirmation, OR an explicit human attestation via
    ``av3 applied`` (the manual operating mode: discover+score only, human applies
    externally). A human explicitly attesting "I applied" is a positive confirmation,
    not an inference from a click — so it honors the same invariant. Hence the
    ``DECIDED → APPLIED`` and ``REVIEW → APPLIED`` edges below (manual mode), alongside
    the bot's ``APPLYING → APPLIED``.
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
    # APPLIED here is the manual mode (human applied externally, `av3 applied`).
    JobState.DECIDED: frozenset(
        {JobState.QUEUED_APPLY, JobState.REVIEW, JobState.SKIPPED, JobState.APPLIED}
    ),
    JobState.QUEUED_APPLY: frozenset({JobState.APPLYING, JobState.REVIEW}),
    # confirmation→APPLIED; no-confirm/break→FAILED; crash-sweep→QUEUED_APPLY re-queue.
    # ASSISTED_PENDING (bot pre-fills, human submits) goes straight to REVIEW — the
    # auto attempt deliberately handed off; not a failure, so going through FAILED
    # would muddy the event spine. UNCONFIRMED/FAILED still route via FAILED (spec §5).
    JobState.APPLYING: frozenset(
        {JobState.APPLIED, JobState.FAILED, JobState.REVIEW, JobState.QUEUED_APPLY}
    ),
    JobState.FAILED: frozenset({JobState.REVIEW, JobState.QUEUED_APPLY}),
    # REVIEW is human-driven; a human may queue it for (assisted) apply, skip it, or
    # attest a manual apply (`av3 applied`) → APPLIED.
    JobState.REVIEW: frozenset({JobState.QUEUED_APPLY, JobState.SKIPPED, JobState.APPLIED}),
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
    MANUAL = "manual"                    # human applied externally; recorded via `av3 applied`


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


class OutcomeKind(str, Enum):
    """A recorded post-apply outcome (spec §8e outcome feedback loop).

    Ordered by funnel depth (``rank``) so analytics can derive a job's *furthest-reached*
    stage from several recorded outcomes. ``GHOST`` and ``REJECTION`` are terminal-negative;
    ``RESPONSE`` < ``INTERVIEW`` < ``OFFER`` is the positive ladder. A ``GHOST`` (employer
    never responded) ranks below ``RESPONSE`` — it's the "applied, heard nothing" signal the
    ghost-job detector wants.
    """

    GHOST = "ghost"             # no response after a long wait (the §8e ghost signal)
    REJECTION = "rejection"     # explicit no
    RESPONSE = "response"       # any human reply / acknowledgement
    INTERVIEW = "interview"     # advanced to an interview
    OFFER = "offer"             # received an offer

    @property
    def rank(self) -> int:
        """Funnel depth for "furthest reached" comparisons. Higher = deeper."""
        return _OUTCOME_RANK[self]

    @property
    def is_positive(self) -> bool:
        """A conversion signal (got a real human response or better)."""
        return self in (OutcomeKind.RESPONSE, OutcomeKind.INTERVIEW, OutcomeKind.OFFER)


#: Funnel depth ranking. GHOST is the floor (worse than an explicit rejection for the
#: ghost detector's purposes — "the posting may not be real"); the positive ladder climbs.
_OUTCOME_RANK: dict["OutcomeKind", int] = {
    OutcomeKind.GHOST: 0,
    OutcomeKind.REJECTION: 1,
    OutcomeKind.RESPONSE: 2,
    OutcomeKind.INTERVIEW: 3,
    OutcomeKind.OFFER: 4,
}
