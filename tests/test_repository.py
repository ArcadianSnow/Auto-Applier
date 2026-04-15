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

    def test_job_already_processed_includes_skipped(self, temp_csvs):
        """Phase A: any Application row dedupes, not just 'applied'.
        Continuous mode would otherwise re-score skipped jobs every cycle."""
        repository.save(Application(job_id="s1", status="skipped", source="indeed"))
        assert repository.job_already_processed("s1", "indeed") == True

    def test_processed_pairs_batch_helper(self, temp_csvs):
        """Batch-level dedup source used by pipeline.discover_jobs."""
        repository.save(Application(job_id="a", status="applied", source="indeed"))
        repository.save(Application(job_id="b", status="skipped", source="dice"))
        pairs = repository.processed_pairs()
        assert ("a", "indeed") in pairs
        assert ("b", "dice") in pairs
        assert ("c", "linkedin") not in pairs

    def test_processed_canonical_hashes_joins_jobs(self, temp_csvs):
        """Must join applications → jobs so unscored Jobs don't dedupe."""
        scored = Job(
            job_id="j1", title="Senior Data Analyst",
            company="Acme", url="u", source="linkedin",
        )
        unscored = Job(
            job_id="j2", title="Staff Engineer",
            company="Other", url="u2", source="indeed",
        )
        repository.save(scored)
        repository.save(unscored)
        repository.save(Application(job_id="j1", status="skipped", source="linkedin"))

        hashes = repository.processed_canonical_hashes()
        assert scored.canonical_hash in hashes
        assert unscored.canonical_hash not in hashes

    def test_multiple_records(self, temp_csvs):
        for i in range(5):
            repository.save(Job(job_id=f"j{i}", title=f"Job {i}", company="Co", url="https://x.com"))

        jobs = repository.load_all(Job)
        assert len(jobs) == 5
