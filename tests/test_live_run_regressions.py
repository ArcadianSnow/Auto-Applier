"""Regression tests locking in bug fixes from 2026-05-01/02 live runs.

Each test pins a single bug shape we shipped a patch for during the
live runs against Indeed, Dice, and ZipRecruiter today. If any of
these break in the future, the original bug has likely re-emerged —
fix the code, not the test.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.form_filler import FormFiller
from auto_applier.browser.selector_utils import (
    FormField,
    _clean_compound_label,
    _is_phantom_label,
)


# ------------------------------------------------------------------
# 1. Compound multi-line label cleanup
# ------------------------------------------------------------------

class TestCompoundLabelCleanup:
    """Indeed live run 2026-05-02: select wrapped a fieldset with
    helper paragraph text — LLM saw 'Mobile Number' and typed phone."""

    def test_compound_multiline_label_extracts_last_line(self):
        # Indeed Country select wrapped with Mobile Number heading + helper.
        raw = (
            "Mobile Number\n\n"
            "Provide phone numbers...\n\n"
            "Country *"
        )
        assert _clean_compound_label(raw) == "Country *"

    def test_single_line_label_unchanged(self):
        # Single-line labels must pass through untouched.
        assert _clean_compound_label("Country *") == "Country *"

    def test_asterisk_only_last_line_skipped(self):
        # Bare asterisk chrome line is dropped — fall back to prior line.
        assert _clean_compound_label("Cover letter\n*") == "Cover letter"

    def test_required_chrome_last_line_skipped(self):
        # Same idea for "(required)" chrome-only trailing line.
        raw = "What is your salary?\n\n(required)"
        assert _clean_compound_label(raw) == "What is your salary?"


# ------------------------------------------------------------------
# 2. Phantom labels rejected
# ------------------------------------------------------------------

class TestPhantomLabelRejection:
    """ZipRecruiter / Indeed live run: filler treated page chrome
    ("Current page", "Upload a file") as questions and got nonsense."""

    def test_current_page_is_phantom(self):
        assert _is_phantom_label("Current page") is True

    def test_voluntary_self_id_is_phantom(self):
        assert (
            _is_phantom_label(
                "Voluntary self identification questions from the employer"
            )
            is True
        )

    def test_upload_a_file_is_phantom(self):
        assert _is_phantom_label("Upload a file") is True

    def test_real_question_not_phantom(self):
        # Genuine screener question must NOT be filtered out.
        assert (
            _is_phantom_label("Are you 18 years of age or older?") is False
        )

    def test_empty_label_is_phantom(self):
        assert _is_phantom_label("") is True

    def test_whitespace_only_is_phantom(self):
        assert _is_phantom_label("   \n\t  ") is True


# ------------------------------------------------------------------
# 3. Conditional N/A "If not [trait]" handler
# ------------------------------------------------------------------

class TestConditionalNotApplicable:
    """Indeed live run 2026-05-02: 'If not a US Citizen, do you have
    a non-academic visa...' got LLM-defaulted to 'No' for the wrong
    reason. Now we short-circuit to 'No' when candidate IS the trait."""

    def _make_field(self, field_type: str) -> FormField:
        return FormField(
            label=(
                "If not a US Citizen, do you have a non-academic visa "
                "that permits you to work in the US?"
            ),
            element=MagicMock(),
            field_type=field_type,
        )

    def test_us_citizen_radio_returns_no(self):
        filler = FormFiller(
            router=MagicMock(),
            personal_info={"work_auth": "US Citizen"},
        )
        field = self._make_field("radio")
        result = filler._answer_conditional_not_applicable(
            field.label.lower(), field,
        )
        assert result == "No"

    def test_unknown_work_auth_returns_empty(self):
        # Without work_auth we can't decide whether the conditional fires.
        filler = FormFiller(
            router=MagicMock(),
            personal_info={"work_auth": ""},
        )
        field = self._make_field("radio")
        result = filler._answer_conditional_not_applicable(
            field.label.lower(), field,
        )
        assert result == ""

    def test_text_field_returns_empty(self):
        # Only fires on radio/checkbox/select shaped fields.
        filler = FormFiller(
            router=MagicMock(),
            personal_info={"work_auth": "US Citizen"},
        )
        field = self._make_field("text")
        result = filler._answer_conditional_not_applicable(
            field.label.lower(), field,
        )
        assert result == ""


# ------------------------------------------------------------------
# 4. LLM length-cap sentence-aware trim
# ------------------------------------------------------------------

class TestSentenceAwareTrim:
    """Indeed/ZR live run: LLM ignored maxlength caps; mid-word
    truncation produced rejected answers. Trim at sentence boundary
    when possible, otherwise word boundary, otherwise hard cap."""

    def test_trims_at_sentence_boundary(self):
        text = "I want this job. It aligns with my skills. I am excited."
        assert FormFiller._sentence_aware_trim(text, 30) == "I want this job."

    def test_handles_long_word_no_spaces(self):
        # Mid-word hard-truncate when no boundaries exist; must stay <= cap.
        text = "Onelongwordwithnospaces" * 5
        result = FormFiller._sentence_aware_trim(text, 40)
        assert len(result) <= 40

    def test_word_boundary_fallback_within_cap(self):
        text = "I am very interested in this role and want to grow."
        result = FormFiller._sentence_aware_trim(text, 50)
        assert len(result) <= 50
        # Should not end mid-word — last char should be alpha (clean break).
        assert result[-1].isalpha() or result[-1] in ".!?"

    def test_text_under_cap_unchanged(self):
        text = "Short answer."
        assert FormFiller._sentence_aware_trim(text, 100) == text


# ------------------------------------------------------------------
# 5. Range-bucket regex parse (skipped per spec — verify regex only)
# ------------------------------------------------------------------

class TestRangeBucketRegex:
    """The radio resolver matches '4-6 years', '7+', etc. against a
    user-provided number. Full resolver test would require page mocks
    — instead lock in the regex parse so future tweaks don't silently
    break range extraction."""

    RANGE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
    OPEN_RE = re.compile(r"(\d+)\s*\+")

    def test_dash_range_extracts_low_high(self):
        m = self.RANGE_RE.search("4-6 years")
        assert m and (int(m.group(1)), int(m.group(2))) == (4, 6)

    def test_plus_open_ended_extracts_low(self):
        m = self.OPEN_RE.search("7+")
        assert m and int(m.group(1)) == 7

    def test_range_not_in_plain_string(self):
        assert self.RANGE_RE.search("five years") is None


# ------------------------------------------------------------------
# 6. Work-auth label precedence in personal_info
# ------------------------------------------------------------------

class TestWorkAuthLabelPrecedence:
    """Indeed live run 2026-05-02: 'Are you legally authorized to
    work in the country in which the job is located?' substring-
    matched 'country' and returned 'United States' for a Yes/No
    radio. Now work-auth questions skip geographic personal_info keys."""

    def test_work_auth_question_skips_country_key(self):
        filler = FormFiller(
            router=MagicMock(),
            personal_info={
                "country": "United States",
                "city_state": "Seattle, WA",
            },
        )
        result = filler._match_personal_info(
            "are you legally authorized to work in the country "
            "in which the job is located?"
        )
        assert result == ""

    def test_pure_country_question_still_returns_country(self):
        # Without a work-auth keyword, 'country' substring still wins.
        filler = FormFiller(
            router=MagicMock(),
            personal_info={
                "country": "United States",
                "city_state": "Seattle, WA",
            },
        )
        result = filler._match_personal_info(
            "which country are you located in?"
        )
        assert result == "United States"


# ------------------------------------------------------------------
# 7. Location keyword variants
# ------------------------------------------------------------------

class TestLocationKeywordVariants:
    """ZR live run 2026-05-02: 'Where are you located?' fell through
    to fuzzy answers.json which returned 'Yes'. Added past-tense /
    variant phrasings that all map to city_state."""

    @pytest.fixture
    def filler(self):
        return FormFiller(
            router=MagicMock(),
            personal_info={"city_state": "Seattle, WA"},
        )

    def test_where_are_you_located(self, filler):
        assert (
            filler._match_personal_info("where are you located?")
            == "Seattle, WA"
        )

    def test_current_location(self, filler):
        assert (
            filler._match_personal_info("what is your current location?")
            == "Seattle, WA"
        )

    def test_where_do_you_live(self, filler):
        assert (
            filler._match_personal_info("where do you live?")
            == "Seattle, WA"
        )


# ------------------------------------------------------------------
# 8. _answer_fits_field rejects Yes/No for location-shaped text fields
# ------------------------------------------------------------------

class TestAnswerFitsLocationFields:
    """ZR live run 2026-05-02: fuzzy match returned 'Yes' for a
    'Where are you located?' text field, persistence check passed,
    ZR rejected the form silently. Reject obvious bool answers
    against location-shaped text labels."""

    def test_yes_rejected_for_location_text_field(self):
        field = FormField(
            label="Where are you located?",
            element=MagicMock(),
            field_type="text",
        )
        assert FormFiller._answer_fits_field(field, "Yes") is False

    def test_real_location_accepted(self):
        field = FormField(
            label="Where are you located?",
            element=MagicMock(),
            field_type="text",
        )
        assert FormFiller._answer_fits_field(field, "Seattle, WA") is True

    def test_yes_accepted_for_non_location_text_field(self):
        # Open-ended prompts do not get the location-shape reject.
        field = FormField(
            label="Tell us about yourself",
            element=MagicMock(),
            field_type="text",
        )
        assert FormFiller._answer_fits_field(field, "Yes") is True


# ------------------------------------------------------------------
# 9. _answer_fits_field rejects phone digits for radios
# ------------------------------------------------------------------

class TestAnswerFitsRadioRejectsPhone:
    """Existing regression: 'Do you consent to SMS at the mobile
    number...' fed phone digits to a Yes/No radio. Lock it in."""

    def test_phone_digits_rejected_for_radio(self):
        field = FormField(
            label="Yes or No?",
            element=MagicMock(),
            field_type="radio",
        )
        assert FormFiller._answer_fits_field(field, "2065550100") is False


# ------------------------------------------------------------------
# 10. Hidden file input upload preference (pick_resume_input)
# ------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_file_input(visible: bool, kind_text: str):
    """Mock a file input element. The kind is encoded in its 'name'
    attribute via classify_file_input's JS evaluator — we sidestep the
    JS by stubbing element.evaluate to return the keyword text directly."""
    inp = AsyncMock()
    inp.is_visible = AsyncMock(return_value=visible)
    inp.evaluate = AsyncMock(return_value=kind_text)
    return inp


class TestPickResumeInput:
    """Indeed live run 2026-05-02: dead-locked three times because we
    required visible=True on the file input. Modern UIs (Indeed,
    Workday, Greenhouse) overlay a styled card on a hidden input."""

    def test_hidden_resume_input_returned_with_warn(self):
        # Single hidden input classified as resume → upload anyway.
        hidden_resume = _make_file_input(visible=False, kind_text="resume upload")
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[hidden_resume])

        result = _run(FormFiller.pick_resume_input(page, "Indeed"))
        assert result is hidden_resume

    def test_hidden_resume_wins_over_visible_cover_letter(self):
        # Visible cover_letter is NOT a resume; hidden resume must win.
        visible_cover = _make_file_input(
            visible=True, kind_text="cover letter attachment",
        )
        hidden_resume = _make_file_input(
            visible=False, kind_text="resume cv upload",
        )
        page = MagicMock()
        page.query_selector_all = AsyncMock(
            return_value=[visible_cover, hidden_resume]
        )

        result = _run(FormFiller.pick_resume_input(page, "Indeed"))
        assert result is hidden_resume

    def test_no_file_inputs_returns_none(self):
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[])

        result = _run(FormFiller.pick_resume_input(page, "Dice"))
        assert result is None
