"""Tests for CLI observability helpers."""
import json
from unittest.mock import patch

import pytest

from auto_applier.analysis.observability import (
    export_all, get_job_detail, write_export,
)
from auto_applier.storage import repository
from auto_applier.storage.models import Application, Followup, Job, SkillGap


@pytest.fixture
def temp_csvs(tmp_path):
    with patch.object(repository, '_CSV_MAP', {
        Job: tmp_path / "jobs.csv",
        Application: tmp_path / "applications.csv",
        SkillGap: tmp_path / "skill_gaps.csv",
        Followup: tmp_path / "followups.csv",
    }):
        yield tmp_path


class TestGetJobDetail:
    def test_missing_returns_empty(self, temp_csvs):
        assert get_job_detail("nope") == {}

    def test_finds_all_related_rows(self, temp_csvs):
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="linkedin"))
        repository.save(Application(job_id="j1", status="applied", source="linkedin", score=8))
        repository.save(SkillGap(job_id="j1", field_label="years of python"))
        repository.save(Followup(job_id="j1", source="linkedin", due_date="2026-04-20"))

        detail = get_job_detail("j1")
        assert detail["job_id"] == "j1"
        assert len(detail["jobs"]) == 1
        assert len(detail["applications"]) == 1
        assert len(detail["skill_gaps"]) == 1
        assert len(detail["followups"]) == 1
        assert detail["applications"][0]["score"] == 8

    def test_multi_source_cross_post(self, temp_csvs):
        # Same job_id can exist under two sources if not canonically deduped
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="linkedin"))
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="indeed"))
        detail = get_job_detail("j1")
        assert len(detail["jobs"]) == 2


class TestExportAll:
    def test_empty_store(self, temp_csvs):
        ex = export_all()
        assert ex["jobs"] == []
        assert ex["applications"] == []
        assert ex["skill_gaps"] == []
        assert ex["followups"] == []
        assert "exported_at" in ex
        assert "schema_hints" in ex

    def test_includes_schema_hints(self, temp_csvs):
        ex = export_all()
        assert "canonical_hash" in ex["schema_hints"]["jobs"]
        assert "liveness" in ex["schema_hints"]["jobs"]
        assert "dimensions_json" in ex["schema_hints"]["applications"]

    def test_serializes_data(self, temp_csvs):
        repository.save(Job(
            job_id="j1", title="Analyst", company="Acme",
            url="https://x.com", source="linkedin",
        ))
        ex = export_all()
        assert len(ex["jobs"]) == 1
        assert ex["jobs"][0]["job_id"] == "j1"
        # Should be JSON-serializable end-to-end
        blob = json.dumps(ex, default=str)
        roundtripped = json.loads(blob)
        assert roundtripped["jobs"][0]["title"] == "Analyst"


class TestWriteExport:
    def test_writes_valid_json_file(self, temp_csvs, tmp_path):
        repository.save(Job(job_id="j1", title="A", company="C", url="u", source="li"))
        out = tmp_path / "export.json"
        path = write_export(out)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["jobs"][0]["job_id"] == "j1"

    def test_creates_parent_dirs(self, temp_csvs, tmp_path):
        out = tmp_path / "nested" / "deeper" / "export.json"
        write_export(out)
        assert out.exists()
