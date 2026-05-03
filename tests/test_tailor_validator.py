"""Tests for the tailored-resume fabrication guard.

Per user feedback 2026-05-03: "we can't just lie about certain
skills for the user." LLMs hallucinate even when prompted not to,
so we validate the output deterministically.

Tests cover:

  Layer 1 — skill validation:
    - Direct substring matches pass
    - All-tokens-match (multi-word skills) passes
    - Special-char skills (C++, .NET, C#) pass
    - Fabricated skill is dropped
    - Mostly-fabricated skills triggers REJECT (fall back to base)

  Layer 2 — experience company validation:
    - Company in source passes
    - Corporate suffixes stripped before match
    - Token-level company match works
    - Fabricated company → entry dropped
    - Single fabricated experience entry triggers REJECT (high stakes)

  Edge cases:
    - Empty source → accept (nothing to compare against)
    - Non-string skill → silently skipped
    - Education left untouched (low fabrication risk)
"""
from __future__ import annotations

import pytest

from auto_applier.resume.tailor import TailoredResume
from auto_applier.resume.tailor_validator import (
    ValidationReport,
    _company_supported,
    _skill_supported,
    validate_tailored_resume,
)


# ----------------------------------------------------------------------
# Skill matcher
# ----------------------------------------------------------------------

class TestSkillSupported:
    SOURCE = (
        "Senior Data Engineer at Acme Corp. Built pipelines in "
        "Python, SQL, and Apache Kafka. Used PostgreSQL daily. "
        "Familiar with C++ and .NET. Led a team of 5 engineers."
    ).lower()

    @pytest.mark.parametrize("skill", [
        "Python",
        "python",  # case insensitive
        "SQL",
        "PostgreSQL",
        "Apache Kafka",
        "Kafka",
        "C++",
        ".NET",
    ])
    def test_substring_match(self, skill):
        assert _skill_supported(skill, self.SOURCE) is True

    def test_token_match_for_multi_word(self):
        """Source says 'Apache Kafka' — tailored claims 'Kafka stream
        processing'. The tokens 'kafka' and 'stream' don't all appear,
        so it should fail (kafka does, stream doesn't)."""
        assert _skill_supported(
            "Kafka stream processing", self.SOURCE,
        ) is False

    @pytest.mark.parametrize("skill", [
        "Snowflake",       # not in source
        "AWS Lambda",      # neither token present
        "TensorFlow",
        "Kubernetes orchestration",
    ])
    def test_fabricated_skill_dropped(self, skill):
        assert _skill_supported(skill, self.SOURCE) is False

    def test_short_language_names_kept(self):
        """1-2 letter language names like 'C', 'R', 'Go' are
        legitimately important and shouldn't be dropped just for
        length. Confirm they pass when present."""
        src = "Wrote production R scripts and Go services.".lower()
        assert _skill_supported("R", src) is True
        assert _skill_supported("Go", src) is True

    def test_empty_skill_returns_false(self):
        assert _skill_supported("", self.SOURCE) is False
        assert _skill_supported("   ", self.SOURCE) is False


# ----------------------------------------------------------------------
# Company matcher
# ----------------------------------------------------------------------

class TestCompanySupported:
    SOURCE = (
        "Senior Engineer at Acme Corp 2020-2023. Junior Dev at "
        "Northwind Logistics, Inc. 2018-2020. Intern at Stripe."
    ).lower()

    def test_direct_match(self):
        assert _company_supported("Stripe", self.SOURCE) is True

    def test_suffix_stripped_match(self):
        """'Acme Corp' in source vs 'Acme' in tailored. Stripping
        ' Corp' from source-side comparison should match."""
        # Source has 'Acme Corp', tailored claims just 'Acme'
        assert _company_supported("Acme", self.SOURCE) is True
        # Reverse direction: tailored has 'Acme Corp', source has 'Acme Corp'
        assert _company_supported("Acme Corp", self.SOURCE) is True

    def test_inc_suffix_stripped(self):
        """Source has 'Northwind Logistics, Inc.' — tailored claims
        'Northwind Logistics'. Match."""
        assert _company_supported("Northwind Logistics", self.SOURCE) is True
        assert _company_supported("Northwind Logistics, Inc.", self.SOURCE) is True

    def test_fabricated_company_rejected(self):
        assert _company_supported("Google", self.SOURCE) is False
        assert _company_supported("Bogus Industries LLC", self.SOURCE) is False

    def test_partial_token_match(self):
        """Source: 'Northwind Logistics'; tailored: 'Northwind'. Single
        token but ≥4 chars, so it should match via the token rule."""
        assert _company_supported("Northwind", self.SOURCE) is True

    def test_empty_company_returns_false(self):
        assert _company_supported("", self.SOURCE) is False


# ----------------------------------------------------------------------
# validate_tailored_resume — full integration on TailoredResume
# ----------------------------------------------------------------------

SOURCE_RESUME = (
    "Jane Doe — Data Engineer\n\n"
    "Senior Data Engineer at Acme Corp (2020-2023):\n"
    "- Built revenue dashboards using Python, SQL, Tableau\n"
    "- Designed event pipelines with Apache Kafka and Postgres\n"
    "- Led migration from on-prem to AWS (S3, Lambda, ECS)\n\n"
    "Junior Engineer at Northwind Logistics, Inc. (2018-2020):\n"
    "- Wrote ETL scripts in Python\n"
    "- Built REST APIs using FastAPI\n\n"
    "Education: BS Computer Science, State University, 2018"
)


def _make_tailored(skills, experience, education=None):
    return TailoredResume(
        summary="Test summary.",
        skills=skills,
        experience=experience,
        education=education or [],
    )


class TestValidateAcceptablePath:
    def test_clean_tailored_passes(self):
        tailored = _make_tailored(
            skills=["Python", "SQL", "Apache Kafka", "Postgres", "AWS Lambda"],
            experience=[
                {"title": "Senior Engineer", "company": "Acme Corp",
                 "dates": "2020-2023", "bullets": ["Built things"]},
                {"title": "Junior Engineer", "company": "Northwind Logistics",
                 "dates": "2018-2020", "bullets": ["Wrote ETL"]},
            ],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is True
        assert report.dropped_skills == []
        assert report.dropped_experience_companies == []
        # Original entries all kept
        assert len(tailored.skills) == 5
        assert len(tailored.experience) == 2

    def test_minor_skill_fabrication_dropped_but_accepted(self):
        """One bad skill in five — accept overall but drop the bad one."""
        tailored = _make_tailored(
            skills=["Python", "SQL", "TensorFlow", "Postgres", "Kafka"],
            #                       ^-- not in source
            experience=[
                {"title": "x", "company": "Acme Corp", "dates": "y", "bullets": []},
            ],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is True
        assert "TensorFlow" in report.dropped_skills
        # 1/5 = 20% fabrication, below 50% threshold
        assert report.skill_fabrication_rate == pytest.approx(0.2)
        # Tailored.skills now has 4 entries
        assert len(tailored.skills) == 4
        assert "TensorFlow" not in tailored.skills


class TestValidateRejectionPath:
    def test_majority_fabricated_skills_reject(self):
        """3 of 5 skills not in source → 60% fabrication → REJECT."""
        tailored = _make_tailored(
            skills=["Python", "TensorFlow", "Snowflake", "Kubernetes", "Rust"],
            #                  ^^^^^^^^^^^  ^^^^^^^^^   ^^^^^^^^^^^^   ^^^^
            experience=[
                {"title": "x", "company": "Acme Corp", "dates": "y", "bullets": []},
            ],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is False
        assert "fabricated" in report.reason.lower() or "not supported" in report.reason.lower()
        assert report.skill_fabrication_rate > 0.5

    def test_fabricated_company_reject(self):
        """A single tailored experience entry with a company that's
        not in source → reject (>34% threshold for experience)."""
        tailored = _make_tailored(
            skills=["Python", "SQL"],
            experience=[
                {"title": "Senior Eng", "company": "Acme Corp",
                 "dates": "2020-2023", "bullets": []},
                {"title": "Lead", "company": "Google",
                 "dates": "2017-2020", "bullets": []},
                # ^^^^^^^^ Google not in source — fabricated
            ],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is False
        assert "Google" in report.dropped_experience_companies
        # Reason mentions company / experience
        msg = report.reason.lower()
        assert "company" in msg or "experience" in msg or "employer" in msg


class TestValidatorEdgeCases:
    def test_empty_source_passes_safely(self):
        """No source to compare against → trust the LLM, return
        acceptable. Outer fallback chain still catches downstream
        errors (PDF render fails, etc.)."""
        tailored = _make_tailored(
            skills=["Python", "Whatever"],
            experience=[],
        )
        report = validate_tailored_resume(tailored, "")
        assert report.is_acceptable is True
        # Skills not filtered when no source to validate against
        assert len(tailored.skills) == 2

    def test_non_string_skills_silently_skipped(self):
        """A list with a stray int / None shouldn't crash the
        validator — just skip the bad entry."""
        tailored = _make_tailored(
            skills=["Python", None, 42, "SQL"],  # type: ignore[list-item]
            experience=[],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is True
        # Only the string skills survived AND were validated
        assert "Python" in tailored.skills
        assert "SQL" in tailored.skills
        assert None not in tailored.skills
        assert 42 not in tailored.skills

    def test_education_unchanged(self):
        """Education isn't validated (rare fabrication target).
        The tailored.education list is left as-is."""
        tailored = _make_tailored(
            skills=["Python"],
            experience=[],
            education=[
                {"school": "Random Made-Up University", "degree": "PhD", "year": "2025"},
            ],
        )
        validate_tailored_resume(tailored, SOURCE_RESUME)
        # Education NOT filtered — same length as input
        assert len(tailored.education) == 1
        assert tailored.education[0]["school"] == "Random Made-Up University"

    def test_no_company_on_experience_entry_kept(self):
        """If tailored experience has no company field, we keep the
        entry — no claim, nothing to verify."""
        tailored = _make_tailored(
            skills=["Python"],
            experience=[
                {"title": "Volunteer", "dates": "Summer 2019", "bullets": []},
            ],
        )
        report = validate_tailored_resume(tailored, SOURCE_RESUME)
        assert report.is_acceptable is True
        assert len(tailored.experience) == 1


# ----------------------------------------------------------------------
# Engine integration smoke test (via the validator hook in
# _tailor_resume_for_job — we exercise the validator directly,
# trusting that the engine wiring is the right thin pass-through)
# ----------------------------------------------------------------------

class TestValidationReportShape:
    def test_report_dataclass_fields(self):
        r = ValidationReport(is_acceptable=True)
        assert r.is_acceptable is True
        assert r.reason == ""
        assert r.dropped_skills == []
        assert r.dropped_experience_companies == []
        assert r.skill_fabrication_rate == 0.0
        assert r.experience_fabrication_rate == 0.0
