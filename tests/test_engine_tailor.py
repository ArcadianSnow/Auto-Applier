"""Tests for the engine's pre-apply resume tailoring (Phase 1.6).

Auto-tailor is the largest Phase 1 quality multiplier. Per research,
it produces a 3-5x interview-rate uplift. The wiring lives at
``ApplicationEngine._tailor_resume_for_job`` and is called between
``APPLICATION_STARTED`` and the actual ``apply_to_job`` invocation.

Tests cover:

  - Disabled config returns base resume path unchanged
  - Cache hit (existing PDF >1KB) skips the LLM call
  - LLM returns None → fall back to base resume
  - LLM returns tailored, PDF renders → return tailored path
  - LLM returns tailored, PDF render fails → fall back to base
  - Exception during tailor → fall back to base
  - DOCX render failure does NOT block (best-effort sibling)

The point is: under no circumstance does this method raise or
return a missing path. The apply MUST proceed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_applier.storage.models import Job


def _run(coro):
    return asyncio.run(coro)


def _job(jid="test_tailor_job_1"):
    return Job(
        job_id=jid,
        source="indeed",
        title="Senior Data Engineer",
        company="Acme",
        url="https://example.com/job/1",
        description="Build modern data pipelines for a large fintech.",
    )


def _make_engine(**config_overrides):
    from auto_applier.orchestrator.engine import ApplicationEngine
    cfg: dict = {}
    cfg.update(config_overrides)
    eng = ApplicationEngine(config=cfg, cli_mode=True)
    eng.router = MagicMock()
    return eng


class TestAutoTailorConfigDefaults:
    def test_default_is_on(self):
        from auto_applier.config import DEFAULT_AUTO_TAILOR_RESUME
        assert DEFAULT_AUTO_TAILOR_RESUME is True

    def test_disabled_returns_base_unchanged(self, tmp_path, monkeypatch):
        eng = _make_engine(auto_tailor_resume=False)
        base = "/path/to/base_resume.pdf"
        result = _run(eng._tailor_resume_for_job(
            job=_job(), base_resume_path=base,
            resume_text="x", resume_label="default",
        ))
        # Disabled = pass-through, no LLM call
        assert result == base


class TestTailorCacheHit:
    def test_cache_hit_returns_tailored_path_without_llm_call(
        self, tmp_path, monkeypatch,
    ):
        """A pre-existing tailored PDF >1KB at the canonical path
        means we already tailored this job. Skip the LLM call."""
        from auto_applier import config as cfg_mod
        gen_dir = tmp_path / "generated"
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", gen_dir)

        eng = _make_engine()
        # Pre-stage a "tailored" PDF (~2KB content)
        from auto_applier.resume.tailor import tailored_pdf_path
        cached = tailored_pdf_path("test_tailor_job_1")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"X" * 2048)

        # Patch the tailor: if called, the test fails.
        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            side_effect=AssertionError("LLM should not be called on cache hit"),
        ):
            result = _run(eng._tailor_resume_for_job(
                job=_job(), base_resume_path="/base.pdf",
                resume_text="x", resume_label="default",
            ))

        assert result == str(cached)

    def test_tiny_existing_file_does_not_count_as_cache_hit(
        self, tmp_path, monkeypatch,
    ):
        """A near-empty PDF (<1KB) should NOT be treated as cached
        — likely a partial write from a prior crash. Re-tailor."""
        from auto_applier import config as cfg_mod
        gen_dir = tmp_path / "generated"
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", gen_dir)

        eng = _make_engine()
        from auto_applier.resume.tailor import tailored_pdf_path
        partial = tailored_pdf_path("test_tailor_job_1")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"x" * 100)  # 100 bytes — too small

        # This time the tailor SHOULD be called. We mock it to
        # produce a tailor-returned-None (so we end up returning
        # the base path, but the tailor was called).
        fake_tailor = MagicMock()
        fake_tailor.tailor = AsyncMock(return_value=None)
        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            return_value=fake_tailor,
        ):
            result = _run(eng._tailor_resume_for_job(
                job=_job(), base_resume_path="/base.pdf",
                resume_text="x", resume_label="default",
            ))

        fake_tailor.tailor.assert_awaited_once()
        # And since tailor returned None, we get base.
        assert result == "/base.pdf"


class TestTailorFailureFallbacks:
    """Failure-mode coverage: tailor MUST never block the apply.
    Every failure path returns the base resume."""

    def test_tailor_returns_none_falls_back(self, tmp_path, monkeypatch, caplog):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", tmp_path / "g")

        eng = _make_engine()
        fake_tailor = MagicMock()
        fake_tailor.tailor = AsyncMock(return_value=None)
        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            return_value=fake_tailor,
        ):
            result = _run(eng._tailor_resume_for_job(
                job=_job(), base_resume_path="/base.pdf",
                resume_text="x", resume_label="default",
            ))
        assert result == "/base.pdf"

    def test_tailor_raises_falls_back(self, tmp_path, monkeypatch, caplog):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", tmp_path / "g")

        eng = _make_engine()
        fake_tailor = MagicMock()
        fake_tailor.tailor = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            return_value=fake_tailor,
        ):
            import logging
            with caplog.at_level(logging.WARNING):
                result = _run(eng._tailor_resume_for_job(
                    job=_job(), base_resume_path="/base.pdf",
                    resume_text="x", resume_label="default",
                ))

        assert result == "/base.pdf"
        assert "unexpected failure" in caplog.text.lower()

    def test_pdf_render_fails_falls_back(self, tmp_path, monkeypatch, caplog):
        """LLM produces a tailored result but Playwright PDF
        renderer fails. Must fall back to base, not return the
        nonexistent or zero-byte tailored path."""
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", tmp_path / "g")

        from auto_applier.resume.tailor import TailoredResume
        eng = _make_engine()
        fake_tailor = MagicMock()
        fake_tailor.tailor = AsyncMock(return_value=TailoredResume(
            summary="Solid eng with 5y data exp.",
            skills=["Python", "SQL"],
            experience=[],
            education=[],
            job_id="test_tailor_job_1",
        ))
        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            return_value=fake_tailor,
        ), patch(
            "auto_applier.resume.tailor.render_pdf",
            new=AsyncMock(return_value=False),
        ), patch(
            "auto_applier.resume.tailor.render_docx",
            new=AsyncMock(return_value=False),
        ):
            import logging
            with caplog.at_level(logging.WARNING):
                result = _run(eng._tailor_resume_for_job(
                    job=_job(), base_resume_path="/base.pdf",
                    resume_text="x", resume_label="default",
                ))

        assert result == "/base.pdf"
        assert "render failed" in caplog.text.lower() or "render" in caplog.text.lower()

    def test_docx_failure_does_not_block_pdf_success(
        self, tmp_path, monkeypatch,
    ):
        """DOCX is best-effort. PDF success + DOCX failure should
        still return the tailored PDF."""
        from auto_applier import config as cfg_mod
        gen_dir = tmp_path / "g"
        monkeypatch.setattr(cfg_mod, "GENERATED_RESUMES_DIR", gen_dir)

        from auto_applier.resume.tailor import (
            TailoredResume, tailored_pdf_path,
        )
        eng = _make_engine()
        fake_tailor = MagicMock()
        fake_tailor.tailor = AsyncMock(return_value=TailoredResume(
            summary="x", skills=["y"], experience=[], education=[],
            job_id="test_tailor_job_1",
        ))

        # render_pdf succeeds (and creates the file so cache logic works)
        async def fake_pdf(html, out_path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"%PDF" + b"X" * 4096)
            return True

        with patch(
            "auto_applier.resume.tailor.ResumeTailor",
            return_value=fake_tailor,
        ), patch(
            "auto_applier.resume.tailor.render_pdf",
            side_effect=fake_pdf,
        ), patch(
            "auto_applier.resume.tailor.render_docx",
            new=AsyncMock(side_effect=RuntimeError("docx broken")),
        ):
            result = _run(eng._tailor_resume_for_job(
                job=_job(), base_resume_path="/base.pdf",
                resume_text="x", resume_label="default",
            ))

        # Tailored PDF returned despite DOCX failure
        expected = tailored_pdf_path("test_tailor_job_1")
        assert result == str(expected)
        assert expected.exists()

    def test_import_failure_falls_back(self, tmp_path, monkeypatch):
        """If tailor.py itself can't be imported (corrupted install,
        future refactor breakage), fall back rather than crash."""
        eng = _make_engine()
        import builtins
        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "auto_applier.resume.tailor":
                raise ImportError("simulated")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = _run(eng._tailor_resume_for_job(
            job=_job(), base_resume_path="/base.pdf",
            resume_text="x", resume_label="default",
        ))
        assert result == "/base.pdf"


class TestCandidateHeaderHelper:
    def test_falls_back_to_candidate_when_no_config(
        self, tmp_path, monkeypatch,
    ):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(
            cfg_mod, "USER_CONFIG_FILE", tmp_path / "no_such_config.json",
        )
        eng = _make_engine()
        name, contact = eng._candidate_header_for_render()
        assert name == "Candidate"
        assert contact == ""

    def test_assembles_full_name_and_contact(
        self, tmp_path, monkeypatch,
    ):
        import json as _json
        from auto_applier import config as cfg_mod
        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(_json.dumps({
            "personal_info": {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@example.com",
                "phone": "555-1234",
                "city_state": "Seattle, WA",
                "linkedin_url": "https://linkedin.com/in/janedoe",
            },
        }), encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg_file)

        eng = _make_engine()
        name, contact = eng._candidate_header_for_render()
        assert name == "Jane Doe"
        assert "jane@example.com" in contact
        assert "555-1234" in contact
        assert "Seattle, WA" in contact
        assert "linkedin.com" in contact
        # Pipe separator joins parts
        assert " | " in contact

    def test_handles_partial_name(self, tmp_path, monkeypatch):
        """Only first_name set — should still produce a valid header."""
        import json as _json
        from auto_applier import config as cfg_mod
        cfg_file = tmp_path / "user_config.json"
        cfg_file.write_text(_json.dumps({
            "personal_info": {"first_name": "Jane"},
        }), encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg_file)

        eng = _make_engine()
        name, _ = eng._candidate_header_for_render()
        assert name == "Jane"
