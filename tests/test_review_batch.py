"""ReviewBatch — the batch barrier for batched assisted review, Phase 2.

Covers: fill-to-N → holds; under N → doesn't hold; idempotent membership; release opens a fresh
empty (non-holding) batch with a new id; size clamp; snapshot shape.
"""

from __future__ import annotations

import pytest

from auto_applier.pipeline.review_batch import ReviewBatch


def test_default_size_is_five():
    assert ReviewBatch().size == 5


def test_size_clamped_to_at_least_one():
    assert ReviewBatch(size=0).size == 1
    assert ReviewBatch(size=-3).size == 1


def test_holds_only_once_full():
    b = ReviewBatch(size=3)
    assert b.is_holding() is False
    assert b.add("j1") is False          # 1/3
    assert b.add("j2") is False          # 2/3
    assert b.is_holding() is False
    assert b.add("j3") is True           # 3/3 → full
    assert b.is_full() is True
    assert b.is_holding() is True
    assert b.count == 3


def test_membership_is_idempotent():
    b = ReviewBatch(size=2)
    b.add("j1")
    b.add("j1")                          # same job counts once
    assert b.count == 1
    assert b.is_holding() is False
    assert b.add("j2") is True
    assert b.count == 2


def test_blank_job_id_ignored():
    b = ReviewBatch(size=1)
    assert b.add("") is False
    assert b.count == 0


def test_release_opens_fresh_empty_batch_with_new_id():
    b = ReviewBatch(size=2)
    first_id = b.batch_id
    b.add("j1")
    b.add("j2")
    assert b.is_holding() is True

    new_id = b.release()
    assert new_id != first_id
    assert b.batch_id == new_id
    assert b.count == 0
    assert b.is_holding() is False       # the hold is lifted → apply resumes


def test_snapshot_shape():
    b = ReviewBatch(size=2)
    b.add("j2")
    b.add("j1")
    snap = b.snapshot()
    assert snap == {
        "batch_id": b.batch_id,
        "size": 2,
        "count": 2,
        "members": ["j1", "j2"],         # sorted
        "dispositions": {"j1": "pending", "j2": "pending"},
        "pending": 2,
        "all_dispositioned": False,
        "holding": True,
    }


# ---- disposition + advance (Phase 4) ----------------------------------------

def test_dispose_records_and_unblocks_when_all_dispositioned():
    b = ReviewBatch(size=2)
    b.add("j1")
    b.add("j2")
    assert b.is_holding() is True          # full, both pending
    assert b.dispose("j1", "applied") is False   # one still pending
    assert b.is_holding() is True
    assert b.pending == 1
    assert b.dispose("j2", "skipped") is True     # now all dispositioned
    assert b.all_dispositioned() is True
    assert b.is_holding() is False          # hold lifts → worker will advance
    assert b.snapshot()["dispositions"] == {"j1": "applied", "j2": "skipped"}


def test_dispose_unknown_value_raises():
    b = ReviewBatch(size=1)
    b.add("j1")
    with pytest.raises(ValueError):
        b.dispose("j1", "maybe")


def test_dispose_non_member_is_noop():
    b = ReviewBatch(size=2)
    b.add("j1")
    assert b.dispose("ghost", "applied") is False  # not a member; nothing changes
    assert b.snapshot()["dispositions"] == {"j1": "pending"}


def test_add_does_not_reset_an_existing_disposition():
    b = ReviewBatch(size=2)
    b.add("j1")
    b.dispose("j1", "applied")
    b.add("j1")                              # idempotent re-add must not revert to pending
    assert b.snapshot()["dispositions"]["j1"] == "applied"


def test_all_dispositioned_false_when_empty():
    assert ReviewBatch(size=2).all_dispositioned() is False


def test_partial_batch_advances_when_all_dispositioned():
    # A partial (not full) batch that the owner fully dispositions is also "ready to advance".
    b = ReviewBatch(size=5)
    b.add("j1")
    b.dispose("j1", "needs_work")
    assert b.is_full() is False
    assert b.all_dispositioned() is True
    assert b.is_holding() is False
