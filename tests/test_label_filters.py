"""Smoke test for the consolidated label-filter taxonomy.

The actual regression coverage is the 1150+ existing tests that
exercise ``_is_phantom_label``, ``_is_skill_shaped``, and
``should_skip_unanswered`` indirectly. This file just confirms the
new public surface in
:mod:`auto_applier.browser.label_filters` exists and classifies a
representative example from each bucket the way every caller expects.
"""

from auto_applier.browser.label_filters import (
    is_non_skill_label,
    is_phantom_label,
    is_prompt_leak,
)


def test_phantom_label_rejects_page_chrome():
    assert is_phantom_label("Voluntary Self Identification Questions") is True
    assert is_phantom_label("Drag and drop your resume here") is True
    assert is_phantom_label("") is True


def test_phantom_label_accepts_real_question():
    assert is_phantom_label("Are you 18 years of age or older?") is False


def test_non_skill_label_rejects_compliance():
    assert is_non_skill_label("Are you authorized to work in the US?") is True
    assert is_non_skill_label("Have you ever been convicted of a felony?") is True
    assert is_non_skill_label("Desired salary") is True


def test_non_skill_label_rejects_personal_info():
    assert is_non_skill_label("Street Address") is True
    assert is_non_skill_label("Zip code") is True


def test_non_skill_label_accepts_skill_question():
    assert is_non_skill_label("How many years of Python experience do you have?") is False
    assert is_non_skill_label("Describe your SQL background") is False


def test_prompt_leak_detects_resume_marker():
    leaked = "Question: ...\nResume:\nJohn Doe — Data Engineer..."
    assert is_prompt_leak(leaked) is True


def test_prompt_leak_ignores_normal_label():
    assert is_prompt_leak("Are you 18 years of age or older?") is False
