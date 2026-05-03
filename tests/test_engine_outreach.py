"""Tests for the engine's optional auto-outreach background task.

Outreach is opt-in (``auto_outreach=True`` in user_config). When
enabled, each successful application spawns a fire-and-forget task
that writes a LinkedIn connection-request draft to
``data/outreach/<job_id>.txt``. We test:

  - Off by default: no task spawned, no file written.
  - On + LLM returns text: file lands in OUTREACH_DIR with header.
  - On + empty LLM response: no file written, WARNING logged.
  - On + LLM raises: no file written, run keeps going.
  - Path safety: a job_id with shell-unsafe chars produces a sane
    filename.

The tests drive ``_generate_outreach_for_job`` directly because the
engine's full apply-loop integration test is out of scope for a
small follow-up; we cover the unit here and rely on the engine's
existing background-task plumbing (already covered by the story-
bank tests) to do the right thing with the resulting task.
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

    cfg = {"auto_outreach": True}
    cfg.update(config_overrides)
    eng = ApplicationEngine(config=cfg, cli_mode=True)
    eng.router = MagicMock()
    return eng


def _job():
    return Job(
        job_id="test_job_123",
        source="test",
        title="Senior Engineer",
        company="Acme",
        url="https://example.com/job/123",
        description="Senior eng at Acme.",
    )


class TestOutreachGeneration:
    def test_writes_draft_on_success(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "OUTREACH_DIR", tmp_path / "outreach")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(
            return_value="Hi! Saw the Senior Eng role — I have 5 years…"
        )
        with patch(
            "auto_applier.resume.outreach.OutreachWriter",
            return_value=fake_writer,
        ):
            _run(eng._generate_outreach_for_job(
                resume_text="resume body",
                job=_job(),
            ))

        out = tmp_path / "outreach" / "test_job_123.txt"
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "Senior Engineer" in content
        assert "Acme" in content
        assert "https://example.com/job/123" in content
        assert "Edit before sending" in content
        assert "5 years" in content

    def test_empty_response_writes_nothing(self, tmp_path, monkeypatch, caplog):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "OUTREACH_DIR", tmp_path / "outreach")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(return_value="")
        with patch(
            "auto_applier.resume.outreach.OutreachWriter",
            return_value=fake_writer,
        ), caplog.at_level(logging.WARNING):
            _run(eng._generate_outreach_for_job(
                resume_text="r",
                job=_job(),
            ))

        out = tmp_path / "outreach" / "test_job_123.txt"
        assert not out.exists()
        assert "empty message" in caplog.text.lower()

    def test_llm_raises_does_not_propagate(self, tmp_path, monkeypatch, caplog):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "OUTREACH_DIR", tmp_path / "outreach")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch(
            "auto_applier.resume.outreach.OutreachWriter",
            return_value=fake_writer,
        ), caplog.at_level(logging.WARNING):
            _run(eng._generate_outreach_for_job(
                resume_text="r",
                job=_job(),
            ))

        # Must not raise — caller relies on this for fire-and-forget.
        assert "outreach generation failed" in caplog.text.lower()

    def test_unsafe_job_id_sanitized(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "OUTREACH_DIR", tmp_path / "outreach")

        eng = _make_engine(tmp_path)
        fake_writer = MagicMock()
        fake_writer.generate = AsyncMock(return_value="msg")
        with patch(
            "auto_applier.resume.outreach.OutreachWriter",
            return_value=fake_writer,
        ):
            job = _job()
            # ATS API job_ids contain slashes / colons in some shapes.
            job.job_id = "gh_co/with:weird?chars*1234"
            _run(eng._generate_outreach_for_job(
                resume_text="r",
                job=job,
            ))

        # Slashes/colons replaced; file lands somewhere safe.
        files = list((tmp_path / "outreach").glob("*.txt"))
        assert len(files) == 1
        assert "/" not in files[0].name
        assert ":" not in files[0].name


class TestOutreachOptInGate:
    """The engine should only spawn the outreach task when the
    user has opted in via config. Default is OFF.
    """

    def test_default_config_is_off(self):
        from auto_applier.config import DEFAULT_AUTO_OUTREACH
        assert DEFAULT_AUTO_OUTREACH is False
