"""Tests for analysis/learning_goals.py — skill state tracking."""

import json

import pytest

from auto_applier.analysis import learning_goals as lg
from auto_applier.analysis.learning_goals import (
    VALID_STATES,
    get_state,
    list_goals,
    remove,
    set_state,
    skills_by_state,
)


@pytest.fixture
def isolated_goals(tmp_path, monkeypatch):
    """Isolate learning_goals.json to tmp_path for each test."""
    monkeypatch.setattr("auto_applier.analysis.learning_goals.DATA_DIR", tmp_path)
    return tmp_path


class TestSetState:
    def test_new_skill(self, isolated_goals):
        set_state("Python", "learning")
        assert get_state("Python") == "learning"

    def test_case_insensitive(self, isolated_goals):
        set_state("Python", "certified")
        assert get_state("python") == "certified"
        assert get_state("PYTHON") == "certified"

    def test_update_existing(self, isolated_goals):
        set_state("tableau", "learning")
        set_state("tableau", "certified")
        assert get_state("tableau") == "certified"

    def test_invalid_state_raises(self, isolated_goals):
        with pytest.raises(ValueError):
            set_state("python", "mastered")

    def test_empty_skill_raises(self, isolated_goals):
        with pytest.raises(ValueError):
            set_state("", "learning")
        with pytest.raises(ValueError):
            set_state("   ", "learning")

    def test_persists_to_disk(self, isolated_goals):
        set_state("Python", "learning")
        # Read the file directly
        path = isolated_goals / "learning_goals.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "python" in data
        assert data["python"]["state"] == "learning"
        assert "added_at" in data["python"]


class TestGetState:
    def test_missing_returns_none(self, isolated_goals):
        assert get_state("nonexistent") is None

    def test_empty_skill(self, isolated_goals):
        assert get_state("") is None


class TestListGoals:
    def test_empty(self, isolated_goals):
        assert list_goals() == []

    def test_all_skills(self, isolated_goals):
        set_state("python", "certified")
        set_state("go", "learning")
        set_state("rust", "not_interested")
        result = list_goals()
        assert len(result) == 3
        # sorted alphabetically
        assert [s for s, _ in result] == ["go", "python", "rust"]

    def test_filter_by_state(self, isolated_goals):
        set_state("python", "certified")
        set_state("go", "learning")
        set_state("rust", "not_interested")
        learning = list_goals(state="learning")
        assert learning == [("go", "learning")]

    def test_filter_unknown_state_empty(self, isolated_goals):
        set_state("python", "certified")
        result = list_goals(state="mastered")
        assert result == []


class TestRemove:
    def test_existing(self, isolated_goals):
        set_state("python", "learning")
        assert remove("python") is True
        assert get_state("python") is None

    def test_case_insensitive(self, isolated_goals):
        set_state("PYTHON", "learning")
        assert remove("python") is True

    def test_missing_returns_false(self, isolated_goals):
        assert remove("nonexistent") is False


class TestSkillsByState:
    def test_empty(self, isolated_goals):
        result = skills_by_state()
        assert result == {
            "learning": set(),
            "certified": set(),
            "not_interested": set(),
        }

    def test_grouping(self, isolated_goals):
        set_state("python", "certified")
        set_state("go", "learning")
        set_state("rust", "not_interested")
        set_state("tableau", "learning")

        result = skills_by_state()
        assert result == {
            "learning": {"go", "tableau"},
            "certified": {"python"},
            "not_interested": {"rust"},
        }


class TestCorruptFile:
    def test_missing_file_returns_empty(self, isolated_goals):
        assert list_goals() == []

    def test_corrupt_json_returns_empty(self, isolated_goals):
        path = isolated_goals / "learning_goals.json"
        path.write_text("not json {{{")
        assert list_goals() == []

    def test_non_dict_root_returns_empty(self, isolated_goals):
        path = isolated_goals / "learning_goals.json"
        path.write_text(json.dumps(["a", "b"]))
        assert list_goals() == []

    def test_invalid_state_filtered(self, isolated_goals):
        path = isolated_goals / "learning_goals.json"
        path.write_text(json.dumps({
            "python": {"state": "bogus"},  # filtered out
            "go": {"state": "learning"},   # kept
        }))
        result = list_goals()
        assert [s for s, _ in result] == ["go"]

    def test_non_dict_entry_skipped(self, isolated_goals):
        path = isolated_goals / "learning_goals.json"
        path.write_text(json.dumps({
            "python": "not a dict",
            "go": {"state": "learning"},
        }))
        result = list_goals()
        assert [s for s, _ in result] == ["go"]


class TestConstants:
    def test_valid_states(self):
        assert VALID_STATES == {"learning", "certified", "not_interested"}
