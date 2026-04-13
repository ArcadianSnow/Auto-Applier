"""Tests for resume/cover_letter.py — CoverLetterWriter."""

import asyncio

import pytest

from auto_applier.llm.base import LLMResponse
from auto_applier.resume.cover_letter import CoverLetterWriter


class FakeRouter:
    """Minimal LLMRouter stub for testing."""

    def __init__(self, response_text="Dear Hiring Manager, ..."):
        self.response_text = response_text
        self.last_call = None

    async def complete(self, **kwargs):
        self.last_call = kwargs
        return LLMResponse(
            text=self.response_text,
            model="fake",
            tokens_used=50,
            cached=False,
            latency_ms=10.0,
        )


class FailingRouter:
    async def complete(self, **kwargs):
        raise RuntimeError("LLM down")


class TestCoverLetterWriter:
    def test_generates_text(self):
        router = FakeRouter("Great cover letter here.")
        writer = CoverLetterWriter(router)

        async def run():
            return await writer.generate(
                resume_text="Skilled in Python",
                job_description="Need Python dev",
                company_name="Acme",
                job_title="SWE",
            )

        result = asyncio.run(run())
        assert result == "Great cover letter here."

    def test_truncates_inputs(self):
        router = FakeRouter("ok")
        writer = CoverLetterWriter(router)
        long_resume = "x" * 10000
        long_jd = "y" * 10000

        asyncio.run(writer.generate(long_resume, long_jd, "Co", "Dev"))
        prompt = router.last_call["prompt"]
        assert len(prompt) < 10000

    def test_returns_empty_on_failure(self):
        writer = CoverLetterWriter(FailingRouter())
        result = asyncio.run(writer.generate("resume", "jd", "Co", "Dev"))
        assert result == ""

    def test_strips_whitespace(self):
        router = FakeRouter("  letter with spaces  \n")
        writer = CoverLetterWriter(router)
        result = asyncio.run(writer.generate("r", "j", "C", "T"))
        assert result == "letter with spaces"

    def test_uses_cover_letter_system_prompt(self):
        from auto_applier.llm.prompts import COVER_LETTER
        router = FakeRouter("text")
        writer = CoverLetterWriter(router)
        asyncio.run(writer.generate("r", "j", "C", "T"))
        assert router.last_call["system_prompt"] == COVER_LETTER.system
