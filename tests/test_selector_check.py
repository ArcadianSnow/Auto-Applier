"""Tests for the selector decay monitor.

Phase 2.1: proactive smoke test that loads a known job page per
platform and asserts the platform's selector lists still match
real DOM elements. Catches site DOM changes BEFORE they break a
real run.

Tests cover:
  - SMOKE_TARGETS structure (each platform defines well-formed targets)
  - Per-target smoke: navigates, queries, picks the best matching selector
  - Navigation failure → reports the error, doesn't crash
  - Empty selector list → 0 matches, FAIL
  - First-good-selector wins (short-circuits the candidate list)
  - format_summary renders OK / FAIL lines + summary footer

We mock Playwright's Page so tests run in <1s without a browser.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.selector_check import (
    SMOKE_TARGETS,
    SelectorTarget,
    SmokeResult,
    format_summary,
    run_smoke_for_platform,
)


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Smoke target schema
# ----------------------------------------------------------------------

class TestSmokeTargetSchema:
    """Each platform's smoke targets must follow the expected shape
    so the runner can iterate without surprise."""

    def test_all_browser_platforms_have_targets(self):
        for plat in ("indeed", "dice", "ziprecruiter", "linkedin"):
            assert plat in SMOKE_TARGETS, (
                f"{plat} missing smoke targets — selectors are "
                "the actual failure mode we keep hitting"
            )

    def test_each_target_has_required_fields(self):
        for plat, targets in SMOKE_TARGETS.items():
            assert isinstance(targets, list)
            assert len(targets) >= 1, f"{plat} has no smoke targets"
            for t in targets:
                assert isinstance(t, SelectorTarget)
                assert t.name, f"{plat}: empty target name"
                assert t.url.startswith("http"), (
                    f"{plat}/{t.name}: bad URL {t.url}"
                )
                assert isinstance(t.selectors, list)
                assert len(t.selectors) >= 2, (
                    f"{plat}/{t.name}: needs ≥2 fallback selectors"
                )
                assert t.min_count >= 1


# ----------------------------------------------------------------------
# run_smoke_for_platform (with mocked Page)
# ----------------------------------------------------------------------

def _make_page(locator_counts: dict[str, int],
               navigation_raises: bool = False):
    """Build a fake Page where ``page.locator(sel).count()`` returns
    ``locator_counts[sel]`` (or 0 for unmapped selectors)."""
    page = MagicMock()
    page.wait_for_timeout = AsyncMock()
    if navigation_raises:
        page.goto = AsyncMock(side_effect=RuntimeError("net unreachable"))
    else:
        page.goto = AsyncMock()

    def make_locator(sel):
        loc = MagicMock()
        count = locator_counts.get(sel, 0)
        loc.count = AsyncMock(return_value=count)
        return loc

    page.locator = MagicMock(side_effect=make_locator)
    return page


class TestRunSmokeForPlatform:
    def test_first_matching_selector_wins(self, monkeypatch):
        """Selector candidates are tried in order. The first one
        matching ≥ min_count short-circuits the rest."""
        from auto_applier.browser import selector_check
        target = SelectorTarget(
            name="Test target",
            url="https://example.com",
            selectors=["#a", "#b", "#c"],
            min_count=2,
        )
        monkeypatch.setattr(
            selector_check, "SMOKE_TARGETS",
            {"testplat": [target]},
        )
        page = _make_page({"#a": 0, "#b": 5, "#c": 999})

        results = _run(run_smoke_for_platform("testplat", page))
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.matched == 5
        assert r.selector_used == "#b"
        # #c never queried because #b already passed
        assert page.locator.call_count == 2

    def test_no_selector_meets_min_count(self, monkeypatch):
        """All selectors return < min_count. Result is the BEST
        observed count, but ok=False."""
        from auto_applier.browser import selector_check
        target = SelectorTarget(
            name="Stale selectors",
            url="https://example.com",
            selectors=["#x", "#y"],
            min_count=10,
        )
        monkeypatch.setattr(
            selector_check, "SMOKE_TARGETS",
            {"testplat": [target]},
        )
        page = _make_page({"#x": 1, "#y": 4})

        results = _run(run_smoke_for_platform("testplat", page))
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.matched == 4
        assert r.selector_used == "#y"

    def test_navigation_failure_records_error(self, monkeypatch):
        from auto_applier.browser import selector_check
        target = SelectorTarget(
            name="bad URL",
            url="https://will-not-resolve.invalid",
            selectors=["#a", "#b"],
            min_count=1,
        )
        monkeypatch.setattr(
            selector_check, "SMOKE_TARGETS",
            {"testplat": [target]},
        )
        page = _make_page({}, navigation_raises=True)

        results = _run(run_smoke_for_platform("testplat", page))
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.error is not None
        assert "navigation failed" in r.error.lower()
        # Selectors NOT queried after navigation failure
        page.locator.assert_not_called()

    def test_unknown_platform_returns_empty(self):
        page = _make_page({})
        results = _run(run_smoke_for_platform("nonexistent_platform", page))
        assert results == []

    def test_per_selector_exception_does_not_kill_target(self, monkeypatch):
        """A locator() that raises (e.g. invalid CSS) should be
        skipped, not crash the whole target."""
        from auto_applier.browser import selector_check
        target = SelectorTarget(
            name="Mixed selectors",
            url="https://example.com",
            selectors=["bad((", "#good"],
            min_count=1,
        )
        monkeypatch.setattr(
            selector_check, "SMOKE_TARGETS",
            {"testplat": [target]},
        )

        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        def make_locator(sel):
            loc = MagicMock()
            if "bad" in sel:
                loc.count = AsyncMock(side_effect=ValueError("bad selector"))
            else:
                loc.count = AsyncMock(return_value=3)
            return loc

        page.locator = MagicMock(side_effect=make_locator)

        results = _run(run_smoke_for_platform("testplat", page))
        assert len(results) == 1
        r = results[0]
        # Bad selector skipped, good selector found
        assert r.ok is True
        assert r.matched == 3
        assert r.selector_used == "#good"


# ----------------------------------------------------------------------
# format_summary
# ----------------------------------------------------------------------

class TestFormatSummary:
    def test_all_pass_renders_clean(self):
        results = [
            SmokeResult(
                platform="indeed", target_name="Search results",
                matched=10, expected=3, selector_used=".job_card",
            ),
        ]
        text = format_summary(results)
        assert "[OK]" in text
        assert "indeed" in text
        assert ".job_card" in text
        assert "all selector smoke targets passed" in text.lower()

    def test_fail_renders_diagnostic(self):
        results = [
            SmokeResult(
                platform="dice", target_name="Search results",
                matched=0, expected=3, selector_used=None,
            ),
        ]
        text = format_summary(results)
        assert "[FAIL]" in text
        assert "dice" in text
        assert "1 target(s) failed" in text

    def test_navigation_error_renders(self):
        results = [
            SmokeResult(
                platform="ziprecruiter", target_name="Search results",
                matched=0, expected=3, selector_used=None,
                error="navigation failed: timeout",
            ),
        ]
        text = format_summary(results)
        assert "[FAIL]" in text
        assert "ziprecruiter" in text
        assert "timeout" in text

    def test_empty_results_no_summary(self):
        text = format_summary([])
        # Empty input produces empty output (no spurious "all passed")
        assert "all selector smoke" not in text.lower()
