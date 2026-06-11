"""STAR+R story bank tests (spec §11 Phase 6 extras, 9/M)."""

from __future__ import annotations

import asyncio
import json

import pytest

from auto_applier.resume.factbank import Contact, FactBank, WorkEntry
from auto_applier.resume.story_bank import (
    Story,
    StoryGenerator,
    append_stories,
    export_bank_markdown,
    load_bank,
    save_bank,
)


def _bank() -> FactBank:
    return FactBank(
        contact=Contact(name="Ada", email="a@b.c"),
        work_history=[WorkEntry(company="Acme", title="Data Engineer",
                                start="2020", end="Present",
                                bullets=["Built the billing pipeline"])],
        skills=["Python", "SQL"],
        allowed_metrics=["13 partner labs"],
    )


def _story(**over) -> Story:
    base = dict(
        title="The silent billing failure",
        question_prompt="Tell me about a production incident",
        situation="Billing ran as a dozen hand-run scripts.",
        task="Stop bad data reaching invoices.",
        action="Rebuilt it as one idempotent orchestrator.",
        result="Validation now blocks bad months before delivery.",
        reflection="Loud failures beat silent ones.",
        job_id="j1", company="Acme", job_title="Data Engineer",
    )
    base.update(over)
    return Story(**base)


class _StubLLM:
    """CompletionClient stub: returns a canned payload or raises."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.prompts: list[str] = []

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.payload


def _ok_payload(n=3) -> dict:
    return {"stories": [
        {"title": f"Story {i}", "question_prompt": f"Q{i}", "situation": "s",
         "task": "t", "action": "a", "result": "r", "reflection": "x"}
        for i in range(n)
    ]}


# ------------------------------------------------------------------ persistence

def test_load_missing_file_returns_empty(tmp_path):
    assert load_bank(tmp_path / "nope.json") == []


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "bank" / "story_bank.json"  # parent dir auto-created
    stories = [_story(), _story(title="Second")]
    save_bank(path, stories)
    got = load_bank(path)
    assert [s.title for s in got] == ["The silent billing failure", "Second"]
    assert got[0].company == "Acme" and got[0].created_at


def test_load_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "story_bank.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_bank(path) == []


def test_load_skips_invalid_entries_and_unknown_keys(tmp_path):
    path = tmp_path / "story_bank.json"
    payload = [
        {"title": "Ok", "question_prompt": "", "situation": "s", "task": "t",
         "action": "a", "result": "r", "reflection": "x", "legacy_field": "ignored"},
        "not-a-dict",
        {"title": "missing required STAR fields"},  # TypeError → skipped
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    got = load_bank(path)
    assert len(got) == 1 and got[0].title == "Ok"


def test_append_accumulates_without_dropping(tmp_path):
    path = tmp_path / "story_bank.json"
    append_stories(path, [_story()])
    append_stories(path, [_story(title="Second")])
    append_stories(path, [])  # no-op, must not clobber
    assert [s.title for s in load_bank(path)] == ["The silent billing failure", "Second"]


# ------------------------------------------------------------------ generation

def test_generate_parses_valid_stories():
    gen = StoryGenerator(_StubLLM(payload=_ok_payload()))
    stories = asyncio.run(gen.generate(
        _bank(), "JD text", company="Acme", title="Data Engineer", job_id="j9"))
    assert len(stories) == 3
    assert all(s.job_id == "j9" and s.company == "Acme" for s in stories)
    assert stories[0].job_title == "Data Engineer"


def test_generate_prompt_carries_bank_facts_and_jd():
    stub = _StubLLM(payload=_ok_payload())
    asyncio.run(StoryGenerator(stub).generate(_bank(), "Debezium CDC pipelines"))
    prompt = stub.prompts[0]
    assert "Acme" in prompt                  # bank fact made it in
    assert "13 partner labs" in prompt       # allowed metric made it in
    assert "Debezium CDC pipelines" in prompt


def test_generate_filters_stories_missing_segments():
    payload = _ok_payload(2)
    payload["stories"][1]["result"] = "   "  # blank segment → dropped
    gen = StoryGenerator(_StubLLM(payload=payload))
    stories = asyncio.run(gen.generate(_bank(), "JD"))
    assert len(stories) == 1


@pytest.mark.parametrize("payload", [
    {},                          # no stories key
    {"stories": "nope"},         # non-list
    {"stories": []},             # empty
    ["not", "a", "dict"],        # non-dict reply
])
def test_generate_malformed_reply_returns_empty(payload):
    gen = StoryGenerator(_StubLLM(payload=payload))
    assert asyncio.run(gen.generate(_bank(), "JD")) == []


def test_generate_llm_failure_returns_empty_never_raises():
    gen = StoryGenerator(_StubLLM(exc=RuntimeError("ollama down")))
    assert asyncio.run(gen.generate(_bank(), "JD")) == []


# ------------------------------------------------------------------ export

def test_export_empty_bank():
    assert "_empty_" in export_bank_markdown([])


def test_export_renders_all_segments_and_provenance():
    md = export_bank_markdown([_story()])
    for fragment in ("# Interview Story Bank", "## 1. The silent billing failure",
                     "**Answers:** Tell me about a production incident",
                     "_Generated for: Data Engineer @ Acme_",
                     "**Situation.**", "**Task.**", "**Action.**",
                     "**Result.**", "**Reflection.**"):
        assert fragment in md
