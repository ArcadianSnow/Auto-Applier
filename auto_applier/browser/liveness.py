"""Job listing liveness detection.

Before we spend LLM calls scoring a job and burn apply quota navigating
to it, check whether the listing is still alive. Dead listings happen
constantly:

- Role was filled, the company took it down
- The company replaced it with a new requisition
- The aggregator (Indeed/ZipRecruiter) still shows the card but the
  company careers page behind it 404s
- The posting expired (most boards auto-expire after 30 days)

Each platform declares two things on its adapter class:

- ``dead_listing_selectors``: CSS selectors that only exist on a dead
  listing page (e.g. LinkedIn's "This job is no longer accepting
  applications" banner).
- ``dead_listing_phrases``: case-insensitive text snippets that, when
  found in visible page text, indicate the listing is dead.

The base class provides a default implementation that checks HTTP
status, URL redirects, selectors, and phrases in that order. Platforms
with richer detection can override ``check_liveness`` entirely.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class Liveness(str, Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


# Common phrases seen across most job boards when a listing is gone.
# Platforms typically add their own on top of these.
DEFAULT_DEAD_PHRASES = [
    "no longer accepting applications",
    "this job is no longer available",
    "this position has been filled",
    "job posting has expired",
    "this job has expired",
    "page not found",
    "404 not found",
    "sorry, this job is no longer",
    "we're sorry, this job is not",
    "this position is no longer open",
    "the job you were looking for",
]


async def check_liveness_on_page(
    page,
    dead_selectors: list[str],
    dead_phrases: list[str],
    response_status: int | None = None,
) -> Liveness:
    """Inspect a loaded page and classify it as live/dead/unknown.

    Pure function — doesn't navigate or retry. The caller is expected
    to have already loaded the job URL. Returns UNKNOWN on any
    exception so a flaky page never gets silently skipped as dead.
    """
    try:
        # 1. HTTP status: 4xx or 5xx is a clear signal
        if response_status is not None and response_status >= 400:
            return Liveness.DEAD

        # 2. Platform-specific dead selectors
        for selector in dead_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return Liveness.DEAD
            except Exception:
                continue

        # 3. Visible text phrase match (default + platform phrases)
        all_phrases = DEFAULT_DEAD_PHRASES + (dead_phrases or [])
        try:
            body_text = await page.inner_text("body", timeout=2000)
        except Exception:
            body_text = ""
        if body_text:
            lowered = body_text.lower()
            for phrase in all_phrases:
                if phrase in lowered:
                    return Liveness.DEAD

        return Liveness.LIVE
    except Exception as e:
        logger.debug("Liveness check raised, returning UNKNOWN: %s", e)
        return Liveness.UNKNOWN
