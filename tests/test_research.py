"""Tests for company research briefing generation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.analysis import research as research_mod
from auto_applier.analysis.research import (
    CompanyBriefing,
    CompanyResearcher,
    briefing_path,
    load_briefing,
    save_briefing,
)


@pytest.fixture
def tmp_research_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(research_mod, "RESEARCH_DIR", tmp_path)
    return tmp_path


def _briefing(company: str = "Acme") -> CompanyBriefing:
    return CompanyBriefing(
        company=company,
        what_they_do="They build things.",
        tech_stack_signals=["Python", "Kafka"],
        culture_signals=["Remote-first"],
        red_flags=[],
        questions_to_ask=["Why?", "How?"],
        talking_points=["Open source"],
    )


class TestBriefingPath:
    def test_normalizes_company_name(self, tmp_research_dir):
        p = briefing_path("Acme Inc.")
        assert p.name.startswith("acme")
        assert p.suffix == ".md"

    def test_strips_unsafe_chars(self, tmp_research_dir):
        p = briefing_path("Acme/../hack")
        assert "/" not in p.name
        assert ".." not in p.name


class TestCompanyBriefingMarkdown:
    def test_all_sections_present(self):
        md = _briefing().to_markdown()
        assert "# Acme" in md
        assert "## What they do" in md
        assert "## Tech stack signals" in md
        assert "## Culture signals" in md
        assert "## Red flags" in md
        assert "## Questions to ask" in md
        assert "## Talking points" in md

    def test_empty_lists_render_placeholder(self):
        b = CompanyBriefing(
            company="x", what_they_do="y",
            tech_stack_signals=[], culture_signals=[], red_flags=[],
            questions_to_ask=[], talking_points=[],
        )
        md = b.to_markdown()
        # Every list section should render the placeholder
        assert md.count("_not in source_") >= 5

    def test_list_items_as_bullets(self):
        md = _briefing().to_markdown()
        assert "- Python" in md
        assert "- Kafka" in md


class TestSaveAndLoadBriefing:
    def test_round_trip(self, tmp_research_dir):
        b = _briefing()
        path = save_briefing(b)
        assert path.exists()
        # JSON companion written too
        assert path.with_suffix(".json").exists()

        loaded = load_briefing("Acme")
        assert loaded is not None
        assert loaded.what_they_do == b.what_they_do
        assert loaded.tech_stack_signals == b.tech_stack_signals

    def test_missing_returns_none(self, tmp_research_dir):
        assert load_briefing("NonexistentCo") is None

    def test_corrupted_json_returns_none(self, tmp_research_dir):
        path = briefing_path("Acme").with_suffix(".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        assert load_briefing("Acme") is None


class TestCompanyResearcher:
    def test_empty_source_returns_none(self, tmp_research_dir):
        router = MagicMock()
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "   \n  "))
        assert result is None
        router.complete_json.assert_not_called()

    def test_llm_exception_returns_none(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(side_effect=RuntimeError("boom"))
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "some real text"))
        assert result is None

    def test_empty_what_they_do_rejected(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "what_they_do": "",
            "tech_stack_signals": ["Python"],
        })
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "source text"))
        assert result is None

    def test_not_in_source_placeholder_rejected(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "what_they_do": "not in source",
        })
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "source text"))
        assert result is None

    def test_valid_response_parsed(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "what_they_do": "They build developer tools.",
            "tech_stack_signals": ["Rust", "WebAssembly"],
            "culture_signals": ["Engineering-led"],
            "red_flags": [],
            "questions_to_ask": ["What's your release cadence?"],
            "talking_points": ["Their open-source framework"],
        })
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "source text"))
        assert result is not None
        assert "developer tools" in result.what_they_do
        assert "Rust" in result.tech_stack_signals

    def test_non_list_fields_coerce_to_empty(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "what_they_do": "y",
            "tech_stack_signals": "Python, Rust",  # string, not list
            "culture_signals": None,
            "red_flags": [],
            "questions_to_ask": [],
            "talking_points": [],
        })
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "source"))
        assert result is not None
        assert result.tech_stack_signals == []
        assert result.culture_signals == []

    def test_strips_empty_list_entries(self, tmp_research_dir):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "what_they_do": "y",
            "tech_stack_signals": ["Python", "", "  ", "Rust"],
            "culture_signals": [],
            "red_flags": [],
            "questions_to_ask": [],
            "talking_points": [],
        })
        r = CompanyResearcher(router)
        result = asyncio.run(r.research("Acme", "source"))
        assert result.tech_stack_signals == ["Python", "Rust"]
