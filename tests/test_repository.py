"""Tests for CSV repository."""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from auto_applier.storage.models import Job, Application, SkillGap
from auto_applier.storage import repository


@pytest.fixture
def temp_csvs(tmp_path):
    """Redirect CSV paths to temp directory."""
    with patch.object(repository, '_CSV_MAP', {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
    }):
        yield tmp_path


class TestRepository:
    def test_save_and_load_job(self, temp_csvs):
        job = Job(job_id="test1", title="Analyst", company="Acme", url="https://example.com")
        repository.save(job)

        jobs = repository.load_all(Job)
        assert len(jobs) == 1
        assert jobs[0].title == "Analyst"

    def test_save_and_load_application(self, temp_csvs):
        app = Application(job_id="test1", status="applied", source="linkedin", resume_used="analyst", score=8)
        repository.save(app)

        apps = repository.load_all(Application)
        assert len(apps) == 1
        assert apps[0].score == 8
        assert apps[0].resume_used == "analyst"

    def test_boolean_round_trip(self, temp_csvs):
        app = Application(job_id="test1", cover_letter_generated=True, used_llm=True)
        repository.save(app)

        apps = repository.load_all(Application)
        assert apps[0].cover_letter_generated == True
        assert apps[0].used_llm == True

    def test_job_already_applied(self, temp_csvs):
        app = Application(job_id="test1", status="applied", source="linkedin")
        repository.save(app)

        assert repository.job_already_applied("test1", "linkedin") == True
        assert repository.job_already_applied("test1", "indeed") == False
        assert repository.job_already_applied("test2", "linkedin") == False

    def test_multiple_records(self, temp_csvs):
        for i in range(5):
            repository.save(Job(job_id=f"j{i}", title=f"Job {i}", company="Co", url="https://x.com"))

        jobs = repository.load_all(Job)
        assert len(jobs) == 5
