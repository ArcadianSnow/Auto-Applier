"""Tests for resume/evolution.py — EvolutionEngine trigger detection."""

import json
from pathlib import Path

import pytest

from auto_applier.resume.evolution import EvolutionEngine, EvolutionTrigger
from auto_applier.storage.models import SkillGap
from auto_applier.storage import repository


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """EvolutionEngine backed by temp CSV and prompted_skills files."""
    csv_path = tmp_path / "skill_gaps.csv"
    # Patch the _CSV_MAP entry so save/load_all use our temp file
    monkeypatch.setitem(repository._CSV_MAP, SkillGap, csv_path)
    monkeypatch.setattr("auto_applier.resume.evolution.DATA_DIR", tmp_path)
    return EvolutionEngine(trigger_threshold=3)


def _gap(field_label, job_id="j1", category="skill", resume_label="default"):
    return SkillGap(
        job_id=job_id,
        field_label=field_label,
        category=category,
        resume_label=resume_label,
        source="test",
    )


class TestCheckTriggers:
    def test_no_gaps_returns_empty(self, engine):
        assert engine.check_triggers() == []

    def test_below_threshold_no_trigger(self, engine):
        for i in range(2):
            repository.save(_gap("python", job_id=f"j{i}"))
        assert engine.check_triggers() == []

    def test_at_threshold_triggers(self, engine):
        for i in range(3):
            repository.save(_gap("kubernetes", job_id=f"j{i}"))
        triggers = engine.check_triggers()
        assert len(triggers) == 1
        assert triggers[0].skill_name == "kubernetes"
        assert triggers[0].times_seen == 3

    def test_above_threshold_triggers(self, engine):
        for i in range(5):
            repository.save(_gap("docker", job_id=f"j{i}"))
        triggers = engine.check_triggers()
        assert triggers[0].times_seen == 5

    def test_case_insensitive_counting(self, engine):
        repository.save(_gap("Python"))
        repository.save(_gap("python"))
        repository.save(_gap("PYTHON"))
        triggers = engine.check_triggers()
        assert len(triggers) == 1

    def test_sorted_by_frequency(self, engine):
        for i in range(5):
            repository.save(_gap("go", job_id=f"j{i}"))
        for i in range(3):
            repository.save(_gap("rust", job_id=f"j{i+10}"))
        triggers = engine.check_triggers()
        assert triggers[0].skill_name == "go"
        assert triggers[1].skill_name == "rust"

    def test_prompted_skills_excluded(self, engine):
        for i in range(4):
            repository.save(_gap("terraform", job_id=f"j{i}"))
        engine.mark_prompted("terraform")
        assert engine.check_triggers() == []


class TestMarkPrompted:
    def test_mark_and_persist(self, engine):
        engine.mark_prompted("python")
        engine.mark_prompted("Docker")
        prompted = engine._load_prompted()
        assert "python" in prompted
        assert "docker" in prompted  # lowercased

    def test_idempotent(self, engine):
        engine.mark_prompted("go")
        engine.mark_prompted("go")
        prompted = engine._load_prompted()
        assert len([x for x in prompted if x == "go"]) == 1


class TestGetGapSummary:
    def test_empty(self, engine):
        assert engine.get_gap_summary() == []

    def test_includes_prompted_skills(self, engine):
        for i in range(3):
            repository.save(_gap("python", job_id=f"j{i}"))
        engine.mark_prompted("python")
        summary = engine.get_gap_summary()
        assert len(summary) == 1
        assert summary[0][0] == "python"

    def test_sorted_by_count(self, engine):
        for i in range(5):
            repository.save(_gap("sql", job_id=f"j{i}"))
        for i in range(2):
            repository.save(_gap("java", job_id=f"j{i+10}"))
        summary = engine.get_gap_summary()
        assert summary[0][0] == "sql"
        assert summary[0][1] == 5
