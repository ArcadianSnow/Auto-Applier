"""InboxMessageRepo round-trips (email-outcome-loop Phase B).

Offline, against a temp ``init_app_db`` (the ``conn`` fixture). Covers idempotency
(is_processed/mark_processed), the review queue (list_for_review), and the per-folder
fetch cursor (last_uid/set_last_uid).
"""

from __future__ import annotations

import pytest

from auto_applier.inbox.repo import InboxMessageRepo


def test_is_processed_false_then_true(conn):
    repo = InboxMessageRepo(conn)
    assert repo.is_processed("<m-1@x>") is False
    repo.mark_processed("<m-1@x>", action="ignored")
    assert repo.is_processed("<m-1@x>") is True


def test_mark_processed_records_fields(conn):
    repo = InboxMessageRepo(conn)
    msg = repo.mark_processed(
        "<m-2@x>", matched_job_id="job-abc", kind="response", action="outcome"
    )
    assert msg.action == "outcome"
    assert msg.matched_job_id == "job-abc"
    assert msg.kind == "response"
    assert msg.noted_at  # a timestamp was set
    assert repo.is_processed("<m-2@x>") is True


def test_mark_processed_rejects_bad_action(conn):
    repo = InboxMessageRepo(conn)
    with pytest.raises(ValueError):
        repo.mark_processed("<m-3@x>", action="bogus")


def test_mark_processed_is_idempotent_on_message_id(conn):
    """A re-mark updates the existing row (no PK conflict, no duplicate)."""
    repo = InboxMessageRepo(conn)
    repo.mark_processed("<m-4@x>", action="review", matched_job_id=None)
    repo.mark_processed("<m-4@x>", action="outcome", matched_job_id="job-z", kind="offer")
    rows = conn.execute(
        "SELECT message_id, action, matched_job_id, kind FROM inbox_messages "
        "WHERE message_id = ?",
        ("<m-4@x>",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "outcome"
    assert rows[0]["matched_job_id"] == "job-z"
    assert rows[0]["kind"] == "offer"


def test_list_for_review_only_review_rows(conn):
    repo = InboxMessageRepo(conn)
    repo.mark_processed("<rev-1@x>", action="review", matched_job_id=None)
    repo.mark_processed("<rev-2@x>", action="review", matched_job_id="job-q")
    repo.mark_processed("<out-1@x>", action="outcome", matched_job_id="job-r", kind="response")
    repo.mark_processed("<ign-1@x>", action="ignored")

    review = repo.list_for_review()
    ids = {m.message_id for m in review}
    assert ids == {"<rev-1@x>", "<rev-2@x>"}
    assert all(m.action == "review" for m in review)


def test_last_uid_round_trip(conn):
    repo = InboxMessageRepo(conn)
    assert repo.last_uid("INBOX") is None
    repo.set_last_uid("INBOX", 42)
    assert repo.last_uid("INBOX") == 42
    # Upsert: a higher cursor replaces the old one.
    repo.set_last_uid("INBOX", 99)
    assert repo.last_uid("INBOX") == 99
    # A different folder is tracked independently.
    assert repo.last_uid("Archive") is None
    repo.set_last_uid("Archive", 7)
    assert repo.last_uid("Archive") == 7
    assert repo.last_uid("INBOX") == 99
