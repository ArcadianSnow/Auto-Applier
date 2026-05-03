"""Selector decay monitor — proactive DOM smoke-test per platform.

Phase 2.1 of the unified action matrix. The actual failure mode we
keep hitting on every live run is selectors breaking when a job
site updates its DOM (Indeed, Dice, ZR, Greenhouse, etc.). The
existing apply path discovers this DURING a run, after we've
spent budget and put the user in front of a half-broken flow.

This module reverses that: load a known-good URL per platform,
assert that the platform's apply / job-card selectors find at
least N expected elements, and report ``FAIL`` from doctor BEFORE
any run starts.

Architecture
------------

For each platform that defines a ``SELECTOR_SMOKE_TARGETS`` class
attribute, we:

  1. Open a fresh page (using the existing ``BrowserSession``).
  2. Navigate to each target URL.
  3. Run each ``(name, selector_list, min_count)`` tuple against
     the page.
  4. Report PASS / FAIL per target with a clear diagnostic.

The smoke tests are intentionally LOAD-TOLERANT — they don't try
to log in, fill forms, or trigger anti-bot heat. They navigate to
a public URL and read selectors. If a site has rolled out new
DOM and our selector list is stale, this catches it cleanly.

This is NOT run on the default ``cli doctor`` because:
  - It costs 5-10 seconds per platform (browser startup + navigation).
  - It hits the public web, which we don't want on every preflight.

Run on demand: ``python -m auto_applier --cli doctor --selectors``
or as a weekly cron entry.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass
class SelectorTarget:
    """One smoke-test target for a platform.

    A platform defines a list of these. Each gets navigated and
    queried independently; one failed target doesn't kill the
    others on the same platform.
    """
    name: str          # human label, e.g. "Indeed search results"
    url: str           # public URL the smoke test navigates to
    selectors: list[str]   # selector candidates to try
    min_count: int = 1     # minimum number of matches expected


@dataclass
class SmokeResult:
    platform: str
    target_name: str
    matched: int
    expected: int
    selector_used: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.matched >= self.expected


# Selector-smoke targets — one batch per browser-driven platform.
# These URLs are public listings that should always render the
# canonical job-card / apply-button DOM. We deliberately use
# generic search results (not specific job postings) because
# postings get filled and disappear weekly; search results are
# stable.
#
# Adding a new target: pick a stable public URL, list 3-5 selector
# candidates that a healthy DOM should contain, set min_count to
# the floor below which we'd want a FAIL.
SMOKE_TARGETS: dict[str, list[SelectorTarget]] = {
    "indeed": [
        SelectorTarget(
            name="Indeed search results — job cards",
            url="https://www.indeed.com/jobs?q=data+analyst&l=Remote",
            selectors=[
                "[data-testid='job-card']",
                "div.job_seen_beacon",
                "a.tapItem",
                "td.resultContent",
                "li[data-jk]",
            ],
            min_count=3,
        ),
    ],
    "dice": [
        SelectorTarget(
            name="Dice search results — job cards",
            url="https://www.dice.com/jobs?q=data+analyst",
            selectors=[
                "[data-cy='card']",
                ".search-result-card",
                "dhi-search-card",
                "[data-cy='search-result-card']",
            ],
            min_count=3,
        ),
    ],
    "ziprecruiter": [
        SelectorTarget(
            name="ZipRecruiter search results — job cards",
            url="https://www.ziprecruiter.com/jobs-search?search=data+analyst&location=Remote",
            selectors=[
                "[data-testid='job-card']",
                ".job_content",
                ".job_result",
                "article.job_result",
            ],
            min_count=3,
        ),
    ],
    "linkedin": [
        # LinkedIn discovery only — we navigate to the public jobs
        # search page, which doesn't require login. If LinkedIn has
        # shipped a major DOM change we want to know before a real
        # discovery run finds 0 cards.
        SelectorTarget(
            name="LinkedIn jobs search — job cards",
            url="https://www.linkedin.com/jobs/search?keywords=data%20analyst",
            selectors=[
                ".job-card-container",
                ".jobs-search-results__list-item",
                "li[data-occludable-job-id]",
                ".base-card",
            ],
            min_count=3,
        ),
    ],
}


async def run_smoke_for_platform(
    platform_key: str, page,
) -> list[SmokeResult]:
    """Run all smoke targets for one platform on a given Page.

    Returns one :class:`SmokeResult` per target. Errors at the
    network / navigation level produce a single error result for
    the target rather than crashing the whole run.
    """
    targets = SMOKE_TARGETS.get(platform_key, [])
    if not targets:
        return []

    results: list[SmokeResult] = []
    for target in targets:
        try:
            await page.goto(
                target.url, wait_until="domcontentloaded", timeout=15000,
            )
        except Exception as exc:
            results.append(SmokeResult(
                platform=platform_key,
                target_name=target.name,
                matched=0,
                expected=target.min_count,
                selector_used=None,
                error=f"navigation failed: {exc}",
            ))
            continue

        # Brief settle so React-rendered cards have a chance to
        # hydrate. Don't go too long here — selector decay should
        # surface fast.
        try:
            await page.wait_for_timeout(2500)
        except Exception:
            pass

        # Try each selector in order; record the first that produces
        # ≥ min_count matches.
        best_count = 0
        best_selector: str | None = None
        for sel in target.selectors:
            try:
                count = await page.locator(sel).count()
            except Exception:
                continue
            if count > best_count:
                best_count = count
                best_selector = sel
            if count >= target.min_count:
                break

        results.append(SmokeResult(
            platform=platform_key,
            target_name=target.name,
            matched=best_count,
            expected=target.min_count,
            selector_used=best_selector,
            error=None,
        ))
    return results


async def run_all_smoke_tests(
    platforms: Sequence[str] | None = None,
) -> list[SmokeResult]:
    """Run selector smoke tests for the requested platforms.

    Spins up a fresh ``BrowserSession`` for the duration of the
    smoke pass and tears it down on exit. Reuses the user's
    persistent profile so login state isn't disrupted.

    Returns a flat list of :class:`SmokeResult` across every
    target. Empty list if no smoke targets are defined for any
    requested platform.
    """
    if platforms is None:
        platforms = list(SMOKE_TARGETS.keys())

    # Filter to just the platforms with defined targets.
    requested = [p for p in platforms if p in SMOKE_TARGETS]
    if not requested:
        return []

    from auto_applier.browser.session import BrowserSession

    session = BrowserSession()
    all_results: list[SmokeResult] = []
    try:
        await session.start()
        page = session.context.pages[0] if session.context.pages else (
            await session.context.new_page()
        )
        for platform_key in requested:
            results = await run_smoke_for_platform(platform_key, page)
            all_results.extend(results)
    finally:
        try:
            await session.stop()
        except Exception:
            pass
    return all_results


def format_summary(results: list[SmokeResult]) -> str:
    """Render results as a printable diagnostic block."""
    lines: list[str] = []
    failures = 0
    for r in results:
        if r.error:
            failures += 1
            lines.append(
                f"  [FAIL] {r.platform:14s}  {r.target_name}  "
                f"— {r.error}"
            )
            continue
        if not r.ok:
            failures += 1
            sel_hint = f" (best selector: {r.selector_used})" if r.selector_used else ""
            lines.append(
                f"  [FAIL] {r.platform:14s}  {r.target_name}  "
                f"— matched {r.matched}/{r.expected}{sel_hint}"
            )
            continue
        lines.append(
            f"  [OK]   {r.platform:14s}  {r.target_name}  "
            f"— matched {r.matched} via {r.selector_used}"
        )
    if failures:
        lines.append("")
        lines.append(
            f"  {failures} target(s) failed. Selectors likely decayed; "
            "expect run breakage on the affected platform(s)."
        )
    elif results:
        lines.append("")
        lines.append("  All selector smoke targets passed.")
    return "\n".join(lines)
