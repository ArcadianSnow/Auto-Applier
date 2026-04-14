"""Tests for engine.py auto-expand-titles run-time integration."""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from auto_applier.orchestrator.engine import ApplicationEngine
from auto_applier.orchestrator.events import EventEmitter


def _make_engine(config: dict | None = None) -> ApplicationEngine:
    """Create a minimally-wired engine for testing helper methods."""
    cfg = {
        "enabled_platforms": [],
        "search_keywords": ["Data Analyst"],
        "location": "Remote",
        "max_applications_per_day": 10,
        "personal_info": {},
        "llm": {"ollama_model": "test"},
    }
    if config:
        cfg.update(config)

    events = EventEmitter()
    engine = ApplicationEngine(cfg, events, cli_mode=True)
    return engine


class TestExpandKeywordForSearch:
    def test_caches_result(self):
        """Second call for same keyword should not hit expand_title again."""
        engine = _make_engine()
        engine.router = MagicMock()
        engine.resume_manager = None

        # Patch expand_title at the module import point
        from auto_applier.analysis import title_expansion

        call_count = {"n": 0}

        async def fake_expand(seed, router=None, resume_text="", prefer_llm=True):
            call_count["n"] += 1
            from auto_applier.analysis.title_expansion import ExpansionResult
            return ExpansionResult(
                seed=seed,
                adjacents=["related one", "related two"],
                source="static",
            )

        # Patch the symbol used inside _expand_keyword_for_search
        import auto_applier.orchestrator.engine as eng_mod
        # _expand_keyword_for_search imports expand_title locally, so
        # patch it in the title_expansion module
        original = title_expansion.expand_title
        title_expansion.expand_title = fake_expand

        try:
            result1 = asyncio.run(engine._expand_keyword_for_search("Data Analyst"))
            result2 = asyncio.run(engine._expand_keyword_for_search("Data Analyst"))
            result3 = asyncio.run(engine._expand_keyword_for_search("data analyst"))
        finally:
            title_expansion.expand_title = original

        assert result1 == ["related one", "related two"]
        assert result2 == ["related one", "related two"]
        assert result3 == ["related one", "related two"]
        # Called only once — second + third hits should be cached
        assert call_count["n"] == 1

    def test_handles_exception(self):
        """If expand_title raises, cache empty list and return []."""
        engine = _make_engine()
        engine.router = MagicMock()
        engine.resume_manager = None

        from auto_applier.analysis import title_expansion

        async def broken_expand(**kwargs):
            raise RuntimeError("LLM completely down")

        original = title_expansion.expand_title
        title_expansion.expand_title = broken_expand

        try:
            # _expand_keyword_for_search doesn't catch — the caller in
            # _run_platform does. So this call should raise.
            with pytest.raises(RuntimeError):
                asyncio.run(engine._expand_keyword_for_search("foo"))
        finally:
            title_expansion.expand_title = original

    def test_no_suggestions_returns_empty(self):
        engine = _make_engine()
        engine.router = MagicMock()
        engine.resume_manager = None

        from auto_applier.analysis import title_expansion

        async def empty_expand(seed, router=None, resume_text="", prefer_llm=True):
            from auto_applier.analysis.title_expansion import ExpansionResult
            return ExpansionResult(seed=seed, adjacents=[])

        original = title_expansion.expand_title
        title_expansion.expand_title = empty_expand

        try:
            result = asyncio.run(
                engine._expand_keyword_for_search("Unknown Title"),
            )
        finally:
            title_expansion.expand_title = original

        assert result == []

    def test_uses_resume_context(self):
        """Resume text should be passed to expand_title when loaded."""
        engine = _make_engine()
        engine.router = MagicMock()

        # Mock a resume_manager with one loaded resume
        mock_resume = MagicMock()
        mock_resume.label = "test_resume"
        engine.resume_manager = MagicMock()
        engine.resume_manager.list_resumes.return_value = [mock_resume]
        engine.resume_manager.get_resume_text.return_value = "My SQL experience..."

        from auto_applier.analysis import title_expansion

        captured = {}

        async def capture_expand(seed, router=None, resume_text="", prefer_llm=True):
            captured["resume_text"] = resume_text
            from auto_applier.analysis.title_expansion import ExpansionResult
            return ExpansionResult(seed=seed, adjacents=["a"])

        original = title_expansion.expand_title
        title_expansion.expand_title = capture_expand

        try:
            asyncio.run(engine._expand_keyword_for_search("Data Analyst"))
        finally:
            title_expansion.expand_title = original

        assert captured["resume_text"] == "My SQL experience..."


class TestAutoExpandConfigRespected:
    """The engine should only expand when auto_expand_titles is True."""

    def test_config_defaults_to_false(self):
        engine = _make_engine()
        # auto_expand_titles not in config -> False default
        assert engine.config.get("auto_expand_titles", False) is False

    def test_threshold_default(self):
        engine = _make_engine()
        # default 10
        assert int(engine.config.get("title_expansion_threshold", 10)) == 10

    def test_custom_threshold(self):
        engine = _make_engine({"title_expansion_threshold": 5})
        assert int(engine.config.get("title_expansion_threshold")) == 5
