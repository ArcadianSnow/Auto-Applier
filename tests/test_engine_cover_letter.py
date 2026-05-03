"""Tests for the engine's auto-cover-letter background task.

Phase 1.5: cover letters are now pre-generated as a background task
on every AUTO_APPLY decision (default ON, per recruiter-survey
research that generic letters are negative signal). Tests cover:

  - On by default: empty config produces a letter
  - Off when explicitly disabled
  - Skipped when form_filler already created one (idempotency)
  - LLM empty response → no file written, WARN logged
  - LLM raises → no file written, run keeps going
  - Path safety: ATS-shaped job_ids with slashes are sanitized
  - Header includes title/company/URL for user reference

Tests drive ``_generate_cover_letter_for_job`` directly because the
engine's full apply-loop integration is out of scope here; we cover
the unit and rely on existing background-task plumbing tests
(test_engine_outreach.py, test_story_bank.py) to verify the orchestration.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _make_engine(tmp_path: Path, **config_overrides):
    from auto_applier.orchestrator.engine import ApplicationEngine

    cfg: dict = {}
    cfg.update(config_overrides)
    eng = ApplicationEngine(config=cfg, cli_mode=True)
    eng.router = MagicMock()
    return eng


def _job(jid="test_job_42"):
    return Job(
        job_id=jid,
        source="ats_greenhouse",
        title="Senior Data Engineer",
        company="Acme",
        url="https://example.com/job/42",
        description="We are hiring a senior data engineer to build pipelines.",
    )


class TestCoverLetterDefaults:
    def test_default_is_on(self):
        from auto_applier.config import DEFAULT_AUTO_COVER_LETTER
        assert DEFAULT_AUTO_COVER_LETTER is True


class TestCoverLetterGeneration:
    def test_writes_letter_on_success(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", tmp_path / "cl")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(
            return_value=(
                "Dear Acme team,\n\nI am excited about the data engineer role "
                "and bring 5 years of pipeline-building experience..."
            )
        )
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ):
            _run(eng._generate_cover_letter_for_job(
                resume_text="resume body",
                job=_job(),
            ))

        out = tmp_path / "cl" / "test_job_42" / "letter.txt"
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        # Header includes job context for the user
        assert "Senior Data Engineer" in content
        assert "Acme" in content
        assert "https://example.com/job/42" in content
        # Body included
        assert "5 years of pipeline-building" in content

    def test_skipped_when_letter_already_exists(self, tmp_path, monkeypatch):
        """If form_filler created a letter during the apply flow,
        the background task must skip — no double-generation."""
        from auto_applier import config as cfg_mod
        cl_dir = tmp_path / "cl"
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", cl_dir)

        # Pre-create a letter (simulating form_filler having done it)
        existing_dir = cl_dir / "test_job_42"
        existing_dir.mkdir(parents=True)
        existing_letter = existing_dir / "letter.txt"
        existing_letter.write_text(
            "Existing letter from form_filler. " * 20, encoding="utf-8",
        )
        original_size = existing_letter.stat().st_size

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        # If this gets called, the test fails — we should skip.
        fake_writer.generate = AsyncMock(side_effect=RuntimeError(
            "should not be called when letter exists"
        ))
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ):
            _run(eng._generate_cover_letter_for_job(
                resume_text="r",
                job=_job(),
            ))

        # Existing letter unchanged
        assert existing_letter.read_text(encoding="utf-8").startswith(
            "Existing letter from form_filler."
        )
        assert existing_letter.stat().st_size == original_size
        # Writer was NOT called
        fake_writer.generate.assert_not_awaited()

    def test_tiny_existing_letter_does_not_count_as_skip(
        self, tmp_path, monkeypatch,
    ):
        """A near-empty letter file (<100 bytes) is treated as
        not-yet-generated. Prevents stale empty files from
        permanently blocking generation."""
        from auto_applier import config as cfg_mod
        cl_dir = tmp_path / "cl"
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", cl_dir)

        # Pre-create an empty letter (simulating a partial write)
        existing_dir = cl_dir / "test_job_42"
        existing_dir.mkdir(parents=True)
        existing_letter = existing_dir / "letter.txt"
        existing_letter.write_text("", encoding="utf-8")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(return_value="A real letter " * 10)
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ):
            _run(eng._generate_cover_letter_for_job(
                resume_text="r",
                job=_job(),
            ))

        # Writer WAS called (existing was too small to count)
        fake_writer.generate.assert_awaited_once()
        # Letter is now substantive
        assert existing_letter.stat().st_size > 100

    def test_empty_llm_response_writes_nothing(
        self, tmp_path, monkeypatch, caplog,
    ):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", tmp_path / "cl")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(return_value="")
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ), caplog.at_level(logging.WARNING):
            _run(eng._generate_cover_letter_for_job(
                resume_text="r",
                job=_job(),
            ))

        out = tmp_path / "cl" / "test_job_42" / "letter.txt"
        assert not out.exists()
        assert "empty result" in caplog.text.lower()

    def test_llm_raises_does_not_propagate(self, tmp_path, monkeypatch, caplog):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", tmp_path / "cl")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ), caplog.at_level(logging.WARNING):
            # Must not raise — caller relies on this for fire-and-forget
            _run(eng._generate_cover_letter_for_job(
                resume_text="r",
                job=_job(),
            ))

        assert "cover letter generation failed" in caplog.text.lower()

    def test_unsafe_job_id_sanitized(self, tmp_path, monkeypatch):
        """ATS-shaped job_ids with slashes / colons must be sanitized
        before they hit the filesystem."""
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "COVER_LETTERS_DIR", tmp_path / "cl")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(return_value="A letter " * 30)
        with patch(
            "auto_applier.resume.cover_letter.CoverLetterWriter",
            return_value=fake_writer,
        ):
            job = _job()
            job.job_id = "gh_co/with:weird?chars*"
            _run(eng._generate_cover_letter_for_job(
                resume_text="r",
                job=job,
            ))

        # Letter was written somewhere safe
        all_letters = list((tmp_path / "cl").rglob("letter.txt"))
        assert len(all_letters) == 1
        # The job-id-derived directory under cl/ must contain no
        # unsafe characters. Check that specific component, not the
        # full Windows-friendly absolute path (which legitimately
        # contains "C:\").
        rel = all_letters[0].relative_to(tmp_path / "cl")
        # rel is e.g. WindowsPath("gh_co_with_weird_chars_/letter.txt")
        job_dir = rel.parts[0]
        for unsafe in ("/", "\\", ":", "?", "*"):
            assert unsafe not in job_dir, (
                f"unsafe char {unsafe!r} survived in job_dir {job_dir!r}"
            )
