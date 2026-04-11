"""Regression tests for AnswersStep._load_unanswered.

The wizard startup crashed with 'unhashable type: dict' because the
form filler's _record_unanswered writes list-of-dict entries but
AnswersStep._load_unanswered was returning the raw list to the
caller, which then tried to use each dict as a dict key.

The loader must ALWAYS return a list of strings regardless of what
shape the file is on disk — plain strings, list of entry dicts, or
a flat {question: count} dict.
"""
import json
from unittest.mock import patch

import pytest

from auto_applier.gui.steps.answers import AnswersStep


@pytest.fixture
def tmp_unanswered(tmp_path, monkeypatch):
    path = tmp_path / "unanswered.json"
    import auto_applier.gui.steps.answers as mod
    monkeypatch.setattr(mod, "UNANSWERED_FILE", path)
    return path


class TestLoadUnanswered:
    def test_missing_file_returns_empty(self, tmp_unanswered):
        assert AnswersStep._load_unanswered() == []

    def test_malformed_json_returns_empty(self, tmp_unanswered):
        tmp_unanswered.write_text("{ not json")
        assert AnswersStep._load_unanswered() == []

    def test_list_of_strings(self, tmp_unanswered):
        tmp_unanswered.write_text(json.dumps(["Q1", "Q2"]))
        assert AnswersStep._load_unanswered() == ["Q1", "Q2"]

    def test_list_of_entry_dicts(self, tmp_unanswered):
        """The real-world crash: form_filler writes this format."""
        tmp_unanswered.write_text(json.dumps([
            {"question": "Zip code", "encountered": 3},
            {"question": "Street address", "encountered": 1},
        ]))
        result = AnswersStep._load_unanswered()
        assert result == ["Zip code", "Street address"]
        # All elements must be strings (not dicts) so they can be
        # used as dict keys or Tk widget text.
        assert all(isinstance(q, str) for q in result)

    def test_flat_dict_format(self, tmp_unanswered):
        tmp_unanswered.write_text(json.dumps({
            "Q1": 3,
            "Q2": 1,
        }))
        result = AnswersStep._load_unanswered()
        assert set(result) == {"Q1", "Q2"}
        assert all(isinstance(q, str) for q in result)

    def test_mixed_entries_handled(self, tmp_unanswered):
        """List containing both plain strings and dicts."""
        tmp_unanswered.write_text(json.dumps([
            "Plain string Q",
            {"question": "Dict Q", "encountered": 1},
            42,  # garbage, should be ignored
            {"encountered": 1},  # missing 'question', should be ignored
        ]))
        result = AnswersStep._load_unanswered()
        assert "Plain string Q" in result
        assert "Dict Q" in result
        assert len(result) == 2

    def test_garbage_top_level_returns_empty(self, tmp_unanswered):
        tmp_unanswered.write_text(json.dumps(42))
        assert AnswersStep._load_unanswered() == []
