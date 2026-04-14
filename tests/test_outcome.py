"""Tests for analysis/outcome.py — application outcome tracking."""

from datetime import datetime, timedelta, timezone

import pytest

from auto_applier.analysis.outcome import (
    CLOSED_OUTCOMES,
    GHOST_DAYS,
    VALID_OUTCOMES,
    auto_mark_ghosted,
    outcome_summary,
    set_outcome,
)
from auto_applier.storage.models import Application
from auto_applier.storage import repository


@pytest.fixture
def isolated_apps(tmp_path, monkeypatch):
    monkeypatch.setitem(repository._CSV_MAP, Application, tmp_path / "applications.csv")
    return tmp_path


def _app(
    job_id="j1",
    status="applied",
    source="indeed",
    outcome="pending",
    applied_at=None,
):
    if applied_at is None:
        applied_at = datetime.now(timezone.utc).isoformat()
    return Application(
        job_id=job_id,
        status=status,
        source=source,
        resume_used="test",
        score=8,
        outcome=outcome,
        applied_at=applied_at,
    )


# ------------------------------------------------------------------
# set_outcome
# ------------------------------------------------------------------

class TestSetOutcome:
    def test_valid_outcome_updates(self, isolated_apps):
        repository.save(_app())
        result = set_outcome("j1", "interview")
        assert result is not None
        assert result.outcome == "interview"
        assert result.outcome_at  # timestamp set

        # Persisted correctly
        reloaded = repository.load_all(Application)
        assert reloaded[0].outcome == "interview"

    def test_invalid_outcome_raises(self, isolated_apps):
        with pytest.raises(ValueError):
            set_outcome("j1", "not_a_real_outcome")

    def test_no_match_returns_none(self, isolated_apps):
        result = set_outcome("nonexistent", "interview")
        assert result is None

    def test_source_narrowing(self, isolated_apps):
        repository.save(_app(job_id="j1", source="indeed"))
        repository.save(_app(job_id="j1", source="dice"))
        # Narrow match to indeed
        result = set_outcome("j1", "interview", source="indeed")
        assert result is not None
        assert result.source == "indeed"

        apps = repository.load_all(Application)
        by_src = {a.source: a.outcome for a in apps}
        assert by_src["indeed"] == "interview"
        assert by_src["dice"] == "pending"  # untouched

    def test_skipped_rows_ignored(self, isolated_apps):
        repository.save(_app(status="skipped"))
        result = set_outcome("j1", "interview")
        assert result is None

    def test_note_recorded(self, isolated_apps):
        repository.save(_app())
        result = set_outcome("j1", "rejected", note="generic rejection email")
        assert result is not None
        assert result.outcome_note == "generic rejection email"


# ------------------------------------------------------------------
# auto_mark_ghosted
# ------------------------------------------------------------------

class TestAutoMarkGhosted:
    def test_old_pending_marked_ghosted(self, isolated_apps):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        repository.save(_app(applied_at=old_date))

        count = auto_mark_ghosted()
        assert count == 1

        apps = repository.load_all(Application)
        assert apps[0].outcome == "ghosted"
        assert "30" in apps[0].outcome_note  # mentions the threshold

    def test_recent_pending_untouched(self, isolated_apps):
        # Yesterday — still pending, not yet ghosted
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        repository.save(_app(applied_at=recent))

        count = auto_mark_ghosted()
        assert count == 0

        apps = repository.load_all(Application)
        assert apps[0].outcome == "pending"

    def test_already_answered_untouched(self, isolated_apps):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        repository.save(_app(outcome="interview", applied_at=old_date))

        count = auto_mark_ghosted()
        assert count == 0

        apps = repository.load_all(Application)
        assert apps[0].outcome == "interview"  # not clobbered

    def test_skipped_not_ghosted(self, isolated_apps):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        repository.save(_app(status="skipped", applied_at=old_date))

        count = auto_mark_ghosted()
        assert count == 0

    def test_custom_threshold(self, isolated_apps):
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        repository.save(_app(applied_at=eight_days_ago))

        # 7-day threshold — should ghost
        count = auto_mark_ghosted(days=7)
        assert count == 1


# ------------------------------------------------------------------
# outcome_summary
# ------------------------------------------------------------------

class TestOutcomeSummary:
    def test_empty(self, isolated_apps):
        assert outcome_summary() == {}

    def test_counts(self, isolated_apps):
        repository.save(_app(job_id="j1", outcome="pending"))
        repository.save(_app(job_id="j2", outcome="interview"))
        repository.save(_app(job_id="j3", outcome="interview"))
        repository.save(_app(job_id="j4", outcome="rejected"))

        summary = outcome_summary()
        assert summary == {
            "pending": 1,
            "interview": 2,
            "rejected": 1,
        }

    def test_ignores_skipped_rows(self, isolated_apps):
        repository.save(_app(status="skipped"))

        summary = outcome_summary()
        assert summary == {}


# ------------------------------------------------------------------
# Constants sanity
# ------------------------------------------------------------------

class TestConstants:
    def test_valid_outcomes_has_all_states(self):
        expected = {
            "pending", "acknowledged", "interview",
            "rejected", "offer", "ghosted", "withdrawn",
        }
        assert VALID_OUTCOMES == expected

    def test_closed_subset(self):
        assert CLOSED_OUTCOMES.issubset(VALID_OUTCOMES)
        assert "pending" not in CLOSED_OUTCOMES

    def test_ghost_days_positive(self):
        assert GHOST_DAYS > 0
