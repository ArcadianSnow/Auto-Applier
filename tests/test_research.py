"""Company-research briefing tests (spec §11 Phase 6 extras, 10/M)."""

from __future__ import annotations

import asyncio

import pytest

from auto_applier.research import (
    CompanyBriefing,
    CompanyResearcher,
    briefing_path,
    load_briefing,
    save_briefing,
)


class _StubLLM:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.prompts: list[str] = []

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.payload


def _ok_payload() -> dict:
    return {
        "what_they_do": "Builds a data-quality platform for warehouses.",
        "tech_stack_signals": ["Snowflake", "dbt"],
        "culture_signals": ["remote-first"],
        "red_flags": [],
        "questions_to_ask": ["How is on-call shared?"],
        "talking_points": ["Billing-pipeline rebuild maps to their reliability story"],
    }


def _briefing(**over) -> CompanyBriefing:
    base = dict(company="Acme Data", what_they_do="Data tools.",
                tech_stack_signals=["SQL"], culture_signals=[], red_flags=[],
                questions_to_ask=["Why now?"], talking_points=[])
    base.update(over)
    return CompanyBriefing(**base)


# ------------------------------------------------------------------ paths + persistence

def test_briefing_path_normalizes_company(tmp_path):
    p = briefing_path(tmp_path, "Acme, Inc. (Data)")
    assert p.parent == tmp_path
    assert p.name == "acme_inc_data.md"


def test_briefing_path_empty_company_is_unknown(tmp_path):
    assert briefing_path(tmp_path, "!!!").name == "unknown.md"


def test_save_and_load_roundtrip(tmp_path):
    md_path = save_briefing(tmp_path / "research", _briefing())
    assert md_path.exists() and md_path.with_suffix(".json").exists()
    got = load_briefing(tmp_path / "research", "Acme Data")
    assert got is not None
    assert got.what_they_do == "Data tools."
    assert got.questions_to_ask == ["Why now?"]


def test_load_missing_returns_none(tmp_path):
    assert load_briefing(tmp_path, "Nobody") is None


def test_load_corrupt_returns_none(tmp_path):
    md = briefing_path(tmp_path, "Bad Co")
    md.parent.mkdir(parents=True, exist_ok=True)
    md.with_suffix(".json").write_text("{broken", encoding="utf-8")
    assert load_briefing(tmp_path, "Bad Co") is None


# ------------------------------------------------------------------ markdown rendering

def test_markdown_renders_sections_and_not_in_source():
    md = _briefing().to_markdown()
    assert "# Acme Data" in md
    assert "## What they do" in md and "Data tools." in md
    assert "- SQL" in md
    assert "_not in source_" in md  # empty culture/red-flags/talking-points sections


# ------------------------------------------------------------------ research flow

def test_research_builds_briefing_from_payload():
    stub = _StubLLM(payload=_ok_payload())
    briefing = asyncio.run(CompanyResearcher(stub).research("Acme Data", "source text"))
    assert briefing is not None
    assert briefing.company == "Acme Data"
    assert briefing.tech_stack_signals == ["Snowflake", "dbt"]
    assert "source text" in stub.prompts[0]


def test_research_empty_source_refuses_to_invent():
    stub = _StubLLM(payload=_ok_payload())
    assert asyncio.run(CompanyResearcher(stub).research("Acme", "   ")) is None
    assert stub.prompts == []  # never even called the LLM


@pytest.mark.parametrize("payload", [
    {},                                    # nothing grounded
    {"what_they_do": "  "},                # blank
    {"what_they_do": "Not In Source"},     # the honesty sentinel
    ["not", "a", "dict"],
])
def test_research_ungrounded_reply_returns_none(payload):
    stub = _StubLLM(payload=payload)
    assert asyncio.run(CompanyResearcher(stub).research("Acme", "text")) is None


def test_research_llm_failure_returns_none_never_raises():
    stub = _StubLLM(exc=RuntimeError("ollama down"))
    assert asyncio.run(CompanyResearcher(stub).research("Acme", "text")) is None


def test_research_tolerates_non_list_sections():
    payload = _ok_payload()
    payload["red_flags"] = "should be a list"
    briefing = asyncio.run(CompanyResearcher(_StubLLM(payload=payload)).research("A", "t"))
    assert briefing is not None and briefing.red_flags == []
