"""Regression tests for dry-run vs real-application budget counting."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from auto_applier.storage import repository
from auto_applier.storage.models import Application, Followup, Job, SkillGap


@pytest.fixture
def temp_csvs(tmp_path):
    with patch.object(repository, "_CSV_MAP", {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


def _today_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestTodaysApplicationCount:
    def test_empty_store_returns_zero(self, temp_csvs):
        assert repository.get_todays_application_count() == 0

    def test_counts_real_applications_only_by_default(self, temp_csvs):
        """The real-world bug: dry runs shouldn't consume daily quota."""
        # Simulate three dry runs earlier today
        for i in range(3):
            a = Application(
                job_id=f"j{i}",
                status="dry_run",
                source="indeed",
                resume_used="testpilot",
                score=8,
            )
            a.applied_at = _today_iso()
            repository.save(a)

        # Default behavior: dry runs don't count toward the budget
        assert repository.get_todays_application_count() == 0

    def test_counts_applied_status(self, temp_csvs):
        for i in range(2):
            a = Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="testpilot",
                score=8,
            )
            a.applied_at = _today_iso()
            repository.save(a)
        assert repository.get_todays_application_count() == 2

    def test_mixed_statuses(self, temp_csvs):
        statuses = ["applied", "applied", "dry_run", "failed", "skipped"]
        for i, s in enumerate(statuses):
            a = Application(
                job_id=f"j{i}",
                status=s,
                source="indeed",
                resume_used="testpilot",
                score=8,
            )
            a.applied_at = _today_iso()
            repository.save(a)
        # Only the two 'applied' rows count by default
        assert repository.get_todays_application_count() == 2
        # With include_dry_run, applied + dry_run = 3
        assert repository.get_todays_application_count(include_dry_run=True) == 3

    def test_ignores_old_days(self, temp_csvs):
        a = Application(
            job_id="old1",
            status="applied",
            source="indeed",
            resume_used="testpilot",
            score=8,
        )
        a.applied_at = "2020-01-01T12:00:00+00:00"
        repository.save(a)
        assert repository.get_todays_application_count() == 0


class TestPerSourceCount:
    """Per-platform daily budget: max_applications_per_day applies
    to each platform independently."""

    def test_counts_only_matching_source(self, temp_csvs):
        for source in ["linkedin", "linkedin", "indeed", "dice"]:
            a = Application(
                job_id=f"j-{source}-{id(source)}",
                status="applied",
                source=source,
                resume_used="testpilot",
                score=8,
            )
            a.applied_at = _today_iso()
            repository.save(a)
        assert repository.get_todays_application_count(source="linkedin") == 2
        assert repository.get_todays_application_count(source="indeed") == 1
        assert repository.get_todays_application_count(source="dice") == 1
        assert repository.get_todays_application_count(source="ziprecruiter") == 0
        # No source argument → all platforms combined
        assert repository.get_todays_application_count() == 4

    def test_empty_source_is_all(self, temp_csvs):
        for source in ["linkedin", "indeed"]:
            a = Application(
                job_id=f"j-{source}",
                status="applied",
                source=source,
                resume_used="testpilot",
                score=8,
            )
            a.applied_at = _today_iso()
            repository.save(a)
        # Explicit empty string and omission both count globally
        assert repository.get_todays_application_count(source="") == 2
        assert repository.get_todays_application_count() == 2
