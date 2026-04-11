"""Tests for follow-up scheduling, listing, and status updates."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from auto_applier.storage.models import Job, Application, SkillGap, Followup
from auto_applier.storage import repository


@pytest.fixture
def temp_csvs(tmp_path):
    """Redirect CSV paths into tmp_path for isolated tests."""
    with patch.object(repository, '_CSV_MAP', {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


class TestScheduleFollowups:
    def test_creates_default_cadence(self, temp_csvs):
        applied = "2026-04-01T12:00:00+00:00"
        created = repository.schedule_followups("job-1", "linkedin", applied)
        assert len(created) == 3
        dues = sorted(f.due_date for f in created)
        assert dues == ["2026-04-08", "2026-04-15", "2026-04-22"]

    def test_respects_custom_cadence(self, temp_csvs):
        applied = "2026-04-01T12:00:00+00:00"
        created = repository.schedule_followups(
            "job-1", "linkedin", applied, cadence_days=[3, 10],
        )
        assert len(created) == 2
        assert created[0].due_date == "2026-04-04"
        assert created[1].due_date == "2026-04-11"

    def test_bad_applied_at_falls_back_to_today(self, temp_csvs):
        created = repository.schedule_followups(
            "job-1", "linkedin", "not-a-date", cadence_days=[0],
        )
        assert len(created) == 1
        assert created[0].due_date == date.today().isoformat()

    def test_persists_to_csv(self, temp_csvs):
        repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
        )
        loaded = repository.load_all(Followup)
        assert len(loaded) == 3
        assert all(f.job_id == "job-1" for f in loaded)
        assert all(f.status == "pending" for f in loaded)

    def test_default_channel_and_status(self, temp_csvs):
        created = repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
            cadence_days=[7],
        )
        f = created[0]
        assert f.status == "pending"
        assert f.channel == "email"


class TestListFollowups:
    def test_empty(self, temp_csvs):
        assert repository.list_followups() == []

    def test_filters_by_status(self, temp_csvs):
        repository.save(Followup(
            job_id="j1", source="linkedin", due_date="2026-04-18", status="pending",
        ))
        repository.save(Followup(
            job_id="j2", source="indeed", due_date="2026-04-18", status="done",
        ))
        assert len(repository.list_followups()) == 2
        assert len(repository.list_followups(status="pending")) == 1
        assert len(repository.list_followups(status="done")) == 1
        assert len(repository.list_followups(status="dismissed")) == 0


class TestGetDueFollowups:
    def test_returns_overdue(self, temp_csvs):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        repository.save(Followup(job_id="j1", source="li", due_date=yesterday))
        repository.save(Followup(job_id="j2", source="li", due_date=tomorrow))

        due = repository.get_due_followups()
        assert len(due) == 1
        assert due[0].job_id == "j1"

    def test_includes_today(self, temp_csvs):
        today = date.today().isoformat()
        repository.save(Followup(job_id="j1", source="li", due_date=today))
        due = repository.get_due_followups()
        assert len(due) == 1

    def test_ignores_non_pending(self, temp_csvs):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        repository.save(Followup(
            job_id="j1", source="li", due_date=yesterday, status="done",
        ))
        repository.save(Followup(
            job_id="j2", source="li", due_date=yesterday, status="dismissed",
        ))
        assert repository.get_due_followups() == []

    def test_respects_as_of(self, temp_csvs):
        repository.save(Followup(
            job_id="j1", source="li", due_date="2026-04-18",
        ))
        assert len(repository.get_due_followups(as_of="2026-04-18")) == 1
        assert len(repository.get_due_followups(as_of="2026-04-17")) == 0


class TestUpdateFollowupsForJob:
    def test_marks_done(self, temp_csvs):
        repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
        )
        n = repository.update_followups_for_job("job-1", "done")
        assert n == 3
        assert all(f.status == "done" for f in repository.load_all(Followup))

    def test_only_updates_matching_job(self, temp_csvs):
        repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
        )
        repository.schedule_followups(
            "job-2", "indeed", "2026-04-01T12:00:00+00:00",
        )
        n = repository.update_followups_for_job("job-1", "dismissed")
        assert n == 3
        by_status = {}
        for f in repository.load_all(Followup):
            by_status.setdefault(f.status, []).append(f.job_id)
        assert "job-1" not in by_status.get("pending", [])
        assert "job-2" in by_status.get("pending", [])
        assert "job-1" in by_status.get("dismissed", [])

    def test_source_filter(self, temp_csvs):
        repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
            cadence_days=[7],
        )
        repository.schedule_followups(
            "job-1", "indeed", "2026-04-01T12:00:00+00:00",
            cadence_days=[7],
        )
        n = repository.update_followups_for_job(
            "job-1", "done", source="linkedin",
        )
        assert n == 1
        # The Indeed one is still pending
        indeed_f = [f for f in repository.load_all(Followup) if f.source == "indeed"]
        assert all(f.status == "pending" for f in indeed_f)

    def test_idempotent_when_already_done(self, temp_csvs):
        repository.schedule_followups(
            "job-1", "linkedin", "2026-04-01T12:00:00+00:00",
        )
        repository.update_followups_for_job("job-1", "done")
        # Second call updates 0 — status is no longer pending
        n = repository.update_followups_for_job("job-1", "done")
        assert n == 0
