"""Tests for the STAR interview story bank."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.resume import story_bank
from auto_applier.resume.story_bank import (
    Story,
    StoryGenerator,
    append_stories,
    export_bank_markdown,
    load_bank,
    save_bank,
)


@pytest.fixture
def tmp_bank_file(tmp_path, monkeypatch):
    monkeypatch.setattr(story_bank, "STORY_BANK_FILE", tmp_path / "story_bank.json")
    return tmp_path / "story_bank.json"


def _make_story(title: str = "Test") -> Story:
    return Story(
        title=title,
        question_prompt="Tell me about a time...",
        situation="sit", task="task", action="act", result="res", reflection="ref",
    )


class TestLoadBank:
    def test_missing_file_returns_empty(self, tmp_bank_file):
        assert load_bank() == []

    def test_malformed_json_returns_empty(self, tmp_bank_file):
        tmp_bank_file.write_text("{ bad")
        assert load_bank() == []

    def test_round_trip_list_format(self, tmp_bank_file):
        save_bank([_make_story("Story A"), _make_story("Story B")])
        loaded = load_bank()
        assert len(loaded) == 2
        assert loaded[0].title == "Story A"

    def test_ignores_unknown_fields(self, tmp_bank_file):
        import json
        tmp_bank_file.write_text(json.dumps([
            {
                "title": "t", "question_prompt": "q",
                "situation": "s", "task": "t", "action": "a",
                "result": "r", "reflection": "ref",
                "garbage_field": "should be dropped",
            }
        ]))
        loaded = load_bank()
        assert len(loaded) == 1
        assert loaded[0].title == "t"


class TestAppendStories:
    def test_empty_list_is_noop(self, tmp_bank_file):
        append_stories([])
        assert load_bank() == []

    def test_appends_to_existing(self, tmp_bank_file):
        save_bank([_make_story("A")])
        append_stories([_make_story("B")])
        bank = load_bank()
        assert [s.title for s in bank] == ["A", "B"]


class TestStoryGenerator:
    def test_returns_empty_on_llm_exception(self, tmp_bank_file):
        router = MagicMock()
        router.complete_json = AsyncMock(side_effect=RuntimeError("boom"))
        gen = StoryGenerator(router)
        result = asyncio.run(gen.generate(
            resume_text="x", job_description="y",
            company_name="Acme", job_title="Analyst",
        ))
        assert result == []

    def test_filters_stories_missing_segments(self, tmp_bank_file):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "stories": [
                {
                    "title": "complete",
                    "question_prompt": "q",
                    "situation": "s", "task": "t", "action": "a",
                    "result": "r", "reflection": "ref",
                },
                {
                    "title": "missing result",
                    "question_prompt": "q",
                    "situation": "s", "task": "t", "action": "a",
                    "result": "", "reflection": "ref",
                },
            ]
        })
        gen = StoryGenerator(router)
        result = asyncio.run(gen.generate(
            resume_text="x", job_description="y",
            company_name="Acme", job_title="Analyst",
        ))
        assert len(result) == 1
        assert result[0].title == "complete"

    def test_malformed_response_returns_empty(self, tmp_bank_file):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={"stories": "not a list"})
        gen = StoryGenerator(router)
        result = asyncio.run(gen.generate(
            resume_text="x", job_description="y",
            company_name="Acme", job_title="Analyst",
        ))
        assert result == []

    def test_provenance_attached(self, tmp_bank_file):
        router = MagicMock()
        router.complete_json = AsyncMock(return_value={
            "stories": [{
                "title": "t", "question_prompt": "q",
                "situation": "s", "task": "t", "action": "a",
                "result": "r", "reflection": "ref",
            }]
        })
        gen = StoryGenerator(router)
        result = asyncio.run(gen.generate(
            resume_text="x", job_description="y",
            company_name="Acme", job_title="Analyst",
            job_id="li-123", resume_label="analyst",
        ))
        assert result[0].job_id == "li-123"
        assert result[0].company == "Acme"
        assert result[0].resume_label == "analyst"


class TestExport:
    def test_empty_bank(self, tmp_bank_file):
        out = export_bank_markdown()
        assert "Interview Story Bank" in out
        assert "empty" in out.lower()

    def test_populated_bank(self, tmp_bank_file):
        save_bank([_make_story("One"), _make_story("Two")])
        out = export_bank_markdown()
        assert "# Interview Story Bank" in out
        assert "2 stories" in out
        assert "One" in out
        assert "Two" in out
        assert "**Situation.**" in out
        assert "**Reflection.**" in out
