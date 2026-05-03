"""LinkedIn discovery via the Nodriver anti-detect backend.

Parallel implementation of :class:`LinkedInPlatform` using Nodriver
(raw CDP, no Playwright wire protocol) instead of patchright. Per
the 2026-05-03 Tier 4 research pass, this is the most promising
candidate for sneaking past LinkedIn's TLS/JA4 fingerprinting.

Discovery-only — same posture as the patchright LinkedIn adapter.
We scrape search results, hand titles + companies + descriptions
to the scorer, and surface matches via ``cli almost``. Apply path
remains manual until we have a confirmed-working backend AND
agreed user consent (LinkedIn's ToS posture in 2026 is hostile).

Configuration
-------------

In ``data/user_config.json``::

    {
      "enabled_platforms": ["linkedin_nodriver"],
      "linkedin_nodriver": {
        "headless": false,        // optional, default false
        "max_pages": 3,           // optional, mirrors the patchright cap
        "max_jobs_per_search": 25
      }
    }

Either ``linkedin`` (patchright) or ``linkedin_nodriver`` should be
enabled at any one time — running both opens two browsers and
consumes 2× the LinkedIn rate-limit budget for the same set of
listings.

Why discovery-only
------------------

Even with a working anti-detect backend, applying through LinkedIn
would expose users to a temporary review/ban (community evidence:
"Helping People Get Jobs = Banned by LinkedIn" / 2026-04 nubela
class action). We get most of the value from discovery alone —
the user sees a ranked list of matches and clicks through to apply
manually, the same way they'd use LinkedIn anyway.

If discovery via Nodriver proves stable across multi-day testing,
a future commit can re-evaluate enabling the apply path behind a
second opt-in flag.

State of this scaffold
----------------------

This adapter is **scaffolding** as of 2026-05-03. It implements:
  - Lazy nodriver import with a clear ImportError remediation
  - Session lifecycle wired to the engine via discovery_only path
  - Search-results URL construction + page open
  - Job card extraction via Chrome's runtime.evaluate (no Playwright
    selectors — Nodriver doesn't have them)

It does NOT yet implement:
  - Real-world LinkedIn HTML resilience (tested against fixtures
    only; LinkedIn rotates classnames frequently)
  - Pagination beyond the first results page
  - Sophisticated anti-detect (Bezier mouse, dwell time, scroll)
  - The apply path (intentionally — see "Why discovery-only")

A real validation pass needs a logged-in LinkedIn account and a
patient operator; we ship the scaffold so the validation work
isn't blocked on infrastructure.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote_plus

from auto_applier.browser.base_platform import JobPlatform
from auto_applier.browser.nodriver_session import (
    NodriverSession,
    is_nodriver_available,
)
from auto_applier.storage.models import ApplyResult, Job

logger = logging.getLogger(__name__)


# Reasonable defaults; mirror the patchright LinkedIn adapter so
# behaviour stays comparable when users switch backends.
DEFAULT_MAX_SEARCH_PAGES = 3
DEFAULT_MAX_JOBS_PER_SEARCH = 25


class LinkedInNodriverPlatform(JobPlatform):
    source_id = "linkedin_nodriver"
    display_name = "LinkedIn (Nodriver)"

    discovery_only = True
    discovery_only_reason = (
        "LinkedIn discovery via Nodriver — scoring only. Open each "
        "match in your normal browser to apply manually. See: cli almost."
    )

    captcha_url_patterns = [
        "/checkpoint/challenge",
        "/uas/login-submit",
        "/captcha",
    ]

    def __init__(self, context, config: dict, form_filler=None) -> None:
        # Accept ``context`` to satisfy the engine's wiring contract
        # but ignore it — Nodriver runs an independent browser.
        super().__init__(context, config, form_filler)
        self._session: NodriverSession | None = None

        platform_cfg = config.get(self.source_id, {}) or {}
        self.max_pages = int(
            platform_cfg.get("max_pages", DEFAULT_MAX_SEARCH_PAGES)
        )
        self.max_jobs_per_search = int(
            platform_cfg.get("max_jobs_per_search", DEFAULT_MAX_JOBS_PER_SEARCH)
        )
        self.headless = bool(platform_cfg.get("headless", False))

    # ------------------------------------------------------------------
    # JobPlatform contract
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """Start Nodriver, navigate to LinkedIn feed, wait for login.

        Re-uses ``NODRIVER_PROFILE_DIR`` so a previously-logged-in
        session survives. If the user isn't logged in we open the
        login page and wait — never automate credential entry, same
        rule as every other adapter.
        """
        if not is_nodriver_available():
            logger.warning(
                "Nodriver not installed. linkedin_nodriver disabled. "
                "Install with: pip install -e \".[nodriver]\""
            )
            return False

        try:
            await self._ensure_session()
        except ImportError as exc:
            logger.warning("Nodriver session start failed: %s", exc)
            return False
        except Exception as exc:
            logger.warning("Nodriver session start raised: %s", exc)
            return False

        # Navigate to feed — the cheapest signal of "logged in".
        try:
            tab = await self._session.new_tab("https://www.linkedin.com/feed/")
            # Give the page a moment to either render the feed or
            # redirect to login. We don't poll aggressively here;
            # in practice Nodriver renders much faster than full
            # patchright + stealth so 3s is enough.
            await asyncio.sleep(3.0)
            try:
                cur_url = await tab.evaluate("window.location.href")
            except Exception:
                cur_url = ""
            if "/login" in (cur_url or "") or "/checkpoint" in (cur_url or ""):
                logger.warning(
                    "LinkedIn (Nodriver): user not logged in. Manual "
                    "login required — close any login interstitial in "
                    "the browser window and re-run."
                )
                # Discovery-only fail: returning False causes the
                # engine to record platform_login_failed and move on
                # to the next platform without aborting the whole run.
                return False
        except Exception as exc:
            logger.warning("LinkedIn (Nodriver): feed-check failed: %s", exc)
            return False

        return True

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        if self._session is None or not self._session.started:
            return []
        url = self._build_search_url(keyword, location)
        logger.info("LinkedIn (Nodriver): GET %s", url)
        try:
            tab = await self._session.new_tab(url)
        except Exception as exc:
            logger.warning(
                "LinkedIn (Nodriver): search navigation failed: %s", exc,
            )
            return []
        # Let LinkedIn's React tree settle. LinkedIn loads job cards
        # progressively; 4-5s catches the first paint reliably for
        # most search queries.
        await asyncio.sleep(4.0)

        try:
            raw_cards = await tab.evaluate(
                _JOB_CARDS_EXTRACT_JS, return_by_value=True,
            )
        except Exception as exc:
            logger.warning(
                "LinkedIn (Nodriver): job-card extract failed: %s", exc,
            )
            return []
        if not isinstance(raw_cards, list):
            return []

        jobs: list[Job] = []
        for card in raw_cards[: self.max_jobs_per_search]:
            try:
                job = self._parse_card(card, keyword)
            except Exception as exc:
                logger.debug(
                    "LinkedIn (Nodriver): skipping malformed card: %s", exc,
                )
                continue
            if job is not None:
                jobs.append(job)

        logger.info(
            "LinkedIn (Nodriver): parsed %d job(s) for '%s'",
            len(jobs), keyword,
        )
        return jobs

    async def get_job_description(self, job: Job) -> str:
        # Discovery-only: descriptions are NOT navigated to. Returning
        # the empty string makes the engine fall through to its
        # title+company pseudo-description path — same as the
        # patchright LinkedIn adapter.
        return job.description or ""

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        return ApplyResult(
            success=False,
            failure_reason=self.discovery_only_reason,
            requires_manual_apply=True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = NodriverSession(headless=self.headless)
        if not self._session.started:
            await self._session.start()

    def _build_search_url(self, keyword: str, location: str) -> str:
        # LinkedIn's public job-search endpoint. ``f_TPR=r86400`` would
        # filter to last-24h postings; we omit it deliberately so
        # discovery surfaces older listings the user might have missed.
        kw = quote_plus(keyword or "")
        loc = quote_plus(location or "")
        return (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={kw}&location={loc}"
        )

    def _parse_card(self, raw: dict[str, Any], keyword: str) -> Job | None:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not company:
            return None
        # Try the data-attribute job id first (most stable across
        # LinkedIn DOM variants), then fall back to extracting the
        # numeric id from the URL path.
        job_id = (raw.get("jobId") or "").strip()
        if not job_id and url:
            m = re.search(r"/jobs/view/(\d+)", url)
            if m:
                job_id = m.group(1)
        if not job_id:
            return None
        # LinkedIn's job_id namespace is global across the platform,
        # but to keep our cross-source dedup robust we still namespace
        # by adapter source so a future re-introduction of a different
        # LinkedIn adapter can't step on these rows.
        return Job(
            job_id=f"linkedin_nd_{job_id}",
            title=title,
            company=company,
            url=url or f"https://www.linkedin.com/jobs/view/{job_id}/",
            source=self.source_id,
            search_keyword=keyword,
        )

    async def stop(self) -> None:
        """Close the Nodriver browser. Called by tests; the engine
        doesn't currently teardown adapters per-run, but this gives
        us a clean handle when we wire in a continuous-run cleanup
        in a future commit.
        """
        if self._session is not None:
            await self._session.stop()
            self._session = None


# ----------------------------------------------------------------------
# JS extractor — runs inside the LinkedIn page context.
# ----------------------------------------------------------------------
#
# Multiple selector fallbacks because LinkedIn rotates classnames at
# whim. We collect everything that LOOKS like a job card and let the
# Python side filter by the presence of title + company.
#
# Returns a JSON-serializable list[dict[str, str]] — one entry per
# matched card, with title / company / url / jobId fields.
_JOB_CARDS_EXTRACT_JS = """
(() => {
  const out = [];
  // Card container fallbacks, ordered by specificity
  const cardSelectors = [
    'li[data-occludable-job-id]',
    'div.job-card-container',
    'li.jobs-search-results__list-item',
    'li.scaffold-layout__list-item',
    '[data-test-id="job-card-container"]',
  ];
  let cards = [];
  for (const sel of cardSelectors) {
    cards = Array.from(document.querySelectorAll(sel));
    if (cards.length > 0) break;
  }
  for (const card of cards) {
    try {
      // jobId — prefer data-occludable-job-id (stable across
      // 2024-2026), then data-job-id, then anchor href.
      let jobId =
        card.getAttribute('data-occludable-job-id') ||
        card.getAttribute('data-job-id') ||
        '';
      // Title link
      const titleEl =
        card.querySelector('a.job-card-container__link') ||
        card.querySelector('.job-card-list__title a') ||
        card.querySelector('a.job-card-list__title--link') ||
        card.querySelector('a[data-control-name="jobPosting_title"]') ||
        card.querySelector('a[href*="/jobs/view/"]');
      const title = titleEl ? (titleEl.innerText || '').trim() : '';
      const url = titleEl ? titleEl.href : '';
      // Try to derive jobId from URL when attribute is absent.
      if (!jobId && url) {
        const m = url.match(/\\/jobs\\/view\\/(\\d+)/);
        if (m) jobId = m[1];
      }
      // Company name
      const companyEl =
        card.querySelector('.job-card-container__primary-description') ||
        card.querySelector('.job-card-container__company-name') ||
        card.querySelector('.artdeco-entity-lockup__subtitle') ||
        card.querySelector('.job-card-list__company-name');
      const company = companyEl ? (companyEl.innerText || '').trim() : '';
      if (title && company) {
        out.push({ title, company, url, jobId });
      }
    } catch (_) {
      // Skip malformed card silently — we have other cards to try.
    }
  }
  return out;
})();
""".strip()
