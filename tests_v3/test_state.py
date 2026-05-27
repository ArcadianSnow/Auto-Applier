"""Job state machine (spec §5) — the load-bearing transition table."""

from __future__ import annotations

import pytest

from av3.domain.state import (
    EPHEMERAL_STATES,
    TERMINAL_STATES,
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
