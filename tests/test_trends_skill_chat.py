"""Tests for the trends panel's "Discuss this skill" chat dialog.

The dialog is heavily Tk-coupled, so we test it the same way as
the other panel scaffolds: import canary, helper-function unit
tests, and pure-data tests on the format_conversation /
collect_profile helpers. Tk-mainloop interactions (button clicks,
worker threads) aren't exercised — they need a live mainloop and
flake under parallel pytest. The import canary catches structural
breakage at collection time.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_module_imports_clean():
    """If the dialog module fails to load (missing import, syntax
    error, broken reference), the trends panel's "Discuss" button
    will explode at click time. Catch that at collection."""
    from auto_applier.gui.panels import trends_skill_chat  # noqa: F401


class TestFormatConversation:
    """The conversation-format helper turns the dialog's history
    list into the prompt's plain-text 'User: ... / Assistant: ...'
    block. Pure function; trivially testable."""

    def test_empty_history(self):
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        out = TrendsSkillChatDialog._format_conversation([])
        assert out == ""

    def test_single_user_turn(self):
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        out = TrendsSkillChatDialog._format_conversation([
            {"role": "user", "text": "What is Snowflake?"},
        ])
        assert "User: What is Snowflake?" in out

    def test_alternating_turns(self):
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        out = TrendsSkillChatDialog._format_conversation([
            {"role": "user", "text": "Tell me about X"},
            {"role": "assist", "text": "X is a database."},
            {"role": "user", "text": "Should I learn it?"},
        ])
        assert "User: Tell me about X" in out
        assert "Assistant: X is a database" in out
        assert "User: Should I learn it" in out
        # Order preserved
        assert out.find("Tell me about X") < out.find("X is a database")

    def test_strips_whitespace(self):
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        out = TrendsSkillChatDialog._format_conversation([
            {"role": "user", "text": "   leading whitespace   "},
        ])
        # Leading/trailing whitespace stripped
        assert "User: leading whitespace" in out
        assert "User:    leading" not in out


class TestCollectProfile:
    """Profile is read from user_config.json. Covers absent file,
    full info, partial info, and shape variations the wizard /
    fixture generator have written historically."""

    def test_missing_config_returns_empty(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        monkeypatch.setattr(
            cfg_mod, "USER_CONFIG_FILE", tmp_path / "no_such.json",
        )
        assert TrendsSkillChatDialog._collect_profile() == ""

    def test_full_profile_renders_lines(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        cfg = tmp_path / "user_config.json"
        cfg.write_text(json.dumps({
            "personal_info": {
                "first_name": "Jane",
                "last_name": "Doe",
            },
            "location": "Remote",
            "search_keywords": ["data analyst", "data engineer"],
        }), encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg)

        out = TrendsSkillChatDialog._collect_profile()
        assert "Jane Doe" in out
        assert "Remote" in out
        assert "data analyst" in out
        assert "data engineer" in out

    def test_combined_name_field(self, tmp_path, monkeypatch):
        """Some configs use 'name' instead of first/last. Both shapes
        must be accepted."""
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        cfg = tmp_path / "user_config.json"
        cfg.write_text(json.dumps({
            "personal_info": {"name": "Jane Doe"},
        }), encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg)

        out = TrendsSkillChatDialog._collect_profile()
        assert "Jane Doe" in out

    def test_malformed_config_returns_empty_safely(
        self, tmp_path, monkeypatch,
    ):
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        cfg = tmp_path / "user_config.json"
        cfg.write_text("{not json}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "USER_CONFIG_FILE", cfg)

        # Must NOT raise; returns empty string so the dialog still
        # works (just without profile context).
        assert TrendsSkillChatDialog._collect_profile() == ""


class TestCollectResumeText:
    """Resume aggregation from data/resumes/. Verifies dotfile
    skipping and parser-failure resilience."""

    def test_no_resumes_dir_returns_empty(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        # Point RESUMES_DIR at a non-existent path
        monkeypatch.setattr(
            cfg_mod, "RESUMES_DIR", tmp_path / "no_such_dir",
        )
        assert TrendsSkillChatDialog._collect_resume_text() == ""

    def test_skips_dotfiles(self, tmp_path, monkeypatch):
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        resumes = tmp_path / "resumes"
        resumes.mkdir()
        # A dotfile that should be ignored
        (resumes / ".DS_Store").write_bytes(b"not a resume")

        monkeypatch.setattr(cfg_mod, "RESUMES_DIR", resumes)

        # Mock extract_text — if it gets called on .DS_Store the
        # test fails (the dotfile filter is upstream).
        with patch(
            "auto_applier.resume.parser.extract_text",
            side_effect=AssertionError("dotfile should be skipped"),
        ):
            out = TrendsSkillChatDialog._collect_resume_text()
        assert out == ""

    def test_parser_failure_skipped_silently(self, tmp_path, monkeypatch):
        """If extract_text raises on one resume, others should still
        be aggregated. Resilience matters because the dialog is
        opened from many entry points and a malformed resume
        shouldn't block career-coaching."""
        from auto_applier import config as cfg_mod
        from auto_applier.gui.panels.trends_skill_chat import (
            TrendsSkillChatDialog,
        )
        resumes = tmp_path / "resumes"
        resumes.mkdir()
        (resumes / "good.pdf").write_bytes(b"good resume content")
        (resumes / "bad.pdf").write_bytes(b"")

        monkeypatch.setattr(cfg_mod, "RESUMES_DIR", resumes)

        def fake_extract(p: Path) -> str:
            if "bad" in p.name:
                raise RuntimeError("simulated parser failure")
            return "Good resume content here."

        with patch(
            "auto_applier.resume.parser.extract_text",
            side_effect=fake_extract,
        ):
            out = TrendsSkillChatDialog._collect_resume_text()

        # The good resume was included, the bad one silently skipped
        assert "Good resume content" in out
        assert "good.pdf" in out
        assert "bad.pdf" not in out


class TestTurnCaps:
    """Class-level constants pin the conversation cap so a future
    refactor can't silently drop them past the working range
    (Gemma 4 instruction-following degrades past ~8k chars in
    prompt, which 12 turns approximately hits)."""

    def test_turn_caps_present(self):
        from auto_applier.gui.panels import trends_skill_chat
        # MAX_TURNS must exist and be at least 6 (otherwise the
        # dialog locks too early for useful coaching) and at most
        # 20 (otherwise prompts grow past local-LLM tolerance)
        assert 6 <= trends_skill_chat.MAX_TURNS <= 20
        # WARN_TURNS must give the user a 1-3 turn buffer to wrap up
        assert 1 <= (trends_skill_chat.MAX_TURNS - trends_skill_chat.WARN_TURNS) <= 3
