"""Tests for resume/skills.py — find_missing_skills and LLM extraction wrappers."""

import asyncio

import pytest

from auto_applier.resume.skills import (
    find_missing_skills,
    extract_resume_skills,
    extract_jd_requirements,
)


# ------------------------------------------------------------------
# find_missing_skills (pure logic, no LLM)
# ------------------------------------------------------------------

class TestFindMissingSkills:
    def test_no_gaps(self):
        resume = {
            "technical_skills": [{"name": "Python", "level": "advanced", "years": 5}],
            "tools": ["Docker"],
            "certifications": [],
        }
        jd = {"required": ["Python", "Docker"]}
        assert find_missing_skills(resume, jd) == []

    def test_missing_skills_returned(self):
        resume = {
            "technical_skills": [{"name": "Python", "level": "mid", "years": 3}],
            "tools": [],
            "certifications": [],
        }
        jd = {"required": ["Python", "Kubernetes", "Terraform"]}
        missing = find_missing_skills(resume, jd)
        assert "Kubernetes" in missing
        assert "Terraform" in missing
        assert "Python" not in missing

    def test_case_insensitive(self):
        resume = {
            "technical_skills": [{"name": "PYTHON", "level": "expert", "years": 10}],
            "tools": ["docker"],
            "certifications": ["AWS SAA"],
        }
        jd = {"required": ["python", "Docker", "aws saa"]}
        assert find_missing_skills(resume, jd) == []

    def test_empty_resume(self):
        resume = {"technical_skills": [], "tools": [], "certifications": []}
        jd = {"required": ["Go", "Rust"]}
        assert find_missing_skills(resume, jd) == ["Go", "Rust"]

    def test_empty_jd(self):
        resume = {
            "technical_skills": [{"name": "Python"}],
            "tools": ["Docker"],
            "certifications": [],
        }
        jd = {"required": []}
        assert find_missing_skills(resume, jd) == []

    def test_missing_keys_in_resume(self):
        """Handles resume dicts missing expected keys."""
        resume = {}
        jd = {"required": ["Python"]}
        assert find_missing_skills(resume, jd) == ["Python"]

    def test_missing_keys_in_jd(self):
        resume = {"technical_skills": [{"name": "Go"}], "tools": [], "certifications": []}
        jd = {}
        assert find_missing_skills(resume, jd) == []

    def test_string_skills_in_resume(self):
        """Handles resume where technical_skills are strings, not dicts."""
        resume = {
            "technical_skills": ["Python", "Go"],
            "tools": [],
            "certifications": [],
        }
        jd = {"required": ["Python", "Rust"]}
        missing = find_missing_skills(resume, jd)
        assert "Rust" in missing
        assert "Python" not in missing

    def test_tools_and_certs_count(self):
        """Tools and certifications are included in the resume skill set."""
        resume = {
            "technical_skills": [],
            "tools": ["Terraform"],
            "certifications": ["CKA"],
        }
        jd = {"required": ["Terraform", "CKA"]}
        assert find_missing_skills(resume, jd) == []


# ------------------------------------------------------------------
# LLM extraction wrappers (mocked router)
# ------------------------------------------------------------------

class FakeJsonRouter:
    def __init__(self, response: dict):
        self.response = response
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class TestExtractResumeSkills:
    def test_returns_structured_data(self):
        router = FakeJsonRouter({
            "technical_skills": [{"name": "Python", "level": "advanced", "years": 5}],
            "soft_skills": ["leadership"],
            "certifications": ["AWS"],
            "tools": ["Docker"],
        })
        result = asyncio.run(extract_resume_skills(router, "my resume text"))
        assert result["technical_skills"][0]["name"] == "Python"
        assert result["soft_skills"] == ["leadership"]
        assert result["certifications"] == ["AWS"]
        assert result["tools"] == ["Docker"]

    def test_defaults_for_missing_keys(self):
        router = FakeJsonRouter({"technical_skills": [{"name": "Go"}]})
        result = asyncio.run(extract_resume_skills(router, "resume"))
        assert result["soft_skills"] == []
        assert result["certifications"] == []
        assert result["tools"] == []

    def test_empty_response(self):
        router = FakeJsonRouter({})
        result = asyncio.run(extract_resume_skills(router, "resume"))
        assert result == {
            "technical_skills": [],
            "soft_skills": [],
            "certifications": [],
            "tools": [],
        }


class TestExtractJdRequirements:
    def test_returns_structured_data(self):
        router = FakeJsonRouter({
            "required": ["Python", "SQL"],
            "preferred": ["Spark"],
            "experience_level": "mid",
        })
        result = asyncio.run(extract_jd_requirements(router, "job desc"))
        assert result["required"] == ["Python", "SQL"]
        assert result["preferred"] == ["Spark"]
        assert result["experience_level"] == "mid"

    def test_defaults_for_missing_keys(self):
        router = FakeJsonRouter({})
        result = asyncio.run(extract_jd_requirements(router, "jd"))
        assert result == {"required": [], "preferred": [], "experience_level": ""}
