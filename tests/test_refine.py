"""Tests for resume/refine.py — gap prioritization + bullet generation."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.resume.refine import (
    RefineCandidate,
    ResumeSuggestion,
    check_resume_suggestion,
    collect_refine_candidates,
    generate_bullets,
    save_confirmed_skill,
)
from auto_applier.storage.models import Application, Job, SkillGap
from auto_applier.storage import repository


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setitem(repository._CSV_MAP, SkillGap, tmp_path / "skill_gaps.csv")
    monkeypatch.setitem(repository._CSV_MAP, Job, tmp_path / "jobs.csv")
    monkeypatch.setitem(repository._CSV_MAP, Application, tmp_path / "applications.csv")
    monkeypatch.setattr(
        "auto_applier.analysis.learning_goals.DATA_DIR", tmp_path,
    )
    # Point EvolutionEngine's DATA_DIR too
    monkeypatch.setattr(
        "auto_applier.resume.evolution.DATA_DIR", tmp_path,
    )
    return tmp_path


def _save_gap(job_id, field_label, resume_label="data_analyst"):
    repository.save(SkillGap(
        job_id=job_id,
        field_label=field_label,
        category="skill",
        resume_label=resume_label,
        source="indeed",
    ))


def _save_job(job_id, title="Data Analyst", company="Acme"):
    repository.save(Job(
        job_id=job_id,
        title=title,
        company=company,
        url=f"https://example.com/{job_id}",
    ))


# ------------------------------------------------------------------
# collect_refine_candidates
# ------------------------------------------------------------------

class TestCollectRefineCandidates:
    def test_empty(self, isolated):
        assert collect_refine_candidates() == []

    def test_single_skill_meets_threshold(self, isolated):
        for i in range(3):
            _save_job(f"j{i}")
            _save_gap(f"j{i}", "Tableau")

        result = collect_refine_candidates(min_count=2)
        assert len(result) == 1
        assert result[0].skill == "tableau"
        assert result[0].count == 3

    def test_below_min_count_filtered(self, isolated):
        _save_job("j1")
        _save_gap("j1", "Python")
        # min_count=2 default — 1 occurrence should be excluded
        assert collect_refine_candidates(min_count=2) == []

    def test_caps_per_group(self, isolated):
        # 5 skills all in same resume/archetype — should cap at max_per_group
        for skill in ["A", "B", "C", "D", "E"]:
            for i in range(3):
                _save_job(f"j_{skill}_{i}")
                _save_gap(f"j_{skill}_{i}", skill)
        result = collect_refine_candidates(min_count=2, max_per_group=3)
        assert len(result) == 3  # capped to 3 per group

    def test_excludes_learning_goals(self, isolated):
        from auto_applier.analysis import learning_goals

        for i in range(3):
            _save_job(f"j{i}")
            _save_gap(f"j{i}", "Python")
        learning_goals.set_state("python", "learning")

        result = collect_refine_candidates()
        assert result == []

    def test_excludes_certified(self, isolated):
        from auto_applier.analysis import learning_goals

        for i in range(3):
            _save_job(f"j{i}")
            _save_gap(f"j{i}", "Python")
        learning_goals.set_state("python", "certified")

        result = collect_refine_candidates()
        assert result == []

    def test_counts_across_archetypes(self, isolated):
        """A skill missing in 1 analyst job + 1 engineer job should count
        as 2 occurrences (regression test — earlier grouping by archetype
        split the count and required min_count to be hit per-archetype)."""
        _save_job("j_analyst", title="Data Analyst")
        _save_job("j_engineer", title="Software Engineer")
        _save_gap("j_analyst", "dbt")
        _save_gap("j_engineer", "dbt")

        result = collect_refine_candidates(min_count=2)
        assert len(result) == 1
        assert result[0].skill == "dbt"
        assert result[0].count == 2
        # Primary archetype = first one alphabetically when tied
        assert result[0].archetype in ("analyst", "engineer")

    def test_includes_company_samples(self, isolated):
        companies = ["Airbnb", "Stripe", "Uber"]
        for i, co in enumerate(companies):
            _save_job(f"j{i}", title="Data Analyst", company=co)
            _save_gap(f"j{i}", "Tableau")

        result = collect_refine_candidates(min_count=2)
        assert len(result) == 1
        # Should include the company names (order may vary)
        assert set(result[0].sample_companies) == set(companies)


# ------------------------------------------------------------------
# generate_bullets
# ------------------------------------------------------------------

class TestGenerateBullets:
    def test_empty_description_returns_empty(self, isolated):
        router = MagicMock()
        result = asyncio.run(generate_bullets(
            skill="Python",
            user_description="",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert result == []

    def test_plain_list_response(self, isolated):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value=[
            "Built data pipeline",
            "Optimized SQL queries",
        ])

        result = asyncio.run(generate_bullets(
            skill="Python",
            user_description="I built an ETL pipeline",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert len(result) == 2
        assert "Built data pipeline" in result

    def test_object_wrapped_response(self, isolated):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "bullets": ["Built X", "Optimized Y"],
        })

        result = asyncio.run(generate_bullets(
            skill="SQL",
            user_description="wrote queries",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert len(result) == 2

    def test_caps_at_three(self, isolated):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value=[
            f"bullet {i}" for i in range(10)
        ])
        result = asyncio.run(generate_bullets(
            skill="X",
            user_description="did stuff",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert len(result) == 3

    def test_llm_failure_returns_empty(self, isolated):
        router = MagicMock()
        router.complete_json = AsyncMock(
            side_effect=RuntimeError("LLM down"),
        )
        result = asyncio.run(generate_bullets(
            skill="X",
            user_description="did stuff",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert result == []

    def test_malformed_response_returns_empty(self, isolated):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value="not a list")
        result = asyncio.run(generate_bullets(
            skill="X",
            user_description="did stuff",
            resume_label="test",
            resume_text="",
            router=router,
        ))
        assert result == []


# ------------------------------------------------------------------
# save_confirmed_skill
# ------------------------------------------------------------------

class TestSaveConfirmedSkill:
    def test_adds_new_skill(self, isolated):
        mgr = MagicMock()
        mgr.get_profile.return_value = {
            "label": "test",
            "confirmed_skills": [],
        }
        mgr.save_profile = MagicMock()

        ok = save_confirmed_skill(
            resume_label="test",
            skill="Python",
            level="advanced",
            bullets=["Built X", "Optimized Y"],
            resume_manager=mgr,
        )
        assert ok is True
        mgr.save_profile.assert_called_once()
        args = mgr.save_profile.call_args
        profile = args[0][1]
        assert profile["confirmed_skills"][0]["name"] == "Python"
        assert profile["confirmed_skills"][0]["level"] == "advanced"
        assert len(profile["confirmed_skills"][0]["bullets"]) == 2

    def test_updates_existing_skill(self, isolated):
        mgr = MagicMock()
        mgr.get_profile.return_value = {
            "label": "test",
            "confirmed_skills": [
                {"name": "Python", "level": "beginner", "bullets": ["old"]},
            ],
        }
        mgr.save_profile = MagicMock()

        ok = save_confirmed_skill(
            resume_label="test",
            skill="Python",
            level="advanced",
            bullets=["new bullet"],
            resume_manager=mgr,
        )
        assert ok is True
        profile = mgr.save_profile.call_args[0][1]
        confirmed = profile["confirmed_skills"]
        assert len(confirmed) == 1  # no duplicate added
        assert confirmed[0]["bullets"] == ["new bullet"]
        assert confirmed[0]["level"] == "advanced"

    def test_missing_resume_returns_false(self, isolated):
        mgr = MagicMock()
        mgr.get_profile.return_value = {}
        result = save_confirmed_skill(
            resume_label="nonexistent",
            skill="X",
            level="expert",
            bullets=["a"],
            resume_manager=mgr,
        )
        assert result is False


# ------------------------------------------------------------------
# check_resume_suggestion
# ------------------------------------------------------------------

class TestCheckResumeSuggestion:
    def test_no_mismatch_no_suggestion(self, isolated):
        # Analyst resume used on analyst jobs with good scores — no suggestion
        for i in range(5):
            _save_job(f"j{i}", title="Data Analyst")
            repository.save(Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="data_analyst",
                score=8,
            ))
        assert check_resume_suggestion() == []

    def test_mismatch_triggers_suggestion(self, isolated):
        # Analyst resume used on engineer jobs with low scores
        for i in range(5):
            _save_job(f"j{i}", title="Software Engineer")
            repository.save(Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="data_analyst",
                score=5,
            ))
        result = check_resume_suggestion()
        assert len(result) == 1
        assert result[0].existing_resume == "data_analyst"
        assert result[0].target_archetype == "engineer"
        assert result[0].avg_score == 5.0

    def test_skips_when_resume_already_matches_archetype(self, isolated):
        # data_engineer resume used on engineer jobs — don't suggest engineer resume
        for i in range(5):
            _save_job(f"j{i}", title="Data Engineer")
            repository.save(Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="data_engineer",
                score=5,
            ))
        assert check_resume_suggestion() == []

    def test_below_min_evidence_count(self, isolated):
        # Only 2 applications — below default threshold of 4
        for i in range(2):
            _save_job(f"j{i}", title="Software Engineer")
            repository.save(Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="data_analyst",
                score=4,
            ))
        assert check_resume_suggestion() == []

    def test_good_score_no_suggestion(self, isolated):
        # Cross-archetype use but doing fine — no need to suggest
        for i in range(5):
            _save_job(f"j{i}", title="Software Engineer")
            repository.save(Application(
                job_id=f"j{i}",
                status="applied",
                source="indeed",
                resume_used="data_analyst",
                score=8,
            ))
        assert check_resume_suggestion() == []

    def test_skipped_rows_ignored(self, isolated):
        for i in range(5):
            _save_job(f"j{i}", title="Software Engineer")
            repository.save(Application(
                job_id=f"j{i}",
                status="skipped",
                source="indeed",
                resume_used="data_analyst",
                score=5,
            ))
        assert check_resume_suggestion() == []
