"""Goal-elicitation chat (Direction 1, Phase B) — module logic.

Covers the deterministic spine (step order, salary regex, keyword fallbacks) and the bounded-LLM
parse path (stub LLM honored; LLM error degrades to the fallback). The flow must be fully
deterministic and never raise mid-conversation — that's the whole design contract.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_applier.onboarding_chat import (
    GOAL_STEPS,
    apply_updates,
    detect_relocation,
    first_step,
    next_step_after,
    parse_answer,
    scan_salary,
    step_for_key,
    suggest_adjacent_roles,
    summarize,
)


def _run(coro):
    return asyncio.run(coro)


class _DictLLM:
    """complete_json returns a fixed dict (accepts the think/num_predict kwargs the parser passes)."""

    def __init__(self, payload: dict):
        self.payload = payload

    async def complete_json(self, prompt, *, system="", think=None, num_predict=None):
        return self.payload


class _RaisingLLM:
    async def complete_json(self, *a, **k):
        raise RuntimeError("ollama down")


# --------------------------------------------------------------- flow (deterministic)


class TestFlow:

    def test_step_order(self):
        assert [s.key for s in GOAL_STEPS] == ["roles", "location", "comp", "priorities"]

    def test_first_and_next(self):
        assert first_step().key == "roles"
        assert next_step_after("roles").key == "location"
        assert next_step_after("location").key == "comp"
        assert next_step_after("comp").key == "priorities"
        assert next_step_after("priorities") is None

    def test_step_for_key_unknown(self):
        assert step_for_key("nope") is None

    def test_parse_unknown_step_raises(self):
        with pytest.raises(KeyError):
            _run(parse_answer("nope", "x"))


# --------------------------------------------------------------- salary (regex, no LLM)


class TestComp:

    @pytest.mark.parametrize("answer,expected", [
        ("$150k", 150000),
        ("150k", 150000),
        ("150", 150000),               # bare < 1000 → thousands
        ("120,000", 120000),
        ("at least 90k", 90000),
        ("around $120,000 a year", 120000),
        ("1.2m", 1200000),
        ("no minimum", None),
        ("flexible", None),
        ("", None),
        ("not sure", None),
    ])
    def test_parse_comp(self, answer, expected):
        # comp ignores the LLM entirely — pass a raising one to prove it's never called.
        out = _run(parse_answer("comp", answer, _RaisingLLM()))
        assert out == {"salary_floor": expected}


# --------------------------------------------------------------- deterministic fallbacks


class TestFallback:

    def test_roles_split_and_seniority(self):
        out = _run(parse_answer("roles", "senior backend engineer, platform engineer", None))
        assert out["titles"] == ["senior backend engineer", "platform engineer"]
        assert out["seniority"] == "senior"

    def test_roles_staff_keyword(self):
        out = _run(parse_answer("roles", "staff or principal SRE", None))
        assert out["seniority"] == "staff"

    def test_location_remote_only(self):
        out = _run(parse_answer("location", "remote in the US", None))
        assert out["remote_ok"] is True
        assert out["onsite_ok"] is False
        # the lone phrase mentions remote → dropped from concrete locations
        assert out["locations"] == []

    def test_location_city_kept(self):
        out = _run(parse_answer("location", "Amsterdam, or remote in the EU", None))
        assert out["locations"] == ["Amsterdam"]
        assert out["remote_ok"] is True

    def test_location_onsite(self):
        out = _run(parse_answer("location", "Seattle office", None))
        assert out["onsite_ok"] is True
        assert out["remote_ok"] is False
        assert out["locations"] == ["Seattle office"]

    def test_location_no_signal_defaults_both(self):
        out = _run(parse_answer("location", "not sure yet", None))
        assert out["remote_ok"] is True
        assert out["onsite_ok"] is True

    def test_priorities_split(self):
        out = _run(parse_answer("priorities", "work-life balance, no on-call; Python stack", None))
        assert out["preferences"] == ["work-life balance", "no on-call", "Python stack"]


# --------------------------------------------------------------- bounded-LLM parse


class TestLLMParse:

    def test_roles_uses_llm_output(self):
        llm = _DictLLM({"titles": ["Senior Backend Engineer"], "seniority": "senior"})
        out = _run(parse_answer("roles", "sr be dev", llm))
        assert out["titles"] == ["Senior Backend Engineer"]
        assert out["seniority"] == "senior"

    def test_location_honors_explicit_false(self):
        llm = _DictLLM({"locations": ["NYC"], "remote_ok": False, "onsite_ok": True})
        out = _run(parse_answer("location", "nyc, onsite only", llm))
        assert out["locations"] == ["NYC"]
        assert out["remote_ok"] is False
        assert out["onsite_ok"] is True

    def test_llm_error_degrades_to_fallback(self):
        # A raising LLM must NOT break the chat — it falls back to the keyword parse.
        out = _run(parse_answer("roles", "senior data engineer", _RaisingLLM()))
        assert out["titles"] == ["senior data engineer"]
        assert out["seniority"] == "senior"

    def test_llm_garbage_falls_back(self):
        # Non-dict / empty LLM output → fallback, never a crash.
        out = _run(parse_answer("priorities", "remote culture, growth", _DictLLM(None)))
        assert out["preferences"] == ["remote culture", "growth"]


# --------------------------------------------------------------- merge + summary


class TestMergeAndSummary:

    def test_apply_updates_overwrites(self):
        draft = {"titles": ["old"], "remote_ok": True}
        merged = apply_updates(draft, {"titles": ["new"], "seniority": "mid"})
        assert merged["titles"] == ["new"]
        assert merged["seniority"] == "mid"
        assert merged["remote_ok"] is True  # untouched key preserved

    def test_summarize_includes_fields(self):
        draft = {
            "titles": ["Backend Engineer"], "seniority": "senior",
            "locations": ["Amsterdam"], "remote_ok": True,
            "salary_floor": 150000, "preferences": ["work-life balance"],
        }
        text = summarize(draft)
        assert "Backend Engineer" in text
        assert "senior" in text
        assert "Amsterdam" in text
        assert "remote" in text
        assert "$150,000" in text
        assert "work-life balance" in text


# --------------------------------------------------------------- widening + salary capture


class TestSalaryScan:

    def test_dollar_k(self):
        assert scan_salary("I currently make $82k and don't want a drop") == 82_000

    def test_bare_k(self):
        assert scan_salary("around 95k would be good") == 95_000

    def test_keyword_plus_number(self):
        assert scan_salary("my salary is 110000 a year") == 110_000

    def test_no_false_positive_on_years(self):
        # No $/k cue and no pay keyword → not a salary.
        assert scan_salary("I have 8 years of experience on a team of 12") is None

    def test_empty(self):
        assert scan_salary("") is None

    def test_opportunistic_capture_in_roles_answer(self):
        # Stated in the roles step → captured so the comp step needn't ask again.
        updates = _run(parse_answer("roles", "data analyst, I make $82k now", llm=None))
        assert updates["salary_floor"] == 82_000


class TestRelocation:

    def test_detects_cues(self):
        assert detect_relocation("open to relocating abroad with visa sponsorship")
        assert detect_relocation("a company that could help with relocation")
        assert not detect_relocation("fully remote in the US")

    def test_location_keeps_onsite_open_when_relocating(self):
        # "fully remote ... but open to moving abroad" must NOT switch on-site off.
        updates = _run(parse_answer(
            "location", "ideally fully remote but open to relocating abroad", llm=None))
        assert updates["onsite_ok"] is True


class TestRoleSuggestions:

    def test_suggests_adjacent_data_roles(self):
        out = suggest_adjacent_roles(["Data Analyst"], "something data related")
        assert "Data Engineer" in out
        assert "Data Analyst" not in out          # already chosen → not re-suggested
        assert len(out) <= 5

    def test_empty_when_no_family_matches(self):
        assert suggest_adjacent_roles(["Chef"], "kitchen work") == []


class TestPreferenceAccumulation:

    def test_preferences_union_not_clobbered(self):
        # A relocation note added at the location step survives the later priorities step.
        draft = apply_updates({}, {"preferences": ["open to relocation / visa sponsorship"]})
        draft = apply_updates(draft, {"preferences": ["work-life balance"]})
        assert draft["preferences"] == [
            "open to relocation / visa sponsorship", "work-life balance",
        ]

    def test_preferences_dedup(self):
        draft = apply_updates({"preferences": ["wlb"]}, {"preferences": ["WLB", "no on-call"]})
        assert draft["preferences"] == ["wlb", "no on-call"]
