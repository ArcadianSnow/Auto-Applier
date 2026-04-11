"""Regression tests for answers.json format tolerance.

Three historical shapes existed in the codebase; the loader must
canonicalize all three and fall through gracefully on garbage.
"""
import json
from unittest.mock import patch

import pytest

from auto_applier.browser.form_filler import FormFiller


@pytest.fixture
def tmp_answers(tmp_path, monkeypatch):
    f = tmp_path / "answers.json"
    from auto_applier.browser import form_filler as ff
    monkeypatch.setattr(ff, "ANSWERS_FILE", f)
    return f


class TestFlatDictFormat:
    def test_loads_flat_dict(self, tmp_answers):
        tmp_answers.write_text(json.dumps({
            "Are you authorized?": "Yes",
            "Salary expectation": "120000",
        }))
        entries = FormFiller._load_answers()
        assert len(entries) == 2
        questions = {e["question"] for e in entries}
        assert "Are you authorized?" in questions
        assert "Salary expectation" in questions

    def test_flat_dict_answers_preserved(self, tmp_answers):
        tmp_answers.write_text(json.dumps({"Q": "A"}))
        entries = FormFiller._load_answers()
        assert entries[0]["answer"] == "A"


class TestListOfEntries:
    def test_loads_list_format(self, tmp_answers):
        tmp_answers.write_text(json.dumps([
            {"question": "Q1", "answer": "A1"},
            {"question": "Q2", "answer": "A2", "aliases": ["alt q2"]},
        ]))
        entries = FormFiller._load_answers()
        assert len(entries) == 2
        assert entries[0]["question"] == "Q1"
        assert entries[1]["aliases"] == ["alt q2"]


class TestWrappedQuestions:
    def test_loads_wrapped_format(self, tmp_answers):
        tmp_answers.write_text(json.dumps({
            "questions": [
                {"question": "Q1", "answer": "A1"},
                {"question": "Q2", "answer": "A2"},
            ]
        }))
        entries = FormFiller._load_answers()
        assert len(entries) == 2


class TestEdgeCases:
    def test_missing_file_returns_empty(self, tmp_answers):
        assert FormFiller._load_answers() == []

    def test_invalid_json_returns_empty(self, tmp_answers):
        tmp_answers.write_text("{ not json")
        assert FormFiller._load_answers() == []

    def test_garbage_shape_returns_empty(self, tmp_answers):
        tmp_answers.write_text(json.dumps(42))
        assert FormFiller._load_answers() == []

    def test_list_of_non_dicts_filtered(self, tmp_answers):
        tmp_answers.write_text(json.dumps([
            "not a dict",
            42,
            {"question": "Q", "answer": "A"},
        ]))
        entries = FormFiller._load_answers()
        assert len(entries) == 1
        assert entries[0]["question"] == "Q"

    def test_entries_without_question_skipped(self, tmp_answers):
        tmp_answers.write_text(json.dumps([
            {"answer": "orphan"},
            {"question": "", "answer": "blank"},
            {"question": "Real", "answer": "Yes"},
        ]))
        entries = FormFiller._load_answers()
        assert len(entries) == 1
        assert entries[0]["question"] == "Real"


class TestMatchAnswers:
    def _filler_with_answers(self, tmp_answers, answers_dict):
        tmp_answers.write_text(json.dumps(answers_dict))
        from unittest.mock import MagicMock
        return FormFiller(
            router=MagicMock(),
            personal_info={},
        )

    def test_exact_match(self, tmp_answers):
        f = self._filler_with_answers(tmp_answers, {
            "Are you authorized to work?": "Yes",
        })
        assert f._match_answers("Are you authorized to work?") == "Yes"

    def test_case_insensitive(self, tmp_answers):
        f = self._filler_with_answers(tmp_answers, {
            "Are you authorized to work?": "Yes",
        })
        assert f._match_answers("ARE YOU AUTHORIZED TO WORK?") == "Yes"

    def test_substring_match(self, tmp_answers):
        """Real-world Indeed forms pad the actual question with extra text."""
        f = self._filler_with_answers(tmp_answers, {
            "Salary expectation": "120000",
        })
        # Label is the saved question surrounded by extra text
        assert f._match_answers("What is your salary expectation for this role?") == "120000"

    def test_fuzzy_match(self, tmp_answers):
        f = self._filler_with_answers(tmp_answers, {
            "How many years of experience with Python": "5",
        })
        # Slightly different wording should still fuzzy-match
        result = f._match_answers("How many years experience with Python?")
        assert result == "5"

    def test_no_match_returns_empty(self, tmp_answers):
        f = self._filler_with_answers(tmp_answers, {
            "Salary": "120000",
        })
        assert f._match_answers("Do you have a driver's license?") == ""
