"""Contract tests for the telemetry mirror queue (Phase 5 2/M, spec §9).

What this file owns:
  * Category-shaped scrubbers (``scrub_error_event`` / ``scrub_inferred_answer_event``).
  * The ``MirrorQueue`` spool: enqueue / next_due / mark_delivered / mark_failed /
    backoff / pending counts / prune.
  * Sink wiring: ``EventSink.emit`` enqueues only when a :class:`MirrorPolicy` is
    attached AND enabled, and only for the two §9 mirror categories.

What this file intentionally does NOT cover:
  * The HTTP relay client itself — that's Phase 5 (4/M).
  * The ``cli telemetry on|off|status`` UX — that's (3/M).
  * The text-level ``scrub()`` helper — already covered in ``test_events.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_applier.telemetry import (
    EventSink,
    MirrorPolicy,
    MirrorQueue,
    QueuedMirrorRow,
    attach_mirror_from_settings,
    scrub_error_event,
    scrub_inferred_answer_event,
    user_id_from_handle,
)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def events_db(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest.fixture
def sink(events_db: Path):
    s = EventSink(events_db)
    yield s
    s.close()


@pytest.fixture
def opted_in_policy() -> MirrorPolicy:
    return MirrorPolicy(enabled=True, user_id="a3f9c1d204", app_version="3.0.0a0")


# ============================================================== scrubbers

class TestScrubErrorEvent:
    """Spec §9 (a): {user_id, app_version, stage, platform, error_type,
    scrubbed_error_msg, ts}. Anything else dropped."""

    def test_shape_matches_spec(self):
        out = scrub_error_event({
            "user_id": "abc1234567",
            "app_version": "3.0.0a0",
            "stage": "apply",
            "platform": "lever",
            "error_type": "TimeoutError",
            "error_msg": "timed out waiting on selector",
            "ts": "2026-05-29T12:00:00",
        })
        assert set(out.keys()) <= {
            "user_id", "app_version", "stage", "platform",
            "error_type", "scrubbed_error_msg", "ts",
        }
        assert out["scrubbed_error_msg"] == "timed out waiting on selector"

    def test_error_msg_is_pii_scrubbed(self):
        out = scrub_error_event({
            "user_id": "x",
            "stage": "apply",
            "error_msg": "submit failed for me@example.com at C:\\Users\\jane\\resume.pdf",
            "ts": "2026-05-29T12:00:00",
        })
        msg = out["scrubbed_error_msg"]
        assert "me@example.com" not in msg
        assert "[email]" in msg
        assert "jane" not in msg

    def test_unknown_keys_are_dropped(self):
        # job_id / error_msg / context / leaked_pii / etc. — all NOT in the spec schema
        out = scrub_error_event({
            "user_id": "x",
            "stage": "score",
            "error_type": "ValueError",
            "error_msg": "bad",
            "ts": "2026-05-29T12:00:00",
            "job_id": "job-123",
            "leaked_pii": "name@example.com",
            "context": {"weird": "data"},
        })
        assert "job_id" not in out
        assert "leaked_pii" not in out
        assert "context" not in out

    def test_none_values_stripped(self):
        out = scrub_error_event({
            "user_id": "x",
            "stage": "apply",
            "platform": None,           # not set on every event
            "error_type": "X",
            "error_msg": "boom",
            "ts": "t",
        })
        assert "platform" not in out

    def test_long_error_type_truncated(self):
        # error_type is a class name; nobody ships 1000-char class names, but
        # defence in depth keeps the wire payload bounded.
        out = scrub_error_event({
            "user_id": "x",
            "stage": "apply",
            "error_type": "X" * 1000,
            "error_msg": "boom",
            "ts": "t",
        })
        assert len(out["error_type"]) <= 600  # 500 + truncation tail


class TestScrubInferredAnswerEvent:
    """Spec §9 (b): {user_id, question_text, category, confidence, outcome, ts}.
    THE ANSWER VALUE MUST NEVER APPEAR."""

    def test_shape_matches_spec(self):
        out = scrub_inferred_answer_event({
            "user_id": "abc1234567",
            "question_text": "Do you require sponsorship?",
            "category": "sponsorship",
            "confidence": 0.82,
            "outcome": "answered",
            "ts": "2026-05-29T12:00:00",
        })
        assert set(out.keys()) <= {
            "user_id", "question_text", "category",
            "confidence", "outcome", "ts",
        }

    def test_answer_value_is_dropped_even_if_passed(self):
        """The spec's most load-bearing guarantee: even if a future call site
        accidentally includes the candidate's answer, the scrubber drops it."""
        out = scrub_inferred_answer_event({
            "user_id": "x",
            "question_text": "What's your name?",
            "category": "none",
            "confidence": 0.9,
            "outcome": "answered",
            "answer": "Jane Doe",                  # MUST NOT appear in output
            "value": "Jane Doe",                   # different shape, same risk
            "resolved_to": "Jane Doe",
            "ts": "t",
        })
        assert "answer" not in out
        assert "value" not in out
        assert "resolved_to" not in out
        assert "Jane Doe" not in json.dumps(out)

    def test_eeo_category_drops_row_entirely(self):
        """Spec §8d: EEO rows do not mirror at all (the *metadata* is sensitive
        too — "asked an EEO question" tells the relay you saw a demographics
        form). Apply-worker already filters upstream; this is defence in depth.
        """
        out = scrub_inferred_answer_event({
            "user_id": "x",
            "question_text": "What is your gender?",
            "category": "eeo",
            "confidence": 0.7,
            "outcome": "answered",
            "ts": "t",
        })
        assert out == {}

    def test_question_text_is_pii_scrubbed(self):
        # Unlikely a question label contains PII, but the form could prepopulate
        # the candidate's name into the label ("Hi Jane, do you authorize…")
        out = scrub_inferred_answer_event({
            "user_id": "x",
            "question_text": "Jane (jane@example.com), are you authorized?",
            "category": "work_authorization",
            "confidence": 0.8,
            "outcome": "answered",
            "ts": "t",
        })
        assert "jane@example.com" not in out["question_text"]
        assert "[email]" in out["question_text"]

    def test_unknown_keys_are_dropped(self):
        out = scrub_inferred_answer_event({
            "user_id": "x",
            "question_text": "q",
            "category": "none",
            "confidence": 0.7,
            "outcome": "bailed",
            "ts": "t",
            "fact_bank_snapshot": {"resume": "30 pages"},   # huge payload risk
            "raw_llm_response": "sensitive",
        })
        assert "fact_bank_snapshot" not in out
        assert "raw_llm_response" not in out


# ============================================================ user_id_from_handle

class TestUserIdFromHandle:
    def test_stable(self):
        # The whole attribution model relies on this being deterministic.
        assert user_id_from_handle("Alice") == user_id_from_handle("Alice")

    def test_truncated_to_10(self):
        assert len(user_id_from_handle("Alice")) == 10

    def test_whitespace_normalized(self):
        # Onboarding inputs commonly trail a newline.
        assert user_id_from_handle("Alice") == user_id_from_handle("  Alice\n")

    def test_different_handles_yield_different_ids(self):
        assert user_id_from_handle("Alice") != user_id_from_handle("Bob")


# =============================================================== MirrorQueue

class TestMirrorQueueEnqueue:
    def test_enqueue_error_persists_scrubbed_payload(self, sink: EventSink):
        row_id = sink.mirror_queue.enqueue("error", {
            "user_id": "u",
            "app_version": "3.0.0a0",
            "stage": "apply",
            "platform": "lever",
            "error_type": "TimeoutError",
            "error_msg": "boom",
            "ts": "2026-05-29T12:00:00",
        })
        assert row_id is not None
        row = sink.conn.execute(
            "SELECT category, payload_json, delivered_at FROM mirror_queue WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["category"] == "error"
        assert row["delivered_at"] is None
        payload = json.loads(row["payload_json"])
        assert payload["scrubbed_error_msg"] == "boom"

    def test_enqueue_inferred_answer_persists_scrubbed_payload(self, sink: EventSink):
        row_id = sink.mirror_queue.enqueue("inferred_answer", {
            "user_id": "u",
            "question_text": "Are you authorized?",
            "category": "work_authorization",
            "confidence": 0.81,
            "outcome": "answered",
            "ts": "2026-05-29T12:00:00",
        })
        assert row_id is not None

    def test_enqueue_eeo_inferred_answer_returns_none(self, sink: EventSink):
        # EEO row scrubbed to empty dict -> queue returns None and writes nothing
        row_id = sink.mirror_queue.enqueue("inferred_answer", {
            "user_id": "u",
            "question_text": "Gender?",
            "category": "eeo",
            "confidence": 0.9,
            "outcome": "answered",
            "ts": "t",
        })
        assert row_id is None
        count = sink.conn.execute(
            "SELECT COUNT(*) AS n FROM mirror_queue"
        ).fetchone()["n"]
        assert count == 0

    def test_enqueue_unknown_category_raises(self, sink: EventSink):
        with pytest.raises(ValueError, match="unknown mirror category"):
            sink.mirror_queue.enqueue("audit_trail", {"user_id": "u"})


class TestMirrorQueueDrainage:
    def _seed(self, q: MirrorQueue, n: int) -> list[int]:
        ids: list[int] = []
        for i in range(n):
            rid = q.enqueue("error", {
                "user_id": "u",
                "stage": "apply",
                "error_type": "X",
                "error_msg": f"boom {i}",
                "ts": "t",
            })
            ids.append(rid)
        return ids

    def test_next_due_returns_oldest_first(self, sink: EventSink):
        ids = self._seed(sink.mirror_queue, 3)
        due = sink.mirror_queue.next_due()
        assert [r.id for r in due] == ids  # ascending == enqueue order

    def test_next_due_respects_limit(self, sink: EventSink):
        self._seed(sink.mirror_queue, 5)
        due = sink.mirror_queue.next_due(limit=2)
        assert len(due) == 2

    def test_next_due_skips_delivered(self, sink: EventSink):
        ids = self._seed(sink.mirror_queue, 3)
        sink.mirror_queue.mark_delivered(ids[1])
        due_ids = [r.id for r in sink.mirror_queue.next_due()]
        assert ids[1] not in due_ids

    def test_next_due_skips_not_yet_due(self, sink: EventSink):
        ids = self._seed(sink.mirror_queue, 2)
        # Push id[0] far into the future so it should not appear at now.
        sink.conn.execute(
            "UPDATE mirror_queue SET next_retry_at = '2099-01-01T00:00:00' WHERE id = ?",
            (ids[0],),
        )
        due_ids = [r.id for r in sink.mirror_queue.next_due()]
        assert ids[0] not in due_ids
        assert ids[1] in due_ids

    def test_mark_failed_bumps_attempts_and_pushes_next_retry(self, sink: EventSink):
        rid = self._seed(sink.mirror_queue, 1)[0]
        sink.mirror_queue.mark_failed(rid, "HTTP 503")
        row = sink.conn.execute(
            "SELECT attempts, last_error, next_retry_at FROM mirror_queue WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["attempts"] == 1
        assert "503" in row["last_error"]
        # next_retry_at should be in the future (relative to enqueue 'now')
        assert row["next_retry_at"] >= "2026"

    def test_mark_failed_caps_backoff_at_top_step(self, sink: EventSink):
        rid = self._seed(sink.mirror_queue, 1)[0]
        for _ in range(50):
            sink.mirror_queue.mark_failed(rid, "still failing")
        row = sink.conn.execute(
            "SELECT attempts FROM mirror_queue WHERE id = ?", (rid,)
        ).fetchone()
        assert row["attempts"] == 50  # no crash, no overflow

    def test_mark_delivered_clears_last_error(self, sink: EventSink):
        rid = self._seed(sink.mirror_queue, 1)[0]
        sink.mirror_queue.mark_failed(rid, "tmp")
        sink.mirror_queue.mark_delivered(rid)
        row = sink.conn.execute(
            "SELECT delivered_at, last_error FROM mirror_queue WHERE id = ?", (rid,)
        ).fetchone()
        assert row["delivered_at"] is not None
        assert row["last_error"] is None

    def test_mark_failed_truncates_long_reason(self, sink: EventSink):
        rid = self._seed(sink.mirror_queue, 1)[0]
        sink.mirror_queue.mark_failed(rid, "x" * 5000)
        row = sink.conn.execute(
            "SELECT last_error FROM mirror_queue WHERE id = ?", (rid,)
        ).fetchone()
        assert len(row["last_error"]) <= 250


class TestMirrorQueueIntrospection:
    def test_pending_and_delivered_counts(self, sink: EventSink):
        rids = [
            sink.mirror_queue.enqueue("error", {
                "user_id": "u", "stage": "apply", "error_type": "X",
                "error_msg": f"e{i}", "ts": "t",
            })
            for i in range(3)
        ]
        sink.mirror_queue.mark_delivered(rids[0])
        assert sink.mirror_queue.pending_count() == 2
        assert sink.mirror_queue.delivered_count() == 1

    def test_prune_delivered_only(self, sink: EventSink):
        rid_d = sink.mirror_queue.enqueue("error", {
            "user_id": "u", "stage": "apply", "error_type": "X",
            "error_msg": "e", "ts": "t",
        })
        rid_p = sink.mirror_queue.enqueue("error", {
            "user_id": "u", "stage": "apply", "error_type": "X",
            "error_msg": "e", "ts": "t",
        })
        sink.mirror_queue.mark_delivered(rid_d)
        # Park delivered_at well in the past so the WHERE clause hits.
        sink.conn.execute(
            "UPDATE mirror_queue SET delivered_at = '2020-01-01T00:00:00' WHERE id = ?",
            (rid_d,),
        )
        n = sink.mirror_queue.prune_delivered(keep_days=30)
        assert n == 1
        survivors = [
            r["id"] for r in sink.conn.execute("SELECT id FROM mirror_queue").fetchall()
        ]
        assert rid_p in survivors
        assert rid_d not in survivors


# ============================================================ sink wiring

class TestSinkEmitOptInGating:
    """The single most important contract: nothing mirrors when telemetry is off."""

    def test_no_policy_attached_writes_locally_only(self, sink: EventSink):
        sink.emit(stage="apply", status="error", error_type="X", error_msg="boom")
        assert sink.mirror_queue.pending_count() == 0
        assert len(sink.errors()) == 1  # local row still written

    def test_policy_disabled_writes_locally_only(self, sink: EventSink):
        sink.attach_mirror(MirrorPolicy(enabled=False, user_id="u", app_version="v"))
        sink.emit(stage="apply", status="error", error_type="X", error_msg="boom")
        assert sink.mirror_queue.pending_count() == 0

    def test_policy_enabled_mirrors_error(self, sink: EventSink, opted_in_policy):
        sink.attach_mirror(opted_in_policy)
        sink.emit(stage="apply", status="error", error_type="Timeout", error_msg="boom")
        assert sink.mirror_queue.pending_count() == 1
        row = sink.conn.execute(
            "SELECT category, payload_json FROM mirror_queue"
        ).fetchone()
        assert row["category"] == "error"
        payload = json.loads(row["payload_json"])
        assert payload["user_id"] == "a3f9c1d204"
        assert payload["app_version"] == "3.0.0a0"
        assert payload["scrubbed_error_msg"] == "boom"

    def test_policy_enabled_mirrors_resolver_inferred(self, sink: EventSink, opted_in_policy):
        sink.attach_mirror(opted_in_policy)
        sink.emit(
            stage="resolver_inferred", status="ok",
            platform="greenhouse", job_id="job-1",
            context={
                "question": "Are you legally authorized to work?",
                "category": "work_authorization",
                "confidence": 0.81,
                "outcome": "answered",
            },
        )
        assert sink.mirror_queue.pending_count() == 1
        row = sink.conn.execute(
            "SELECT category, payload_json FROM mirror_queue"
        ).fetchone()
        assert row["category"] == "inferred_answer"
        payload = json.loads(row["payload_json"])
        assert payload["question_text"] == "Are you legally authorized to work?"
        assert payload["category"] == "work_authorization"
        # NEVER the answer value:
        assert "answer" not in payload
        assert "value" not in payload

    def test_status_ok_non_resolver_does_not_mirror(self, sink: EventSink, opted_in_policy):
        sink.attach_mirror(opted_in_policy)
        sink.emit(stage="apply", status="ok", duration_ms=120)
        assert sink.mirror_queue.pending_count() == 0

    def test_status_skip_does_not_mirror(self, sink: EventSink, opted_in_policy):
        sink.attach_mirror(opted_in_policy)
        sink.emit(stage="dedup", status="skip", context={"reason": "duplicate"})
        assert sink.mirror_queue.pending_count() == 0

    def test_detach_mirror_silences_subsequent_emits(self, sink: EventSink, opted_in_policy):
        sink.attach_mirror(opted_in_policy)
        sink.emit(stage="apply", status="error", error_type="X", error_msg="first")
        sink.detach_mirror()
        sink.emit(stage="apply", status="error", error_type="X", error_msg="second")
        assert sink.mirror_queue.pending_count() == 1  # only the first

    def test_resolver_inferred_with_eeo_category_does_not_mirror(
        self, sink: EventSink, opted_in_policy
    ):
        """Defence in depth — apply_worker already filters EEO upstream, but
        if a regression leaks one through, the scrubber drops it."""
        sink.attach_mirror(opted_in_policy)
        sink.emit(
            stage="resolver_inferred", status="ok",
            context={
                "question": "What is your gender?",
                "category": "eeo",
                "confidence": 0.9,
                "outcome": "answered",
            },
        )
        assert sink.mirror_queue.pending_count() == 0


# ============================================================ attach_mirror_from_settings

class TestAttachMirrorFromSettings:
    """The CLI helper that ties Settings -> attached policy."""

    def test_disabled_telemetry_attaches_disabled_policy(self, sink: EventSink, settings):
        # Default settings have telemetry.enabled=False.
        policy = attach_mirror_from_settings(sink, settings)
        assert policy.enabled is False
        assert sink.mirror_policy is policy

    def test_enabled_telemetry_with_handle_attaches_user_id(
        self, sink: EventSink, settings
    ):
        settings.telemetry.enabled = True
        settings.telemetry.handle = "Alice"
        policy = attach_mirror_from_settings(sink, settings)
        assert policy.enabled is True
        assert policy.user_id == user_id_from_handle("Alice")
        assert len(policy.user_id) == 10

    def test_enabled_without_handle_falls_back_to_anonymous(
        self, sink: EventSink, settings
    ):
        # The 3/M onboarding flow asks for the handle, but in the interim a user
        # could flip enabled=True without setting a handle; we don't crash.
        settings.telemetry.enabled = True
        settings.telemetry.handle = None
        policy = attach_mirror_from_settings(sink, settings)
        assert policy.user_id == "anonymous"
