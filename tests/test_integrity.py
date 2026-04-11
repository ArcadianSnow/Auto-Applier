"""Tests for the fsck and normalize integrity commands."""
from unittest.mock import patch

import pytest

from auto_applier.storage import repository, integrity
from auto_applier.storage.models import Application, Followup, Job, SkillGap


@pytest.fixture
def temp_csvs(tmp_path, monkeypatch):
    """Redirect CSV paths + backup dir into tmp_path."""
    backup = tmp_path / ".backups"
    backup.mkdir()
    monkeypatch.setattr(
        "auto_applier.storage.migrations.BACKUP_DIR", backup,
    )
    with patch.object(repository, '_CSV_MAP', {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


class TestFsck:
    def test_empty_store_is_healthy(self, temp_csvs):
        report = integrity.fsck()
        assert report["healthy"] is True
        assert report["jobs"] == 0

    def test_counts_rows(self, temp_csvs):
        repository.save(Job(job_id="j1", title="Analyst", company="Acme", url="u", source="linkedin"))
        repository.save(Application(job_id="j1", status="applied", source="linkedin"))
        report = integrity.fsck()
        assert report["jobs"] == 1
        assert report["applications"] == 1

    def test_detects_orphan_application(self, temp_csvs):
        repository.save(Application(job_id="nonexistent", status="applied", source="linkedin"))
        report = integrity.fsck()
        assert any("orphan application" in i for i in report["issues"])

    def test_detects_orphan_skill_gap(self, temp_csvs):
        repository.save(SkillGap(job_id="nonexistent", field_label="years"))
        report = integrity.fsck()
        assert any("orphan skill_gap" in i for i in report["issues"])

    def test_detects_bad_application_status(self, temp_csvs):
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        repository.save(Application(job_id="j1", status="SUCCESS", source="li"))
        report = integrity.fsck()
        # "success" (after lower) maps to applied alias
        assert any("alias" in i.lower() for i in report["issues"])

    def test_detects_duplicate_canonical_hash(self, temp_csvs):
        # Two different job_ids but same canonical hash — cross-post leak
        a = Job(job_id="li-1", title="Senior Data Analyst", company="Acme Inc.", url="u", source="linkedin")
        b = Job(job_id="ind-99", title="Senior Data Analyst", company="ACME Corp", url="u", source="indeed")
        repository.save(a)
        repository.save(b)
        report = integrity.fsck()
        # Both are different (id,source) so this is a cross-post leak, not dupe row
        assert any("duplicate canonical_hash" in i for i in report["issues"])

    def test_detects_duplicate_job_row(self, temp_csvs):
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        report = integrity.fsck()
        assert any("duplicate jobs.csv row" in i for i in report["issues"])

    def test_detects_orphan_followup(self, temp_csvs):
        repository.save(Followup(job_id="nope", source="li", due_date="2026-04-20"))
        report = integrity.fsck()
        assert any("orphan followup" in i for i in report["issues"])


class TestNormalize:
    def test_noop_on_clean_store(self, temp_csvs):
        changes = integrity.normalize()
        assert changes["total"] == 0

    def test_dedupes_job_rows(self, temp_csvs):
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        changes = integrity.normalize()
        assert changes["jobs_deduped"] == 1
        jobs = repository.load_all(Job)
        assert len(jobs) == 1

    def test_fixes_application_status_aliases(self, temp_csvs):
        repository.save(Application(job_id="j1", status="success", source="li"))
        repository.save(Application(job_id="j2", status="submitted", source="li"))
        changes = integrity.normalize()
        assert changes["application_statuses_fixed"] == 2
        apps = repository.load_all(Application)
        assert all(a.status == "applied" for a in apps)

    def test_fixes_followup_status_aliases(self, temp_csvs):
        repository.save(Followup(job_id="j1", source="li", due_date="2026-04-20", status="todo"))
        repository.save(Followup(job_id="j2", source="li", due_date="2026-04-20", status="cancelled"))
        changes = integrity.normalize()
        assert changes["followup_statuses_fixed"] == 2
        fu = repository.load_all(Followup)
        assert {f.status for f in fu} == {"pending", "dismissed"}

    def test_normalizes_company_casing(self, temp_csvs):
        repository.save(Job(
            job_id="j1", title="A",
            company="ACME INC.",  # loud-case variant of "Acme"
            url="u", source="li",
        ))
        changes = integrity.normalize()
        # Should have renormalized to "Acme" (Title case after strip)
        assert changes["companies_renormalized"] >= 1
        jobs = repository.load_all(Job)
        assert jobs[0].company == "Acme"

    def test_backup_written_before_rewrite(self, temp_csvs):
        repository.save(Application(job_id="j1", status="success", source="li"))
        integrity.normalize()
        from auto_applier.storage.migrations import BACKUP_DIR
        backups = list(BACKUP_DIR.glob("applications.*.csv"))
        assert len(backups) >= 1

    def test_no_alias_leaves_unknown_status_alone(self, temp_csvs):
        # A status that isn't in canonical OR aliases should survive
        repository.save(Application(job_id="j1", status="mystery_status", source="li"))
        integrity.normalize()
        apps = repository.load_all(Application)
        assert apps[0].status == "mystery_status"
