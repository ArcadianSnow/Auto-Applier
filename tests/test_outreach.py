"""Tests for the LinkedIn outreach writer."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from auto_applier.llm.base import LLMResponse
from auto_applier.resume.outreach import (
    LINKEDIN_CONNECTION_LIMIT,
    OutreachWriter,
)


def _router_returning(text: str):
    router = MagicMock()
    router.complete = AsyncMock(return_value=LLMResponse(
        text=text, model="test", tokens_used=0, cached=False, latency_ms=1.0,
    ))
    return router


def _call(writer: OutreachWriter) -> str:
    return asyncio.run(writer.generate(
        resume_text="Python developer with 5 years of SQL and dashboards",
        job_description="Looking for a data analyst",
        company_name="Acme",
        job_title="Data Analyst",
    ))


class TestOutreachWriter:
    def test_returns_empty_on_llm_exception(self):
        router = MagicMock()
        router.complete = AsyncMock(side_effect=RuntimeError("boom"))
        writer = OutreachWriter(router)
        assert _call(writer) == ""

    def test_strips_wrapping_quotes(self):
        router = _router_returning('"Hello, this is a short message."')
        writer = OutreachWriter(router)
        result = _call(writer)
        assert not result.startswith('"')
        assert not result.endswith('"')

    def test_strips_wrapping_asterisks(self):
        router = _router_returning("*this is bolded*")
        writer = OutreachWriter(router)
        result = _call(writer)
        assert not result.startswith("*")

    def test_enforces_280_char_limit(self):
        long = "word " * 100  # ~500 chars
        router = _router_returning(long.strip())
        writer = OutreachWriter(router)
        result = _call(writer)
        assert len(result) <= LINKEDIN_CONNECTION_LIMIT

    def test_trims_to_sentence_boundary_when_possible(self):
        text = (
            "First sentence is reasonably long and establishes context here. "
            "Second sentence keeps going and eventually crosses the limit by a lot, "
            "adding even more words to push it well past 280 chars."
        )
        router = _router_returning(text)
        writer = OutreachWriter(router)
        result = _call(writer)
        assert len(result) <= LINKEDIN_CONNECTION_LIMIT
        # Should end on a period, not mid-word
        assert result.endswith(".")

    def test_short_output_preserved_unchanged(self):
        msg = "Saw the data analyst role — my 5 years with SQL dashboards look like a match."
        router = _router_returning(msg)
        writer = OutreachWriter(router)
        result = _call(writer)
        assert result == msg
