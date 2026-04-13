"""Tests for llm/prompts.py — template structure and placeholder validation."""

import pytest

from auto_applier.llm import prompts


# All prompt templates that should exist
ALL_TEMPLATES = [
    prompts.FORM_FILL,
    prompts.JOB_SCORE,
    prompts.SKILL_EXTRACT_RESUME,
    prompts.SKILL_EXTRACT_JD,
    prompts.RESUME_BULLET,
    prompts.SCORE_DIMENSIONS,
    prompts.RESUME_SELECT,
    prompts.CLASSIFY_JOB_ARCHETYPE,
    prompts.COMPANY_RESEARCH,
    prompts.GHOST_JOB_CHECK,
    prompts.TAILOR_RESUME,
    prompts.FOLLOWUP_EMAIL,
    prompts.OUTREACH_MESSAGE,
    prompts.STAR_STORIES,
    prompts.COVER_LETTER,
]


class TestPromptTemplateStructure:
    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_has_nonempty_system_prompt(self, template):
        assert template.system.strip(), f"{template} has empty system prompt"

    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_has_nonempty_template(self, template):
        assert template.template.strip(), f"{template} has empty template"

    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_system_is_frozen(self, template):
        with pytest.raises(AttributeError):
            template.system = "changed"


class TestJSONPrompts:
    """JSON-output prompts must demand JSON-only output."""

    JSON_TEMPLATES = [
        prompts.JOB_SCORE,
        prompts.SKILL_EXTRACT_RESUME,
        prompts.SKILL_EXTRACT_JD,
        prompts.RESUME_BULLET,
        prompts.SCORE_DIMENSIONS,
        prompts.RESUME_SELECT,
        prompts.CLASSIFY_JOB_ARCHETYPE,
        prompts.COMPANY_RESEARCH,
        prompts.GHOST_JOB_CHECK,
        prompts.TAILOR_RESUME,
        prompts.STAR_STORIES,
    ]

    @pytest.mark.parametrize("template", JSON_TEMPLATES)
    def test_demands_json_only(self, template):
        sys = template.system.lower()
        assert "json" in sys, f"JSON template missing 'json' in system prompt"

    @pytest.mark.parametrize("template", JSON_TEMPLATES)
    def test_forbids_preamble_or_fences(self, template):
        sys = template.system.lower()
        assert "no other text" in sys or "no preamble" in sys, (
            f"JSON template should forbid preamble/extra text"
        )


class TestFormatMethod:
    def test_form_fill_format(self):
        result = prompts.FORM_FILL.format(
            resume_text="my resume",
            job_description="the jd",
            company_name="Acme",
            question="Are you authorized?",
        )
        assert "my resume" in result
        assert "Acme" in result
        assert "Are you authorized?" in result

    def test_job_score_format(self):
        result = prompts.JOB_SCORE.format(
            resume_text="resume here",
            job_description="jd here",
        )
        assert "resume here" in result

    def test_cover_letter_format(self):
        result = prompts.COVER_LETTER.format(
            resume_text="resume",
            job_description="jd",
            company_name="TestCo",
            job_title="Engineer",
        )
        assert "TestCo" in result
        assert "Engineer" in result

    def test_missing_placeholder_raises(self):
        with pytest.raises(KeyError):
            prompts.FORM_FILL.format(resume_text="x")

    def test_ghost_check_format(self):
        result = prompts.GHOST_JOB_CHECK.format(
            company_name="BigCorp",
            job_title="SWE",
            job_description="Build stuff",
        )
        assert "BigCorp" in result
        assert "SWE" in result

    def test_score_dimensions_format(self):
        result = prompts.SCORE_DIMENSIONS.format(
            resume_label="Data Analyst",
            resume_text="resume",
            job_description="jd",
        )
        assert "Data Analyst" in result
