"""Tests for LLM abstraction layer."""
import json
import pytest
import tempfile
from pathlib import Path

from auto_applier.llm.prompts import FORM_FILL, JOB_SCORE, COVER_LETTER, RESUME_SELECT
from auto_applier.llm.cache import ResponseCache
from auto_applier.llm.base import LLMResponse


class TestPrompts:
    def test_form_fill_format(self):
        prompt = FORM_FILL.format(
            resume_text="Python developer with 5 years experience",
            job_description="Looking for a Python developer",
            question="How many years of Python experience?",
        )
        assert "Python developer with 5 years experience" in prompt
        assert "How many years of Python experience?" in prompt

    def test_job_score_format(self):
        prompt = JOB_SCORE.format(
            resume_text="Resume text here",
            job_description="JD text here",
        )
        assert "Resume text here" in prompt
        assert "JD text here" in prompt

    def test_cover_letter_format(self):
        prompt = COVER_LETTER.format(
            resume_text="Resume",
            job_description="JD",
            company_name="Acme Corp",
            job_title="Software Engineer",
        )
        assert "Acme Corp" in prompt
        assert "Software Engineer" in prompt

    def test_resume_select_format(self):
        prompt = RESUME_SELECT.format(
            resume_label="data_analyst",
            resume_text="Resume",
            job_description="JD",
        )
        assert "data_analyst" in prompt

    def test_all_prompts_have_system(self):
        from auto_applier.llm import prompts
        for name in ["FORM_FILL", "JOB_SCORE", "SKILL_EXTRACT_RESUME", "SKILL_EXTRACT_JD", "RESUME_BULLET", "RESUME_SELECT", "COVER_LETTER"]:
            template = getattr(prompts, name)
            assert template.system, f"{name} missing system prompt"
            assert template.template, f"{name} missing template"


class TestCache:
    def test_cache_put_and_get(self, tmp_path):
        cache = ResponseCache(cache_dir=tmp_path, ttl_hours=72)
        response = LLMResponse(text="Hello", model="test", tokens_used=10, cached=False, latency_ms=100)

        cache.put("system", "prompt", response)
        result = cache.get("system", "prompt")

        assert result is not None
        assert result.text == "Hello"
        assert result.cached == True

    def test_cache_miss(self, tmp_path):
        cache = ResponseCache(cache_dir=tmp_path, ttl_hours=72)
        result = cache.get("system", "nonexistent")
        assert result is None

    def test_cache_different_prompts(self, tmp_path):
        cache = ResponseCache(cache_dir=tmp_path, ttl_hours=72)
        r1 = LLMResponse(text="A", model="test", tokens_used=1, cached=False, latency_ms=1)
        r2 = LLMResponse(text="B", model="test", tokens_used=1, cached=False, latency_ms=1)

        cache.put("sys", "prompt1", r1)
        cache.put("sys", "prompt2", r2)

        assert cache.get("sys", "prompt1").text == "A"
        assert cache.get("sys", "prompt2").text == "B"
