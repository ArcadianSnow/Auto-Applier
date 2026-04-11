"""Tests for ghost job detection."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.analysis.ghost_check import (
    GhostCheckResult,
    GhostJobChecker,
    should_skip_ghost,
)


def _run(coro):
    return asyncio.run(coro)


def _router_returning(payload: dict):
    router = MagicMock()
    router.complete_json = AsyncMock(return_value=payload)
    return router


class TestShouldSkipGhost:
    def test_unchecked_never_skips(self):
        assert should_skip_ghost(-1, "high", 8) is False

    def test_low_confidence_never_skips(self):
        assert should_skip_ghost(10, "low", 8) is False

    def test_medium_confidence_never_skips(self):
        assert should_skip_ghost(10, "medium", 8) is False

    def test_below_threshold_never_skips(self):
        assert should_skip_ghost(7, "high", 8) is False

    def test_high_confidence_at_threshold_skips(self):
        assert should_skip_ghost(8, "high", 8) is True

    def test_high_confidence_above_threshold_skips(self):
        assert should_skip_ghost(10, "high", 8) is True


class TestGhostJobChecker:
    def test_empty_description_returns_none(self):
        checker = GhostJobChecker(MagicMock())
        result = _run(checker.check("", "Acme", "Engineer"))
        assert result is None

    def test_too_short_description_returns_none(self):
        checker = GhostJobChecker(MagicMock())
        result = _run(checker.check("hire us", "Acme", "Engineer"))
        assert result is None

    def test_llm_exception_returns_none(self):
        router = MagicMock()
        router.complete_json = AsyncMock(side_effect=RuntimeError("boom"))
        checker = GhostJobChecker(router)
        result = _run(checker.check(
            "a" * 200, "Acme", "Engineer",
        ))
        assert result is None

    def test_valid_response_parses(self):
        router = _router_returning({
            "ghost_score": 3,
            "confidence": "medium",
            "signals": ["specific team mentioned", "realistic years"],
            "verdict": "Probably real",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check(
            "a" * 200, "Acme", "Senior Analyst",
        ))
        assert result is not None
        assert result.score == 3
        assert result.confidence == "medium"
        assert "specific team mentioned" in result.signals
        assert result.verdict == "Probably real"

    def test_clamps_score_to_range(self):
        router = _router_returning({
            "ghost_score": 99,
            "confidence": "high",
            "signals": ["suspicious"],
            "verdict": "Ghost",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result.score == 10

    def test_negative_score_clamped_to_zero(self):
        router = _router_returning({
            "ghost_score": -5,
            "confidence": "high",
            "signals": [],
            "verdict": "Real",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result.score == 0

    def test_non_numeric_score_returns_none(self):
        router = _router_returning({
            "ghost_score": "n/a",
            "confidence": "high",
            "signals": [],
            "verdict": "Real",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result is None

    def test_unknown_confidence_defaults_to_low(self):
        router = _router_returning({
            "ghost_score": 5,
            "confidence": "certain",  # not in the valid set
            "signals": [],
            "verdict": "Maybe",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result.confidence == "low"

    def test_missing_verdict_returns_none(self):
        router = _router_returning({
            "ghost_score": 5,
            "confidence": "high",
            "signals": ["something"],
            # no verdict
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result is None

    def test_signals_coerced_to_strings(self):
        router = _router_returning({
            "ghost_score": 5,
            "confidence": "medium",
            "signals": ["valid", "", "  ", 42, None],
            "verdict": "Mixed",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert "valid" in result.signals
        assert "42" in result.signals
        assert "" not in result.signals

    def test_non_list_signals_becomes_empty(self):
        router = _router_returning({
            "ghost_score": 5,
            "confidence": "medium",
            "signals": "not a list",
            "verdict": "Mixed",
        })
        checker = GhostJobChecker(router)
        result = _run(checker.check("a" * 200, "Acme", "Engineer"))
        assert result.signals == []
