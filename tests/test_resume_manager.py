"""Tests for resume/manager.py — ResumeManager listing, profiles, scoring."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from auto_applier.resume.manager import ResumeManager, ResumeInfo, ResumeScore
from auto_applier.scoring.models import DimensionScore


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """Set up temporary resume and profile directories."""
    resumes_dir = tmp_path / "resumes"
    profiles_dir = tmp_path / "profiles"
    resumes_dir.mkdir()
    profiles_dir.mkdir()
    monkeypatch.setattr("auto_applier.resume.manager.RESUMES_DIR", resumes_dir)
    monkeypatch.setattr("auto_applier.resume.manager.PROFILES_DIR", profiles_dir)
    return resumes_dir, profiles_dir


@pytest.fixture
def router():
    """Fake LLM router."""
    r = MagicMock()
    r.complete_json = AsyncMock(return_value={
        "technical_skills": [{"name": "Python", "level": "advanced", "years": 5}],
        "soft_skills": ["leadership"],
        "certifications": [],
        "tools": ["Docker"],
    })
    return r


def _create_profile(profiles_dir, label, raw_text="Test resume text", skills=None):
    """Helper to create a profile JSON file."""
    profile = {
        "label": label,
        "source_file": f"{label}.pdf",
        "raw_text": raw_text,
        "skills": skills or [],
        "tools": [],
        "certifications": [],
        "soft_skills": [],
        "confirmed_skills": [],
    }
    path = profiles_dir / f"{label}.json"
    path.write_text(json.dumps(profile, indent=2))
    return path


class TestListResumes:
    def test_empty_profiles(self, data_dirs, router):
        _, profiles_dir = data_dirs
        mgr = ResumeManager(router)
        assert mgr.list_resumes() == []

    def test_lists_profiles(self, data_dirs, router):
        resumes_dir, profiles_dir = data_dirs
        _create_profile(profiles_dir, "analyst")
        _create_profile(profiles_dir, "engineer")
        mgr = ResumeManager(router)
        resumes = mgr.list_resumes()
        assert len(resumes) == 2
        labels = [r.label for r in resumes]
        assert "analyst" in labels
        assert "engineer" in labels

    def test_sorted_alphabetically(self, data_dirs, router):
        _, profiles_dir = data_dirs
        _create_profile(profiles_dir, "zebra")
        _create_profile(profiles_dir, "alpha")
        mgr = ResumeManager(router)
        resumes = mgr.list_resumes()
        assert resumes[0].label == "alpha"
        assert resumes[1].label == "zebra"

    def test_skips_corrupt_profiles(self, data_dirs, router):
        _, profiles_dir = data_dirs
        _create_profile(profiles_dir, "good")
        (profiles_dir / "bad.json").write_text("not json {{{")
        mgr = ResumeManager(router)
        resumes = mgr.list_resumes()
        assert len(resumes) == 1
        assert resumes[0].label == "good"


class TestGetResume:
    def test_found(self, data_dirs, router):
        _, profiles_dir = data_dirs
        _create_profile(profiles_dir, "analyst")
        mgr = ResumeManager(router)
        info = mgr.get_resume("analyst")
        assert info is not None
        assert info.label == "analyst"

    def test_not_found(self, data_dirs, router):
        mgr = ResumeManager(router)
        assert mgr.get_resume("nonexistent") is None


class TestGetProfile:
    def test_returns_dict(self, data_dirs, router):
        _, profiles_dir = data_dirs
        _create_profile(profiles_dir, "test", raw_text="hello world")
        mgr = ResumeManager(router)
        profile = mgr.get_profile("test")
        assert profile["label"] == "test"
        assert profile["raw_text"] == "hello world"

    def test_missing_returns_empty_dict(self, data_dirs, router):
        mgr = ResumeManager(router)
        assert mgr.get_profile("nope") == {}


class TestGetResumeText:
    def test_returns_raw_text(self, data_dirs, router):
        _, profiles_dir = data_dirs
        _create_profile(profiles_dir, "test", raw_text="My resume content")
        mgr = ResumeManager(router)
        text = mgr.get_resume_text("test")
        assert "My resume content" in text

    def test_appends_confirmed_skills(self, data_dirs, router):
        _, profiles_dir = data_dirs
        profile = {
            "label": "test",
            "source_file": "test.pdf",
            "raw_text": "Base text",
            "skills": [],
            "tools": [],
            "certifications": [],
            "soft_skills": [],
            "confirmed_skills": [
                {"name": "Kubernetes", "level": "intermediate", "bullets": ["Deployed clusters"]}
            ],
        }
        (profiles_dir / "test.json").write_text(json.dumps(profile))
        mgr = ResumeManager(router)
        text = mgr.get_resume_text("test")
        assert "Kubernetes" in text
        assert "Deployed clusters" in text

    def test_empty_for_missing_profile(self, data_dirs, router):
        mgr = ResumeManager(router)
        assert mgr.get_resume_text("nope") == ""


class TestSaveProfile:
    def test_saves_to_disk(self, data_dirs, router):
        _, profiles_dir = data_dirs
        mgr = ResumeManager(router)
        mgr.save_profile("new", {"label": "new", "skills": ["Go"]})
        loaded = json.loads((profiles_dir / "new.json").read_text())
        assert loaded["skills"] == ["Go"]


class TestParseDimensions:
    def test_valid_dimensions(self):
        result = {
            "skills": {"score": 8.0, "reason": "Good"},
            "experience": {"score": 7.0, "reason": "OK"},
            "seniority": {"score": 6.0, "reason": "Match"},
            "location": {"score": 9.0, "reason": "Remote"},
            "compensation": {"score": 5.0, "reason": "Not specified"},
            "culture": {"score": 7.0, "reason": "Aligned"},
            "growth": {"score": 6.0, "reason": "Some"},
        }
        dims = ResumeManager._parse_dimensions(result)
        assert len(dims) == 7
        names = [d.name for d in dims]
        assert "skills" in names
        assert "growth" in names

    def test_partial_below_threshold(self):
        """Fewer than half recognized dimensions returns empty."""
        result = {
            "skills": {"score": 8.0, "reason": "Good"},
            "experience": {"score": 7.0, "reason": "OK"},
        }
        assert ResumeManager._parse_dimensions(result) == []

    def test_clamps_scores(self):
        result = {
            "skills": {"score": 15.0, "reason": "x"},
            "experience": {"score": -3.0, "reason": "x"},
            "seniority": {"score": 5.0, "reason": "x"},
            "location": {"score": 5.0, "reason": "x"},
            "compensation": {"score": 5.0, "reason": "x"},
            "culture": {"score": 5.0, "reason": "x"},
            "growth": {"score": 5.0, "reason": "x"},
        }
        dims = ResumeManager._parse_dimensions(result)
        skills_dim = next(d for d in dims if d.name == "skills")
        exp_dim = next(d for d in dims if d.name == "experience")
        assert skills_dim.score == 10.0
        assert exp_dim.score == 0.0

    def test_non_dict_cells_skipped(self):
        result = {
            "skills": "just a string",  # Invalid
            "experience": {"score": 7.0, "reason": "OK"},
            "seniority": {"score": 5.0, "reason": "x"},
            "location": {"score": 5.0, "reason": "x"},
            "compensation": {"score": 5.0, "reason": "x"},
            "culture": {"score": 5.0, "reason": "x"},
            "growth": {"score": 5.0, "reason": "x"},
        }
        dims = ResumeManager._parse_dimensions(result)
        assert len(dims) == 6  # skills skipped

    def test_empty_result(self):
        assert ResumeManager._parse_dimensions({}) == []


class TestRemoveResume:
    def test_remove_existing(self, data_dirs, router):
        resumes_dir, profiles_dir = data_dirs
        _create_profile(profiles_dir, "victim")
        # Create the fake resume file too
        (resumes_dir / "victim.pdf").write_text("fake pdf")
        mgr = ResumeManager(router)
        mgr.remove_resume("victim")
        assert not (profiles_dir / "victim.json").exists()
        assert not (resumes_dir / "victim.pdf").exists()

    def test_remove_nonexistent_is_noop(self, data_dirs, router):
        mgr = ResumeManager(router)
        mgr.remove_resume("ghost")  # should not raise
