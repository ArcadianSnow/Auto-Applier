"""Tests for analysis/title_expansion.py — seed-to-adjacents expansion."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.analysis.title_expansion import (
    ExpansionResult,
    STATIC_TITLE_EXPANSIONS,
    _normalize,
    _static_expand,
    expand_title,
)


# ------------------------------------------------------------------
# _normalize
# ------------------------------------------------------------------

class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Data Analyst") == "data analyst"

    def test_collapse_whitespace(self):
        assert _normalize("data   analyst") == "data analyst"
        assert _normalize("  data analyst  ") == "data analyst"

    def test_empty(self):
        assert _normalize("") == ""
        assert _normalize(None) == ""


# ------------------------------------------------------------------
# _static_expand
# ------------------------------------------------------------------

class TestStaticExpand:
    def test_known_seed(self):
        result = _static_expand("Data Analyst")
        assert len(result) >= 3
        assert "business intelligence analyst" in result

    def test_case_insensitive(self):
        assert _static_expand("DATA ANALYST") == _static_expand("data analyst")

    def test_unknown_seed(self):
        assert _static_expand("Plumber") == []

    def test_empty_seed(self):
        assert _static_expand("") == []

    def test_returns_copy(self):
        """Callers shouldn't be able to mutate the dict from outside."""
        a = _static_expand("Data Analyst")
        a.append("oops")
        b = _static_expand("Data Analyst")
        assert "oops" not in b


class TestStaticDictCoverage:
    """Sanity checks on the static dictionary itself."""

    def test_minimum_seeds(self):
        """We want at least 15 seeds covering common career tracks."""
        assert len(STATIC_TITLE_EXPANSIONS) >= 15

    def test_all_values_are_lists(self):
        for seed, adjacents in STATIC_TITLE_EXPANSIONS.items():
            assert isinstance(adjacents, list), f"{seed} has non-list value"
            assert all(isinstance(x, str) for x in adjacents), \
                f"{seed} has non-string in list"

    def test_all_keys_lowercase(self):
        for seed in STATIC_TITLE_EXPANSIONS:
            assert seed == seed.lower(), f"{seed} is not lowercase"

    def test_no_seniority_inflation(self):
        """Static adjacents should never be senior/lead/principal versions."""
        forbidden_prefixes = ("senior ", "lead ", "principal ", "staff ", "director")
        for seed, adjacents in STATIC_TITLE_EXPANSIONS.items():
            for adj in adjacents:
                for prefix in forbidden_prefixes:
                    assert not adj.lower().startswith(prefix), (
                        f"'{adj}' (for seed '{seed}') inflates seniority"
                    )

    def test_seed_not_in_own_list(self):
        """The seed shouldn't appear in its own adjacent list."""
        for seed, adjacents in STATIC_TITLE_EXPANSIONS.items():
            assert seed not in [a.lower() for a in adjacents], (
                f"'{seed}' appears in its own adjacents list"
            )


# ------------------------------------------------------------------
# expand_title — LLM path
# ------------------------------------------------------------------

class FakeRouter:
    """Minimal router stub for testing the LLM branch."""

    def __init__(self, response: dict | None = None, raises: Exception | None = None):
        self.response = response or {}
        self.raises = raises
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.response


class TestExpandTitleLLM:
    def test_llm_success(self):
        router = FakeRouter(response={
            "adjacents": [
                "Business Intelligence Analyst",
                "Reporting Analyst",
                "Analytics Engineer",
            ],
            "reasoning": "All use SQL + dashboards daily.",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
            resume_text="",
        ))
        assert result.source == "llm"
        assert len(result.adjacents) == 3
        assert "Business Intelligence Analyst" in result.adjacents
        assert result.reasoning == "All use SQL + dashboards daily."

    def test_llm_dedups_against_seed(self):
        """LLM shouldn't return the seed itself."""
        router = FakeRouter(response={
            "adjacents": ["Data Analyst", "Business Analyst", "Reporting Analyst"],
            "reasoning": "Same skill set",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        # seed stripped
        assert "Data Analyst" not in result.adjacents
        assert len(result.adjacents) == 2

    def test_llm_dedups_duplicates(self):
        router = FakeRouter(response={
            "adjacents": ["Business Analyst", "business analyst", "Reporting"],
            "reasoning": "",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        assert len(result.adjacents) == 2  # duplicate stripped

    def test_llm_caps_at_five(self):
        router = FakeRouter(response={
            "adjacents": [f"title {i}" for i in range(10)],
            "reasoning": "",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        assert len(result.adjacents) == 5

    def test_llm_failure_falls_back_to_static(self):
        router = FakeRouter(raises=RuntimeError("LLM down"))
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        assert result.source == "static"
        assert len(result.adjacents) > 0

    def test_llm_empty_response_falls_back_to_static(self):
        router = FakeRouter(response={"adjacents": [], "reasoning": ""})
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        assert result.source == "static"
        assert len(result.adjacents) > 0

    def test_llm_malformed_falls_back_to_static(self):
        """LLM returns non-list for adjacents."""
        router = FakeRouter(response={
            "adjacents": "not a list",
            "reasoning": "broken",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
        ))
        assert result.source == "static"

    def test_resume_text_passed_to_llm(self):
        router = FakeRouter(response={
            "adjacents": ["Analyst 1"],
            "reasoning": "",
        })
        asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
            resume_text="I use Python and SQL daily.",
        ))
        # verify resume was included in the prompt
        assert "Python" in router.calls[0]["prompt"]


# ------------------------------------------------------------------
# expand_title — static path
# ------------------------------------------------------------------

class TestExpandTitleStatic:
    def test_no_router_uses_static(self):
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=None,
        ))
        assert result.source == "static"
        assert len(result.adjacents) > 0

    def test_prefer_llm_false_uses_static(self):
        router = FakeRouter(response={
            "adjacents": ["llm result"],
            "reasoning": "",
        })
        result = asyncio.run(expand_title(
            seed="Data Analyst",
            router=router,
            prefer_llm=False,
        ))
        assert result.source == "static"
        assert "llm result" not in result.adjacents

    def test_unknown_seed_no_router(self):
        result = asyncio.run(expand_title(
            seed="Completely Unknown Title",
            router=None,
        ))
        assert result.source == "static"
        assert result.adjacents == []
        assert not result.has_suggestions

    def test_empty_seed(self):
        result = asyncio.run(expand_title(seed="", router=None))
        assert result.seed == ""
        assert not result.has_suggestions

    def test_case_insensitive_lookup(self):
        result = asyncio.run(expand_title(
            seed="DATA ANALYST",
            router=None,
        ))
        assert result.source == "static"
        assert len(result.adjacents) > 0


# ------------------------------------------------------------------
# ExpansionResult
# ------------------------------------------------------------------

class TestExpansionResult:
    def test_has_suggestions_true(self):
        r = ExpansionResult(seed="x", adjacents=["a", "b"])
        assert r.has_suggestions is True

    def test_has_suggestions_false(self):
        r = ExpansionResult(seed="x", adjacents=[])
        assert r.has_suggestions is False

    def test_defaults(self):
        r = ExpansionResult(seed="x")
        assert r.adjacents == []
        assert r.source == "static"
        assert r.reasoning == ""
