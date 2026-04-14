"""Tests for analysis/gap_tracker.py — gaps_with_context() enrichment."""

import pytest

from auto_applier.analysis.gap_tracker import gaps_with_context, GapContext
from auto_applier.storage.models import Job, SkillGap
from auto_applier.storage import repository


@pytest.fixture
def temp_data(tmp_path, monkeypatch):
    """Isolate repository CSVs to tmp_path for each test."""
    monkeypatch.setitem(repository._CSV_MAP, SkillGap, tmp_path / "skill_gaps.csv")
    monkeypatch.setitem(repository._CSV_MAP, Job, tmp_path / "jobs.csv")
    return tmp_path


def _make_gap(job_id, field_label="SQL", resume="data_analyst"):
    return SkillGap(
        job_id=job_id,
        field_label=field_label,
        category="skill",
        resume_label=resume,
        source="indeed",
    )


def _make_job(job_id, title, company="Acme"):
    return Job(
        job_id=job_id,
        title=title,
        company=company,
        url=f"https://example.com/{job_id}",
    )


class TestGapsWithContext:
    def test_empty_returns_empty(self, temp_data):
        assert gaps_with_context() == []

    def test_enriches_with_job_title(self, temp_data):
        repository.save(_make_job("j1", "Data Analyst"))
        repository.save(_make_gap("j1", "SQL"))

        result = gaps_with_context()
        assert len(result) == 1
        ctx = result[0]
        assert ctx.gap.field_label == "SQL"
        assert ctx.job_title == "Data Analyst"
        assert ctx.company == "Acme"
        assert ctx.archetype == "analyst"

    def test_missing_job_falls_back_to_other(self, temp_data):
        """Gap with no matching Job row still enriches, just without context."""
        repository.save(_make_gap("orphan", "Python"))

        result = gaps_with_context()
        assert len(result) == 1
        assert result[0].job_title == ""
        assert result[0].archetype == "other"

    def test_multiple_gaps_per_job(self, temp_data):
        repository.save(_make_job("j1", "Senior Data Analyst"))
        repository.save(_make_gap("j1", "SQL"))
        repository.save(_make_gap("j1", "Tableau"))
        repository.save(_make_gap("j1", "dbt"))

        result = gaps_with_context()
        assert len(result) == 3
        for ctx in result:
            assert ctx.archetype == "analyst"
            assert ctx.job_title == "Senior Data Analyst"

    def test_archetypes_respect_user_config(self, temp_data):
        repository.save(_make_job("j1", "Marketing Analyst"))
        repository.save(_make_gap("j1", "SEO"))

        # With regex fallback: Marketing Analyst → analyst (first pattern match)
        result = gaps_with_context()
        assert result[0].archetype == "analyst"

        # With user archetype that keys on 'marketing'
        user_archetypes = [{"name": "marketing_ops", "keywords": ["marketing"]}]
        result2 = gaps_with_context(user_archetypes=user_archetypes)
        assert result2[0].archetype == "marketing_ops"

    def test_mix_of_jobs_and_orphans(self, temp_data):
        repository.save(_make_job("j1", "Data Engineer"))
        repository.save(_make_gap("j1", "Spark"))
        repository.save(_make_gap("orphan", "Python"))

        result = gaps_with_context()
        assert len(result) == 2
        # j1 gap enriched
        enriched = [r for r in result if r.gap.job_id == "j1"][0]
        orphan = [r for r in result if r.gap.job_id == "orphan"][0]
        assert enriched.archetype == "engineer"
        assert orphan.archetype == "other"
