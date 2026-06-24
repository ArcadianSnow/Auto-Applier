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


# ---- durability (follow-up: durable batch state) ----------------------------

def test_no_path_writes_nothing(tmp_path):
    """Without a ``path`` the batch is pure in-memory — no sidecar appears."""
    b = ReviewBatch(size=2, path=None)
    b.add("j1")
    assert list(tmp_path.iterdir()) == []   # nothing written anywhere we control


def test_persists_and_restores_members_and_dispositions(tmp_path):
    """A new ReviewBatch over the same sidecar resumes the grouping: members, their
    dispositions, and the batch id all survive a (simulated) restart."""
    path = tmp_path / "review_batch.json"
    b = ReviewBatch(size=2, path=path)
    b.add("j1")
    b.add("j2")
    b.dispose("j1", "applied")
    saved_id = b.batch_id
    assert path.exists()

    restored = ReviewBatch(size=2, path=path)   # "restart"
    snap = restored.snapshot()
    assert snap["batch_id"] == saved_id
    assert snap["members"] == ["j1", "j2"]
    assert snap["dispositions"] == {"j1": "applied", "j2": "pending"}
    assert restored.is_holding() is True        # full, one still pending — hold resumes
    assert restored.pending == 1


def test_release_persists_empty_batch(tmp_path):
    """A released batch persists as empty with the fresh id, so a restart starts clean."""
    path = tmp_path / "review_batch.json"
    b = ReviewBatch(size=2, path=path)
    b.add("j1")
    b.add("j2")
    new_id = b.release()

    restored = ReviewBatch(size=2, path=path)
    assert restored.count == 0
    assert restored.batch_id == new_id
    assert restored.is_holding() is False


def test_size_comes_from_config_not_file(tmp_path):
    """``size`` is never restored from the sidecar — changing batch_review_size takes effect on
    restart while the in-flight grouping is preserved."""
    path = tmp_path / "review_batch.json"
    b = ReviewBatch(size=2, path=path)
    b.add("j1")
    b.add("j2")
    assert b.is_full() is True              # 2/2 under the old size

    restored = ReviewBatch(size=5, path=path)   # config bumped to 5
    assert restored.size == 5
    assert restored.count == 2                  # members preserved
    assert restored.is_full() is False          # 2 < 5 → room for more


def test_corrupt_sidecar_yields_empty_batch(tmp_path):
    """A corrupt / non-JSON sidecar must never raise — it just starts empty."""
    path = tmp_path / "review_batch.json"
    path.write_text("{not json", encoding="utf-8")
    b = ReviewBatch(size=2, path=path)
    assert b.count == 0


def test_load_coerces_unknown_disposition_to_pending(tmp_path):
    """A hand-edited file can't smuggle an out-of-vocabulary disposition into the barrier."""
    import json
    path = tmp_path / "review_batch.json"
    path.write_text(
        json.dumps({"batch_id": "b1", "members": {"j1": "bogus", "j2": "applied"}}),
        encoding="utf-8",
    )
    b = ReviewBatch(size=2, path=path)
    assert b.snapshot()["dispositions"] == {"j1": "pending", "j2": "applied"}


def test_persistence_is_best_effort_on_io_error(tmp_path):
    """A read/write failure on the sidecar is swallowed; the in-memory barrier stays correct."""
    bad = tmp_path / "review_batch.json"
    bad.mkdir()                              # a directory where a file is expected
    b = ReviewBatch(size=1, path=bad)        # _load swallows the read error
    assert b.count == 0
    assert b.add("j1") is True               # _persist_locked swallows the write error
    assert b.count == 1
