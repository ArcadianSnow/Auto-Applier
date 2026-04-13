"""Tests for form_filler.py pure-logic helpers not covered by test_form_filler_smart.py."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from auto_applier.browser.form_filler import (
    FormFiller,
    _normalize_phone_for_field,
    PERSONAL_INFO_KEYS,
    HONEYPOT_KEYWORDS,
    SOURCE_QUESTION_KEYWORDS,
    SOURCE_QUESTION_REGEX,
)


# ------------------------------------------------------------------
# _normalize_phone_for_field
# ------------------------------------------------------------------

class TestNormalizePhone:
    def test_strips_country_code(self):
        assert _normalize_phone_for_field("+1 206 555 0100") == "2065550100"

    def test_11_digit_us(self):
        assert _normalize_phone_for_field("12065550100") == "2065550100"

    def test_10_digit_passthrough(self):
        assert _normalize_phone_for_field("2065550100") == "2065550100"

    def test_formatted_phone(self):
        assert _normalize_phone_for_field("(206) 555-0100") == "2065550100"

    def test_empty(self):
        assert _normalize_phone_for_field("") == ""

    def test_non_us_preserved(self):
        # 12-digit non-US number, won't match 11-digit US pattern
        result = _normalize_phone_for_field("+44 7911 123456")
        assert result == "447911123456"


# ------------------------------------------------------------------
# _categorize_field
# ------------------------------------------------------------------

class TestCategorizeField:
    def test_certification(self):
        assert FormFiller._categorize_field("Do you have AWS certification?") == "certification"

    def test_experience(self):
        assert FormFiller._categorize_field("How many years of Python experience?") == "experience"

    def test_skill(self):
        assert FormFiller._categorize_field("Rate your proficiency in SQL") == "skill"

    def test_other(self):
        assert FormFiller._categorize_field("When can you start?") == "other"

    def test_case_insensitive(self):
        assert FormFiller._categorize_field("YOUR SKILLS AND KNOWLEDGE") == "skill"


# ------------------------------------------------------------------
# _find_best_option
# ------------------------------------------------------------------

class TestFindBestOption:
    def test_exact_match(self):
        assert FormFiller._find_best_option("Yes", ["No", "Yes"]) == "Yes"

    def test_case_insensitive(self):
        assert FormFiller._find_best_option("yes", ["No", "Yes"]) == "Yes"

    def test_substring_match(self):
        assert FormFiller._find_best_option(
            "United States", ["United States of America", "Canada"]
        ) == "United States of America"

    def test_reverse_substring(self):
        assert FormFiller._find_best_option(
            "United States of America", ["United States", "Canada"]
        ) == "United States"

    def test_fuzzy_match(self):
        result = FormFiller._find_best_option("Pyhton", ["Python", "Java", "Go"])
        assert result == "Python"

    def test_no_match(self):
        assert FormFiller._find_best_option("xyz", ["Alpha", "Beta"]) == ""

    def test_empty_options(self):
        assert FormFiller._find_best_option("anything", []) == ""


# ------------------------------------------------------------------
# _coerce_iso_date
# ------------------------------------------------------------------

class TestCoerceIsoDate:
    def test_iso_format(self):
        assert FormFiller._coerce_iso_date("2026-05-01") == "2026-05-01"

    def test_us_format(self):
        assert FormFiller._coerce_iso_date("05/01/2026") == "2026-05-01"

    def test_us_dash_format(self):
        assert FormFiller._coerce_iso_date("05-01-2026") == "2026-05-01"

    def test_long_month(self):
        assert FormFiller._coerce_iso_date("May 01, 2026") == "2026-05-01"

    def test_short_month(self):
        assert FormFiller._coerce_iso_date("May 1, 2026") == "2026-05-01"

    def test_empty_returns_fallback(self):
        result = FormFiller._coerce_iso_date("")
        expected = (date.today() + timedelta(days=14)).isoformat()
        assert result == expected

    def test_garbage_returns_fallback(self):
        result = FormFiller._coerce_iso_date("not a date at all")
        expected = (date.today() + timedelta(days=14)).isoformat()
        assert result == expected


# ------------------------------------------------------------------
# _load_answers (3 shapes)
# ------------------------------------------------------------------

class TestLoadAnswers:
    def test_flat_dict_shape(self, tmp_path, monkeypatch):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps({
            "Are you authorized?": "Yes",
            "Years of experience": "5",
        }))
        monkeypatch.setattr("auto_applier.browser.form_filler.ANSWERS_FILE", answers_file)
        result = FormFiller._load_answers()
        assert len(result) == 2
        assert result[0]["question"] == "Are you authorized?"
        assert result[0]["answer"] == "Yes"
        assert result[0]["aliases"] == []

    def test_list_shape(self, tmp_path, monkeypatch):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps([
            {"question": "Q1", "answer": "A1", "aliases": ["alias1"]},
            {"question": "Q2", "answer": "A2"},
        ]))
        monkeypatch.setattr("auto_applier.browser.form_filler.ANSWERS_FILE", answers_file)
        result = FormFiller._load_answers()
        assert len(result) == 2
        assert result[0]["aliases"] == ["alias1"]
        assert result[1]["aliases"] == []

    def test_wrapped_list_shape(self, tmp_path, monkeypatch):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps({
            "questions": [{"question": "Q1", "answer": "A1"}]
        }))
        monkeypatch.setattr("auto_applier.browser.form_filler.ANSWERS_FILE", answers_file)
        result = FormFiller._load_answers()
        assert len(result) == 1

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "auto_applier.browser.form_filler.ANSWERS_FILE",
            tmp_path / "nonexistent.json",
        )
        assert FormFiller._load_answers() == []

    def test_corrupt_json(self, tmp_path, monkeypatch):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text("not json {{{")
        monkeypatch.setattr("auto_applier.browser.form_filler.ANSWERS_FILE", answers_file)
        assert FormFiller._load_answers() == []


# ------------------------------------------------------------------
# _match_answers (3-pass matching)
# ------------------------------------------------------------------

class TestMatchAnswers:
    @pytest.fixture
    def filler(self, tmp_path, monkeypatch):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps([
            {"question": "Are you authorized to work?", "answer": "Yes",
             "aliases": ["work authorization"]},
            {"question": "Do you have a clearance?", "answer": "No"},
            {"question": "Expected salary", "answer": "90000"},
        ]))
        monkeypatch.setattr("auto_applier.browser.form_filler.ANSWERS_FILE", answers_file)
        from unittest.mock import MagicMock
        return FormFiller(
            router=MagicMock(), personal_info={},
            resume_text="", job_description="",
        )

    def test_exact_match(self, filler):
        assert filler._match_answers("Are you authorized to work?") == "Yes"

    def test_exact_case_insensitive(self, filler):
        assert filler._match_answers("are you authorized to work?") == "Yes"

    def test_alias_match(self, filler):
        assert filler._match_answers("work authorization") == "Yes"

    def test_substring_match(self, filler):
        assert filler._match_answers(
            "Are you authorized to work in the United States?"
        ) == "Yes"

    def test_fuzzy_match(self, filler):
        result = filler._match_answers("What is your expected sallary?")
        assert result == "90000"

    def test_no_match(self, filler):
        assert filler._match_answers("Completely unrelated question xyz") == ""


# ------------------------------------------------------------------
# _match_personal_info
# ------------------------------------------------------------------

class TestMatchPersonalInfo:
    def test_first_name(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={"first_name": "John"},
        )
        assert filler._match_personal_info("first name") == "John"

    def test_phone_normalized(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={"phone": "+1 206-555-0100"},
        )
        result = filler._match_personal_info("phone number")
        assert result == "2065550100"

    def test_no_match(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={"email": "test@test.com"},
        )
        assert filler._match_personal_info("favorite color") == ""

    def test_empty_value_skipped(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={"first_name": ""},
        )
        assert filler._match_personal_info("first name") == ""


# ------------------------------------------------------------------
# _match_contextual
# ------------------------------------------------------------------

class TestMatchContextual:
    def test_source_attribution(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={},
            platform_display_name="Indeed",
        )
        from auto_applier.browser.selector_utils import FormField
        field = MagicMock(spec=FormField)
        assert filler._match_contextual("how did you hear about this position?", field) == "Indeed"

    def test_source_regex_fallback(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={},
            platform_display_name="Dice",
        )
        field = MagicMock()
        assert filler._match_contextual("how did you first discover this role?", field) == "Dice"

    def test_previously_worked_no(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={},
            resume_text="Worked at Google for 3 years",
            company_name="Microsoft",
        )
        field = MagicMock()
        assert filler._match_contextual("have you previously worked for us?", field) == "No"

    def test_previously_worked_yes(self):
        filler = FormFiller(
            router=MagicMock(), personal_info={},
            resume_text="Worked at Microsoft for 3 years",
            company_name="Microsoft",
        )
        field = MagicMock()
        assert filler._match_contextual("have you previously worked for us?", field) == "Yes"

    def test_start_date(self):
        filler = FormFiller(router=MagicMock(), personal_info={})
        field = MagicMock()
        result = filler._match_contextual("what is your earliest start date?", field)
        expected = (date.today() + timedelta(days=14)).isoformat()
        assert result == expected

    def test_no_contextual_match(self):
        filler = FormFiller(router=MagicMock(), personal_info={})
        field = MagicMock()
        assert filler._match_contextual("random question", field) == ""


# ------------------------------------------------------------------
# Source question regex
# ------------------------------------------------------------------

class TestSourceQuestionRegex:
    @pytest.mark.parametrize("text", [
        "how did you hear about this job?",
        "where did you find this position?",
        "how did you first discover this role?",
        "how did you eventually find this opportunity?",
        "where did you come across this listing?",
    ])
    def test_matches(self, text):
        assert SOURCE_QUESTION_REGEX.search(text)

    @pytest.mark.parametrize("text", [
        "what is your favorite color?",
        "how many years of experience?",
        "where did you work before?",
    ])
    def test_no_match(self, text):
        assert not SOURCE_QUESTION_REGEX.search(text)


# ------------------------------------------------------------------
# Honeypot detection
# ------------------------------------------------------------------

class TestHoneypotKeywords:
    def test_all_keywords_are_lowercase(self):
        for kw in HONEYPOT_KEYWORDS:
            assert kw == kw.lower()

    def test_detection(self):
        label = "Leave this blank if you're a real person"
        assert any(kw in label.lower() for kw in HONEYPOT_KEYWORDS)

    def test_normal_field_not_detected(self):
        label = "What is your name?"
        assert not any(kw in label.lower() for kw in HONEYPOT_KEYWORDS)
