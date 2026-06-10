"""Job state machine (spec §5) — the load-bearing transition table."""

from __future__ import annotations

import pytest

from auto_applier.domain.state import (
    EPHEMERAL_STATES,
    TERMINAL_STATES,
    ApplyMode,
    InvalidTransition,
    JobState,
    can_transition,
    transition,
)


def test_happy_path_to_applied():
    path = [
        JobState.DISCOVERED, JobState.DESCRIBED, JobState.SCORED,
        JobState.DECIDED, JobState.QUEUED_APPLY, JobState.APPLYING, JobState.APPLIED,
    ]
    for src, dst in zip(path, path[1:]):
        assert can_transition(src, dst)
        assert transition(src, dst) is dst


def test_decided_can_branch():
    for dst in (JobState.QUEUED_APPLY, JobState.REVIEW, JobState.SKIPPED):
        assert can_transition(JobState.DECIDED, dst)


def test_apply_failure_routes_to_review():
    # mid-form break: APPLYING → FAILED → REVIEW (no retry loop)
    assert can_transition(JobState.APPLYING, JobState.FAILED)
    assert can_transition(JobState.FAILED, JobState.REVIEW)


def test_crash_sweep_can_requeue_applying():
    # a crashed run leaves jobs in APPLYING; sweep re-queues or fails them (spec §5)
    assert can_transition(JobState.APPLYING, JobState.QUEUED_APPLY)
    assert can_transition(JobState.APPLYING, JobState.FAILED)


def test_assisted_pending_goes_directly_to_review():
    # ASSISTED_PENDING is a deliberate handoff (bot pre-fills, human submits) — not a
    # failure. The apply worker uses APPLYING → REVIEW directly so the event spine
    # doesn't record it as an error. UNCONFIRMED/FAILED still route through FAILED.
    assert can_transition(JobState.APPLYING, JobState.REVIEW)


def test_terminal_states_have_no_exits():
    for term in TERMINAL_STATES:
        assert transition_targets(term) == set()


def transition_targets(state: JobState) -> set:
    return {s for s in JobState if can_transition(state, s)}


def test_applied_is_terminal():
    assert JobState.APPLIED in TERMINAL_STATES
    with pytest.raises(InvalidTransition):
        transition(JobState.APPLIED, JobState.REVIEW)


def test_cannot_skip_describe():
    # can't jump DISCOVERED straight to SCORED — must DESCRIBE (score on full JD)
    with pytest.raises(InvalidTransition):
        transition(JobState.DISCOVERED, JobState.SCORED)


def test_invalid_transition_message_lists_allowed():
    with pytest.raises(InvalidTransition, match="DESCRIBED"):
        transition(JobState.DISCOVERED, JobState.APPLIED)


def test_ephemeral_subset_of_terminal():
    assert EPHEMERAL_STATES <= TERMINAL_STATES
    assert JobState.APPLIED not in EPHEMERAL_STATES  # kept forever (dedup truth)


# --- manual / human-apply mode (spec §5): DECIDED/REVIEW → APPLIED ----------------

def test_manual_apply_edges_from_decided_and_review():
    # A human attesting "I applied" is a positive confirmation, not a click inference,
    # so DECIDED → APPLIED and REVIEW → APPLIED are allowed (the `av3 applied` path).
    assert can_transition(JobState.DECIDED, JobState.APPLIED)
    assert transition(JobState.DECIDED, JobState.APPLIED) is JobState.APPLIED
    assert can_transition(JobState.REVIEW, JobState.APPLIED)
    assert transition(JobState.REVIEW, JobState.APPLIED) is JobState.APPLIED


def test_applied_still_terminal_after_manual_edges():
    # Adding inbound edges must not give APPLIED any outbound ones.
    assert transition_targets(JobState.APPLIED) == set()
    assert JobState.APPLIED in TERMINAL_STATES


def test_manual_apply_does_not_open_illegal_shortcuts():
    # Only DECIDED/REVIEW may jump to APPLIED — not earlier pipeline states.
    for src in (JobState.DISCOVERED, JobState.DESCRIBED, JobState.SCORED,
                JobState.SKIPPED, JobState.FILTERED):
        with pytest.raises(InvalidTransition):
            transition(src, JobState.APPLIED)


def test_apply_mode_manual_round_trips():
    assert ApplyMode("manual") is ApplyMode.MANUAL
    assert ApplyMode.MANUAL.value == "manual"
