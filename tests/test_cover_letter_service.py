"""Tests for resume/cover_letter_service.py — on-demand letter generation."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.llm.base import LLMResponse
from auto_applier.resume.cover_letter_service import (
    CoverLetterResult,
    _pick_resume_for_job,
    _slugify,
    generate_cover_letter,
)
from auto_applier.storage.models import Application, Job
from auto_applier.storage import repository


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    """Isolate CSVs + cover letters dir for each test."""
    monkeypatch.setitem(repository._CSV_MAP, Job, tmp_path / "jobs.csv")
    monkeypatch.setitem(repository._CSV_MAP, Application, tmp_path / "applications.csv")
    monkeypatch.setattr(
        "auto_applier.resume.cover_letter_service.COVER_LETTERS_DIR",
        tmp_path / "cover_letters",
    )
    return tmp_path


def _job(job_id="j1", title="Data Analyst", company="Acme"):
    return Job(
        job_id=job_id,
        title=title,
        company=company,
        url=f"https://example.com/{job_id}",
        description="We need someone who knows SQL and Python.",
    )


def _mock_resume_manager(resumes_data: dict[str, str]):
    """resumes_data: {label: resume_text}"""
    mgr = MagicMock()

    def _list_resumes():
        result = []
        for label in sorted(resumes_data.keys()):
            info = MagicMock()
            info.label = label
            result.append(info)
        return result

    def _get_resume(label):
        if label in resumes_data:
            info = MagicMock()
            info.label = label
            return info
        return None

    def _get_resume_text(label):
        return resumes_data.get(label, "")

    mgr.list_resumes = _list_resumes
    mgr.get_resume = _get_resume
    mgr.get_resume_text = _get_resume_text
    return mgr


# ------------------------------------------------------------------
# _slugify
# ------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert _slugify("Acme Corp") == "acme-corp"

    def test_special_chars(self):
        assert _slugify("Data Analyst (Remote)") == "data-analyst-remote"

    def test_multiple_spaces(self):
        assert _slugify("a    b    c") == "a-b-c"

    def test_leading_trailing_hyphens(self):
        assert _slugify("---foo---") == "foo"

    def test_empty_returns_job(self):
        assert _slugify("") == "job"
        assert _slugify("---") == "job"

    def test_long_truncated(self):
        long = "Senior Data Analyst with Full Stack Engineering Experience"
        result = _slugify(long, max_len=20)
        assert len(result) <= 20


# ------------------------------------------------------------------
# _pick_resume_for_job
# ------------------------------------------------------------------

class TestPickResumeForJob:
    def test_preferred_wins(self, isolated_storage):
        mgr = _mock_resume_manager({"pref": "preferred text", "other": "other text"})
        job = _job()
        label, text = _pick_resume_for_job(job, mgr, preferred_label="pref")
        assert label == "pref"
        assert text == "preferred text"

    def test_preferred_missing_falls_through(self, isolated_storage):
        mgr = _mock_resume_manager({"only_resume": "text"})
        job = _job()
        label, text = _pick_resume_for_job(
            job, mgr, preferred_label="nonexistent",
        )
        # Falls through to single-resume case
        assert label == "only_resume"
        assert text == "text"

    def test_from_application_record(self, isolated_storage):
        repository.save(Application(
            job_id="j1",
            status="applied",
            source="indeed",
            resume_used="app_resume",
            score=8,
        ))
        mgr = _mock_resume_manager({"app_resume": "app text", "other": "other text"})
        job = _job("j1")
        label, text = _pick_resume_for_job(job, mgr)
        assert label == "app_resume"
        assert text == "app text"

    def test_single_resume_fallback(self, isolated_storage):
        mgr = _mock_resume_manager({"solo": "solo text"})
        job = _job()
        label, text = _pick_resume_for_job(job, mgr)
        assert label == "solo"
        assert text == "solo text"

    def test_no_resume_returns_empty(self, isolated_storage):
        mgr = _mock_resume_manager({})
        job = _job()
        label, text = _pick_resume_for_job(job, mgr)
        assert label == ""
        assert text == ""


# ------------------------------------------------------------------
# generate_cover_letter
# ------------------------------------------------------------------

class TestGenerateCoverLetter:
    def test_missing_job_returns_none(self, isolated_storage):
        mgr = _mock_resume_manager({"solo": "text"})
        router = MagicMock()
        result = asyncio.run(generate_cover_letter(
            job_id="nonexistent",
            router=router,
            resume_manager=mgr,
        ))
        assert result is None

    def test_no_resume_returns_none(self, isolated_storage):
        repository.save(_job())
        mgr = _mock_resume_manager({})
        router = MagicMock()
        result = asyncio.run(generate_cover_letter(
            job_id="j1",
            router=router,
            resume_manager=mgr,
        ))
        assert result is None

    def test_successful_generation(self, isolated_storage):
        repository.save(_job())
        mgr = _mock_resume_manager({"solo": "I have 5 years of SQL experience."})
        router = MagicMock()
        router.complete = AsyncMock(return_value=LLMResponse(
            text="Dear Hiring Manager,\n\nI am excited...",
            model="test", tokens_used=50, cached=False, latency_ms=5,
        ))

        result = asyncio.run(generate_cover_letter(
            job_id="j1",
            router=router,
            resume_manager=mgr,
        ))
        assert result is not None
        assert result.job_id == "j1"
        assert result.job_title == "Data Analyst"
        assert result.company == "Acme"
        assert result.resume_label == "solo"
        assert "Dear Hiring Manager" in result.letter
        assert result.file_path is not None
        assert result.file_path.exists()
        # Check file contents include header
        content = result.file_path.read_text(encoding="utf-8")
        assert "# Cover Letter" in content
        assert "Data Analyst" in content
        assert "Acme" in content
        assert "Dear Hiring Manager" in content

    def test_save_to_disk_false(self, isolated_storage):
        repository.save(_job())
        mgr = _mock_resume_manager({"solo": "resume text"})
        router = MagicMock()
        router.complete = AsyncMock(return_value=LLMResponse(
            text="Letter body",
            model="test", tokens_used=50, cached=False, latency_ms=5,
        ))

        result = asyncio.run(generate_cover_letter(
            job_id="j1",
            router=router,
            resume_manager=mgr,
            save_to_disk=False,
        ))
        assert result is not None
        assert result.letter == "Letter body"
        assert result.file_path is None

    def test_llm_failure_returns_empty_letter(self, isolated_storage):
        repository.save(_job())
        mgr = _mock_resume_manager({"solo": "resume text"})
        router = MagicMock()
        router.complete = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = asyncio.run(generate_cover_letter(
            job_id="j1",
            router=router,
            resume_manager=mgr,
        ))
        # CoverLetterWriter.generate catches and returns "", so we get
        # a result object with empty letter
        assert result is not None
        assert result.letter == ""
        assert result.file_path is None
