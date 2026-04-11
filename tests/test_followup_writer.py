"""Tests for the follow-up email drafter."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from auto_applier.llm.base import LLMResponse
from auto_applier.resume.followup_writer import (
    FOLLOWUP_EMAIL_MAX_WORDS,
    FollowupEmailWriter,
)


def _router_returning(text: str):
    router = MagicMock()
    router.complete = AsyncMock(return_value=LLMResponse(
        text=text, model="test", tokens_used=0, cached=False, latency_ms=1.0,
    ))
    return router


def _call(writer, attempt=1, days_since=7):
    return asyncio.run(writer.generate(
        resume_text="Senior data analyst with 6 years of SQL and dashboards",
        job_description="Looking for an analytics engineer",
        company_name="Acme",
        job_title="Analytics Engineer",
        attempt=attempt,
        days_since=days_since,
    ))


class TestFollowupEmailWriter:
    def test_empty_on_llm_failure(self):
        router = MagicMock()
        router.complete = AsyncMock(side_effect=RuntimeError("boom"))
        writer = FollowupEmailWriter(router)
        assert _call(writer) == ""

    def test_strips_wrapping_quotes(self):
        router = _router_returning('"I wanted to follow up on my application."')
        writer = FollowupEmailWriter(router)
        result = _call(writer)
        assert not result.startswith('"')

    def test_strips_leading_greeting(self):
        router = _router_returning(
            "Hi Jordan,\n\nI'm writing to follow up on my application "
            "for the Analytics Engineer role."
        )
        writer = FollowupEmailWriter(router)
        result = _call(writer)
        assert not result.lower().startswith("hi ")

    def test_attempt_clamped_high(self):
        """Any attempt > 3 should be treated as 3 (closing the loop)."""
        router = _router_returning("Short body")
        writer = FollowupEmailWriter(router)
        _call(writer, attempt=99)
        call_args = router.complete.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Attempt number: 3" in prompt

    def test_attempt_clamped_low(self):
        router = _router_returning("Short body")
        writer = FollowupEmailWriter(router)
        _call(writer, attempt=-1)
        prompt = router.complete.call_args.kwargs["prompt"]
        assert "Attempt number: 1" in prompt

    def test_word_cap_enforced(self):
        """Overshooting the word cap trims to a sentence boundary."""
        long_body = (
            "This is a sentence that ends on a period. " * 40
        )
        router = _router_returning(long_body)
        writer = FollowupEmailWriter(router)
        result = _call(writer)
        word_count = len(result.split())
        assert word_count <= FOLLOWUP_EMAIL_MAX_WORDS
        # Should end cleanly on a period, not mid-word
        assert result.endswith(".")

    def test_short_body_preserved(self):
        body = (
            "I wanted to follow up on my application for the Analytics "
            "Engineer role I submitted last week. My SQL dashboard work "
            "at Northwind maps directly to the hiring description. Any "
            "update on the timeline?"
        )
        router = _router_returning(body)
        writer = FollowupEmailWriter(router)
        result = _call(writer)
        assert result == body

    def test_days_since_threaded_into_prompt(self):
        router = _router_returning("ok")
        writer = FollowupEmailWriter(router)
        _call(writer, days_since=21)
        prompt = router.complete.call_args.kwargs["prompt"]
        assert "21" in prompt
