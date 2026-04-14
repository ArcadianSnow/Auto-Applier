"""Tests for the neutral-fallback behavior in form_filler.

When Ollama returns empty text on a required free-text field, the
form would dead-lock at the Continue button because Dice/LinkedIn/
Indeed refuse to advance with empty required fields. The neutral
fallback provides a safe placeholder — but ONLY for free-text; it
must NEVER fabricate answers for yes/no, number, select, or date
fields where a wrong answer could disqualify the candidate.
"""
from unittest.mock import MagicMock

import pytest

from auto_applier.browser.form_filler import FormFiller
from auto_applier.browser.selector_utils import FormField


def _filler():
    return FormFiller(router=MagicMock(), personal_info={})


def _field(label: str, field_type: str = "text", options=None) -> FormField:
    return FormField(
        label=label,
        element=MagicMock(),
        field_type=field_type,
        options=list(options or []),
    )


class TestNeutralFallbackFreeText:
    def test_textarea_always_gets_fallback(self):
        """Textareas are almost always free-text by UI convention."""
        f = _filler()
        result = f._neutral_fallback(_field("Any random question?", "textarea"))
        assert "discuss" in result.lower() or "interview" in result.lower()
        assert result != ""

    def test_textarea_fallback_regardless_of_label(self):
        """Even without open-ended keywords, textarea gets fallback."""
        f = _filler()
        result = f._neutral_fallback(_field("Comments", "textarea"))
        assert result != ""

    @pytest.mark.parametrize("label", [
        "Tell us about yourself",
        "Describe your experience with Python",
        "Why do you want this role?",
        "Why are you leaving your current job?",
        "What interests you about our company?",
        "What makes you a good fit?",
        "What attracted you to this position?",
        "Anything else you'd like us to know?",
        "Additional information",
        "Please share your thoughts",
        "Elaborate on your leadership style",
        "Explain a time you disagreed with a decision",
        "Any questions for us?",
    ])
    def test_open_ended_text_gets_fallback(self, label):
        f = _filler()
        result = f._neutral_fallback(_field(label, "text"))
        assert result != "", f"Open-ended label '{label}' should get fallback"


class TestNeutralFallbackRefusesFabrication:
    """These must return empty — fabricating would put wrong data on the app."""

    def test_number_field_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(
            _field("Years of Python experience", "number"),
        )
        assert result == ""

    def test_select_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(
            _field("Highest education", "select", options=["BA", "MA", "PhD"]),
        )
        assert result == ""

    def test_radio_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(
            _field("Are you authorized?", "radio"),
        )
        assert result == ""

    def test_checkbox_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(_field("I agree", "checkbox"))
        assert result == ""

    def test_date_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(_field("Start date", "date"))
        assert result == ""

    def test_file_no_fallback(self):
        f = _filler()
        result = f._neutral_fallback(_field("Upload resume", "file"))
        assert result == ""


class TestNeutralFallbackShortTextFields:
    """Short text fields that aren't open-ended questions should NOT
    get a neutral fallback — they usually want specific factual data
    (name, years, address, etc.) that a placeholder would corrupt."""

    @pytest.mark.parametrize("label", [
        "Phone number",
        "Zip code",
        "LinkedIn URL",
        "Years of experience",
        "Expected salary",
        "Current company",
        "When can you start?",
        "How did you hear about us?",
    ])
    def test_factual_text_fields_get_no_fallback(self, label):
        f = _filler()
        result = f._neutral_fallback(_field(label, "text"))
        assert result == "", (
            f"Factual text field '{label}' should NOT get fabricated answer"
        )


class TestFallbackContent:
    """The fallback text itself should be truthful and professional."""

    def test_fallback_is_non_empty(self):
        f = _filler()
        result = f._neutral_fallback(_field("Anything else?", "textarea"))
        assert result.strip()

    def test_fallback_is_professional_tone(self):
        """Should sound like something a candidate would actually write."""
        f = _filler()
        result = f._neutral_fallback(_field("Anything else?", "textarea"))
        # No enthusiasm words that feel scammy / bot-like
        low = result.lower()
        assert "excited" not in low  # avoids cliché enthusiasm
        assert "!!" not in result
        # Is a complete sentence (ends with punctuation)
        assert result.strip()[-1] in ".!?"

    def test_fallback_does_not_claim_facts(self):
        """Fallback must not claim specific experience / numbers."""
        f = _filler()
        result = f._neutral_fallback(_field("Anything else?", "textarea"))
        low = result.lower()
        # No specific years / numbers / employer names
        import re
        assert not re.search(r"\b\d+\s+years?\b", low)
        assert "fortune" not in low
        assert "built" not in low  # avoids claiming concrete work
