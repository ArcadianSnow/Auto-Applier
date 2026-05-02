"""Dice platform adapter for job search and Easy Apply automation.

This adapter handles:
- Manual login detection and prompting
- Job search with Easy Apply filter
- Job card parsing from search results
- Easy Apply modal walking (multi-step forms)
- Form field filling via FormFiller
- CAPTCHA detection and hard stop
- External ATS redirect detection (new tab opens)

IMPORTANT: Dice changes its DOM frequently. Every selector here has
multiple fallbacks. When selectors break, add new ones to the lists --
do not remove old ones (they may still work for some users).
"""
import asyncio
import logging
import random
import re
from urllib.parse import quote_plus

from playwright.async_api import Page

from auto_applier.browser.anti_detect import (
    human_click,
    human_scroll,
    random_delay,
    reading_pause,
    simulate_organic_behavior,
)
from auto_applier.browser.base_platform import CaptchaDetectedError, JobPlatform
from auto_applier.browser.selector_utils import find_form_fields
from auto_applier.storage.models import ApplyResult, Job

logger = logging.getLogger(__name__)


# ── Selector Groups (multiple fallbacks for DOM changes) ─────────────

# Indicators that the user is logged in.
#
# IMPORTANT: These selectors need to match ONLY when logged in, not
# on Dice's signup / login pages. Generic href selectors like
# a[href*='/dashboard'] and a[href*='/profile'] are BANNED here
# because Dice's signup pages have "Already have an account? Go to
# dashboard" links that match these selectors and cause false
# positives, leading the tool to walk the signup form as if it were
# an apply form.
LOGGED_IN_SELECTORS = [
    "[data-cy='user-menu']",
    "[data-cy='header-user-menu']",
    ".user-menu",
    ".header-user-info",
    "img.user-avatar",
    ".avatar-img",
    "[aria-label='User Menu']",
    # Header avatar image with candidate initials — present only
    # when the user is logged in
    "img[alt*='avatar']",
    "button[aria-label*='account menu' i]",
]

# Job card selectors on the search results page. Newest Dice layout
# selectors at the top, historical ones kept as fallbacks so old
# profiles keep working.
JOB_CARD_SELECTORS = [
    # 2024+ layouts — the card container wraps the title link,
    # company, location, and apply button. Never put a link/title
    # selector here: _parse_single_card searches INSIDE the card
    # for the title, so the card must be the container, not the link.
    "div[data-testid='job-search-serp-card']",
    "div[data-cy='card']",
    "dhi-search-card",
    # Parent of the title link — if the known containers miss but
    # the title link still exists, walk up to its nearest <div>
    # parent to get a card-like scope.
    "div:has(> a[data-testid='job-search-job-detail-link'])",
    # Historical
    "[data-cy='search-card']",
    ".search-card",
    ".card.search-card",
    "[data-testid='search-card']",
    "div.search-result-card",
    ".job-search-card",
]

# Job title within a card
JOB_TITLE_SELECTORS = [
    # 2024+
    "a[data-testid='job-search-job-detail-link']",
    "a[data-cy='card-title-link']",
    "h5 a[data-cy='card-title-link']",
    # Historical
    "a.card-title-link",
    "[data-cy='card-title-link']",
    "h5 a.card-title-link",
    ".card-title-link",
    "a[data-cy='card-title']",
    ".search-card-title a",
    "h5.card-title a",
]

# Company name within a card
JOB_COMPANY_SELECTORS = [
    "a[data-cy='search-result-company-name']",
    "[data-cy='search-result-company-name']",
    ".card-company a",
    ".card-company",
    "a[data-cy='company-name']",
    ".search-card-company",
    "[data-testid='company-name']",
    "span.company-name",
]

# Easy Apply button on job detail page
EASY_APPLY_BUTTON_SELECTORS = [
    # Current (2026) — Dice uses <a data-testid="apply-button">
    "a[data-testid='apply-button']",
    "[data-testid='apply-button']",
    # Legacy selectors kept for backwards compatibility
    "[data-cy='apply-button-wc']",
    "apply-button-wc",
    "button.btn-apply",
    "[data-cy='apply-button']",
    "a.apply-button",
    "button[data-cy='apply-btn']",
    ".apply-button button",
    "button.seds-button-primary[data-cy*='apply']",
    "dhi-wc-apply-button button",
]

# Job description on the detail page
JOB_DESCRIPTION_SELECTORS = [
    ".job-description",
    "[data-cy='jobDescription']",
    "[data-testid='jobDescription']",
    "#jobDescription",
    ".job-details__description",
    "div.job-description-container",
    "[class*='jobDescription']",
]

# Form elements in the Easy Apply modal
FORM_FIELD_SELECTORS = [
    "[data-cy*='form']",
    ".form-group",
    ".seds-form-group",
    "fieldset",
    ".apply-form-field",
    ".form-field",
]

# Navigation buttons in the Easy Apply modal
MODAL_NEXT_SELECTORS = [
    # Current (2026)
    "button[data-testid='next-button']",
    "button[data-testid='submit-next']",
    # Legacy
    "button[data-cy='next-button']",
    "button.btn-next",
    "button.seds-button-primary",
    "[data-cy='submit-next']",
    "button[type='submit']",
    "button.btn-primary",
    # Text-based fallbacks
    "button:has-text('Next')",
    "button:has-text('Continue')",
]

MODAL_SUBMIT_SELECTORS = [
    # Current (2026)
    "button[data-testid='submit-application']",
    "button[data-testid='submit-button']",
    "button[data-testid='apply-button']",
    # Legacy
    "button[data-cy='submit-application']",
    "button[data-cy='submit-button']",
    "button.btn-submit",
    "button[aria-label*='Submit']",
    "button[type='submit']",
    "button.seds-button-primary",
    # Text-based fallbacks
    "button:has-text('Submit')",
    "button:has-text('Apply')",
    "button:has-text('Submit Application')",
]

MODAL_CLOSE_SELECTORS = [
    "button[data-cy='close-modal']",
    "button.btn-close",
    "button[aria-label='Close']",
    ".modal-header button.close",
    "button.seds-modal-close",
    "[data-dismiss='modal']",
]

# Resume upload input
RESUME_UPLOAD_SELECTORS = [
    "input[type='file'][data-cy*='resume']",
    "input[type='file'][name*='resume']",
    "input[type='file'][accept*='.pdf']",
    "input[type='file'][id*='resume']",
    "input[type='file']",
]

# Application success indicators
SUCCESS_SELECTORS = [
    "[data-cy='application-success']",
    "[data-testid='application-success']",
    ".application-success",
    ".apply-success",
    ".success-message",
    "[class*='success']",
]

# Max pagination and result limits
MAX_SEARCH_PAGES = 3
MAX_JOBS_PER_SEARCH = 25
MAX_MODAL_STEPS = 8  # Safety limit for multi-step modals


class DicePlatform(JobPlatform):
    """Dice job search and Easy Apply adapter.

    Requires the user to be logged in manually -- this adapter will
    never automate credential entry. It navigates to Dice, checks
    for login indicators, and waits for the user if needed.
    """

    source_id = "dice"
    display_name = "Dice"

    dead_listing_selectors = [
        "[data-cy='job-closed-notice']",
        ".job-closed",
        ".expired-job",
    ]
    dead_listing_phrases = [
        "this job is no longer available",
        "this position is no longer open",
        "job has been closed",
    ]

    captcha_url_patterns = [
        "/captcha",
        "/recaptcha",
        "/challenge",
    ]

    # check_is_external intentionally NOT overridden for Dice.
    #
    # Dice's apply button is a Web Component (<apply-button-wc>)
    # that hydrates asynchronously and lives inside shadow DOM.
    # Every attempt to detect it during fetch_description
    # (instant query_selector, safe_query with 3s timeout) failed
    # and falsely marked 100% of jobs as external. The base
    # platform's default (return False) is correct here —
    # apply_to_job already handles missing buttons with a 5s
    # timeout and a descriptive failure_reason.

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    # URL substring patterns indicating we're on a Dice auth page
    # (NOT logged in). When the URL contains any of these, the user
    # is on a sign-in / sign-up flow regardless of what's in the DOM.
    _AUTH_URL_PATTERNS = (
        "/login", "/register", "/signup", "/sign-up",
        "/auth", "/account/create", "/create-account",
    )

    async def ensure_logged_in(self) -> bool:
        """Navigate to Dice and verify the user is logged in.

        Uses a URL-first detection strategy: the user is logged in
        when they're on dice.com but NOT on an auth page. Selectors
        are too fragile on their own — Dice's logged-in DOM uses
        different markup across page templates, and popups often
        intercept early DOM checks. URL-based detection is robust
        across all of these.
        """
        page = await self.get_page()

        # Navigate to Dice home to check login state
        await page.goto("https://www.dice.com/", wait_until="domcontentloaded")
        await random_delay(2.0, 4.0)

        if await self._dice_url_indicates_logged_in(page):
            logger.info("Dice: Already logged in (URL=%s)", page.url)
            return True

        # Not logged in -- navigate to login page
        logger.info(
            "Dice: Not logged in. Navigating to login page for manual login..."
        )
        await page.goto(
            "https://www.dice.com/dashboard/login", wait_until="domcontentloaded"
        )

        return await self._wait_for_dice_login(page, timeout=300)

    async def _dice_url_indicates_logged_in(self, page: Page) -> bool:
        """True when the page is on dice.com and NOT on an auth page.

        Tries to dismiss any common popups first so subsequent flows
        (apply click) aren't blocked. Popup dismissal is best-effort.
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            return False

        if "dice.com" not in url:
            return False
        if any(pat in url for pat in self._AUTH_URL_PATTERNS):
            return False

        # We're on a dice.com page that isn't an auth page — almost
        # certainly logged in. Dismiss any popups before proceeding.
        await self._dismiss_common_popups(page)
        return True

    async def _wait_for_dice_login(self, page: Page, timeout: int = 300) -> bool:
        """Custom polling loop for Dice login completion.

        Replaces the generic wait_for_manual_login to use URL-based
        detection. Polls every 2 seconds for ``timeout`` seconds.
        Logs progress so the user sees the tool isn't stuck. Fires
        a Windows toast on entry so the user notices even if their
        focus is on a different window.
        """
        import time
        from auto_applier.notify import notify_user
        logger.info(
            "Waiting for manual login on Dice (URL-based, timeout=%ds)...",
            timeout,
        )
        # Defer the urgent notify — Dice often auto-resolves login
        # within a couple seconds via stored cookies. Only beep if
        # the user actually has to do something. Polling loop below
        # checks at 5s mark.
        notify_grace_s = 5.0
        notified = False
        start = time.monotonic()
        last_log_url = ""
        while time.monotonic() - start < timeout:
            if await self._dice_url_indicates_logged_in(page):
                logger.info(
                    "Dice: Login detected (URL=%s)", page.url,
                )
                return True
            # Periodic visibility into what URL we're sitting on
            try:
                cur_url = page.url
                if cur_url != last_log_url:
                    logger.debug("Dice login wait: still at %s", cur_url)
                    last_log_url = cur_url
            except Exception:
                pass
            # Deferred urgent notify — fire once, after the grace
            # window confirms this isn't a stored-cookie auto-resolve.
            if not notified and (time.monotonic() - start) >= notify_grace_s:
                notified = True
                notify_user(
                    "Auto Applier — Dice login needed",
                    "Dice is asking you to log in before applying. "
                    f"Complete the sign-in in the open browser window "
                    f"within {timeout}s.",
                    urgent=True,
                )
            await asyncio.sleep(2.0)

        logger.warning("Dice: Manual login timed out after %ds", timeout)
        return False

    async def _dismiss_common_popups(self, page: Page) -> None:
        """Best-effort dismissal of common popups that block apply flow.

        Dice and the wider web throw a lot of overlays at logged-in
        users — cookie banners, "tour the dashboard" walkthroughs,
        notification permission requests. Any of these can intercept
        clicks on the actual page below. We try a short list of
        common close-button selectors and silently ignore failures —
        a popup we can't close is one we report as a failure later.
        """
        dismiss_selectors = [
            "button[aria-label='Close']",
            "button[aria-label*='close' i]",
            "button[aria-label*='dismiss' i]",
            "button[data-cy*='close']",
            "button[data-testid*='close']",
            ".modal-close",
            ".onetrust-close-btn-handler",  # Cookie banner close
            "#onetrust-accept-btn-handler",  # Cookie accept
            "button:has-text('Accept all')",
            "button:has-text('No thanks')",
            "button:has-text('Maybe later')",
            "button:has-text('Got it')",
            "button:has-text('Skip')",
            "button:has-text('Dismiss')",
        ]
        for sel in dismiss_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible():
                            await el.click(timeout=1000)
                            logger.debug("Dismissed popup via %s", sel)
                            await asyncio.sleep(0.3)
                    except Exception:
                        continue
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Job Search
    # ------------------------------------------------------------------

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search Dice Jobs and return job cards from the results.

        Uses a minimal search URL — the older filters.postedDate=SEVEN
        and filters.easyApply=true combo filtered out real matches
        aggressively, and Dice's current site renders zero results
        with them active on most queries. Scoring + _is_external_apply
        downstream still filter non-applicable jobs at click time.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

        # Warm-up via homepage before the search URL, same pattern
        # as the other platforms.
        try:
            if "dice.com" not in page.url.lower():
                logger.info("Dice: warm-up via homepage before search")
                await page.goto(
                    "https://www.dice.com/",
                    wait_until="domcontentloaded",
                )
                await reading_pause(page)
                await simulate_organic_behavior(page)
        except Exception as exc:
            logger.debug("Dice warm-up skipped: %s", exc)

        jobs: list[Job] = []
        encoded_kw = quote_plus(keyword)
        encoded_loc = quote_plus(location)

        for page_num in range(MAX_SEARCH_PAGES):
            # Dice uses 1-based page numbering
            search_url = (
                f"https://www.dice.com/jobs"
                f"?q={encoded_kw}"
                f"&location={encoded_loc}"
                f"&page={page_num + 1}"
            )

            logger.info(
                "Dice: Searching page %d -- %s %s",
                page_num + 1,
                keyword,
                location,
            )
            await page.goto(search_url, wait_until="domcontentloaded")
            await reading_pause(page)
            await self.check_and_abort_on_captcha(page)

            # Parse job cards from this page
            page_jobs = await self._parse_job_cards(page, keyword)
            jobs.extend(page_jobs)

            if len(jobs) >= MAX_JOBS_PER_SEARCH:
                break

            # Check if there are more pages
            if not page_jobs:
                logger.info("Dice: No more job cards found, stopping pagination")
                break

            # Organic delay between pages
            await simulate_organic_behavior(page)
            await random_delay(3.0, 6.0)

        logger.info(
            "Dice: Found %d jobs for '%s' in '%s'", len(jobs), keyword, location
        )
        return jobs[:MAX_JOBS_PER_SEARCH]

    async def _parse_job_cards(self, page: Page, keyword: str) -> list[Job]:
        """Parse job cards from the current search results page."""
        jobs: list[Job] = []

        # Scroll down to load lazy-loaded cards
        for _ in range(random.randint(2, 4)):
            await human_scroll(page, "down", random.randint(300, 500))
            await asyncio.sleep(random.uniform(0.5, 1.5))

        # Find all job card containers
        cards = []
        for sel in JOB_CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
                if cards:
                    logger.debug("Found %d job cards via '%s'", len(cards), sel)
                    break
            except Exception:
                continue

        if not cards:
            # CSS selectors missed. Fall back to anchor-based finder.
            logger.info(
                "Dice: no CSS selectors matched, trying anchor-based "
                "fallback for /job-detail/ links"
            )
            anchor_hits = await self.find_jobs_by_anchors(
                page, href_pattern="/job-detail/",
            )
            if not anchor_hits:
                # Dice also uses /jobs/ and /jobid paths on some layouts
                anchor_hits = await self.find_jobs_by_anchors(
                    page, href_pattern="/jobs/",
                )
            if anchor_hits:
                logger.info(
                    "Dice: anchor fallback recovered %d jobs",
                    len(anchor_hits),
                )
                for title, url in anchor_hits:
                    jobs.append(Job(
                        job_id=f"dice-{abs(hash(url)) % 10**10}",
                        title=title,
                        company="",
                        url=url,
                        search_keyword=keyword,
                        source=self.source_id,
                    ))
                return jobs

            try:
                current_url = page.url
                title = await page.title()
                body = await page.inner_text("body")
            except Exception:
                current_url = "?"
                title = "?"
                body = ""
            snippet = body.strip()[:400].replace("\n", " | ")
            logger.warning(
                "Dice: 0 job cards found and anchor fallback came up "
                "empty too.\n"
                "  url=%s\n"
                "  title=%s\n"
                "  page snippet: %s",
                current_url, title, snippet,
            )
            body_lower = body.lower()
            if "no results" in body_lower or "no jobs found" in body_lower:
                logger.warning(
                    "Dice is telling us zero jobs match the search — "
                    "try broader keywords or location."
                )
            elif "verify" in body_lower or "unusual" in body_lower:
                logger.warning(
                    "Dice page contains 'verify' / 'unusual' text — may "
                    "need a manual captcha solve in the browser window."
                )
            return jobs

        for card in cards:
            try:
                job = await self._parse_single_card(card, page, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug("Failed to parse a job card: %s", exc)
                continue

        # Cards were detected but per-card parsing returned zero jobs
        # — this happens when the "card" selector matches anchor
        # links directly instead of wrapper divs, so titles/companies
        # aren't inside each element. Fall back to anchor-based finder.
        if not jobs and cards:
            logger.info(
                "Dice: %d cards detected but none parsed, falling back "
                "to anchor-based finder", len(cards),
            )
            anchor_hits = await self.find_jobs_by_anchors(
                page, href_pattern="/job-detail/",
            )
            if not anchor_hits:
                anchor_hits = await self.find_jobs_by_anchors(
                    page, href_pattern="/jobs/",
                )
            for title, url in anchor_hits:
                jobs.append(Job(
                    job_id=f"dice-{abs(hash(url)) % 10**10}",
                    title=title,
                    company="",
                    url=url,
                    search_keyword=keyword,
                    source=self.source_id,
                ))
            if anchor_hits:
                logger.info(
                    "Dice: anchor fallback recovered %d jobs",
                    len(anchor_hits),
                )

        return jobs

    async def _parse_single_card(
        self, card, page: Page, keyword: str
    ) -> Job | None:
        """Extract job data from a single job card element."""
        # Get title
        title = ""
        for sel in JOB_TITLE_SELECTORS:
            try:
                title_el = await card.query_selector(sel)
                if title_el:
                    title = (await title_el.inner_text()).strip()
                    break
            except Exception:
                continue

        if not title:
            return None

        # Get company — try inside the card first, then walk up to
        # the parent container if the card selector is too narrow
        # (happens when div:has(> a[...]) matched a minimal wrapper).
        company = ""
        for sel in JOB_COMPANY_SELECTORS:
            try:
                company_el = await card.query_selector(sel)
                if company_el:
                    company = (await company_el.inner_text()).strip()
                    break
            except Exception:
                continue
        if not company:
            # Try the card's parent — the narrow wrapper matched by
            # 'div:has(> a[data-testid=...])' often sits inside a
            # larger card container that has the company element.
            try:
                parent = await card.evaluate_handle("el => el.parentElement")
                if parent:
                    for sel in JOB_COMPANY_SELECTORS:
                        try:
                            company_el = await parent.query_selector(sel)
                            if company_el:
                                company = (await company_el.inner_text()).strip()
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        # Get job URL / ID
        job_url = ""
        job_id = ""

        # Try the title link which typically contains /job-detail/{id}
        for sel in JOB_TITLE_SELECTORS:
            try:
                link_el = await card.query_selector(sel)
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    if href:
                        job_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.dice.com{href}"
                        )
                        # Extract job ID from /job-detail/{id} pattern
                        match = re.search(r"/job-detail/([a-f0-9-]+)", href)
                        if match:
                            job_id = f"dice-{match.group(1)}"
                        break
            except Exception:
                continue

        # Fallback: try data attributes on the card
        if not job_id:
            try:
                data_id = await card.get_attribute("data-id")
                if data_id:
                    job_id = f"dice-{data_id}"
            except Exception:
                pass

        if not job_id:
            try:
                data_id = await card.get_attribute("id")
                if data_id:
                    job_id = f"dice-{data_id}"
            except Exception:
                pass

        if not job_id:
            # Generate a fallback ID from title + company
            job_id = f"dice-{hash(title + company) % 10**8}"

        return Job(
            job_id=job_id,
            title=title,
            company=company,
            url=job_url,
            search_keyword=keyword,
            source=self.source_id,
        )

    # ------------------------------------------------------------------
    # Job Description
    # ------------------------------------------------------------------

    async def get_job_description(self, job: Job) -> str:
        """Navigate to the job detail page and extract the description."""
        page = await self.get_page()

        if job.url:
            await page.goto(job.url, wait_until="domcontentloaded")
        else:
            logger.warning(
                "Dice: No URL for job %s, cannot fetch description", job.job_id
            )
            return ""

        await reading_pause(page)
        await self.check_and_abort_on_captcha(page)

        # Extract description text
        description = await self.safe_get_text(
            page, JOB_DESCRIPTION_SELECTORS, timeout=5000
        )

        if not description:
            # CSS selectors missed — Dice changes description container
            # classes frequently. Use a JS-based fallback that searches
            # for elements with JD-like headings or the largest text
            # block that doesn't look like navigation chrome.
            try:
                description = await page.evaluate("""() => {
                    // Strategy 1: find element with JD-like headings
                    const jdHeadings = /^(about|overview|description|responsibilities|qualifications|requirements|who we|what you|the role|position|summary|job purpose)/i;
                    const allEls = document.querySelectorAll('div, section, article');
                    for (const el of allEls) {
                        const text = (el.innerText || '').trim();
                        if (text.length > 200 && text.length < 15000 && jdHeadings.test(text)) {
                            return text.substring(0, 8000);
                        }
                    }

                    // Strategy 2: largest text block that isn't nav chrome
                    const candidates = [...allEls].filter(el => {
                        const text = (el.innerText || '').trim();
                        if (text.length < 200 || text.length > 15000) return false;
                        if (/\\d+ .*(jobs|results)/i.test(text.substring(0, 100))) return false;
                        const h2Count = el.querySelectorAll('h2').length;
                        if (h2Count > 3) return false;
                        return true;
                    }).sort((a, b) => b.innerText.length - a.innerText.length);

                    return candidates.length > 0 ? candidates[0].innerText.substring(0, 8000) : '';
                }""")
                if description:
                    logger.debug(
                        "Dice: extracted description via JS fallback (%d chars)",
                        len(description),
                    )
            except Exception:
                pass

        if description:
            logger.debug(
                "Dice: Got description for %s (%d chars)",
                job.job_id,
                len(description),
            )
        else:
            # Diagnostic: what IS on the page?
            try:
                snippet = await page.evaluate("""() => {
                    const candidates = [...document.querySelectorAll(
                        'section, article, [class*=description], [class*=detail], [role=main], main'
                    )].filter(el => el.innerText.length > 200)
                     .sort((a,b) => b.innerText.length - a.innerText.length);
                    if (candidates.length > 0) {
                        const el = candidates[0];
                        return 'largest text block (' + el.tagName + '.' +
                               el.className.substring(0,80) + '): ' +
                               el.innerText.substring(0, 300);
                    }
                    return 'no large text blocks found on page';
                }""")
                logger.info(
                    "Dice: description diagnostic for %s: %s",
                    job.job_id, snippet,
                )
            except Exception:
                pass
            logger.warning(
                "Dice: Could not extract description for %s", job.job_id
            )

        return description

    # ------------------------------------------------------------------
    # Easy Apply
    # ------------------------------------------------------------------

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """Walk the Dice Easy Apply modal and fill all form fields.

        In dry_run mode, fills fields and navigates steps but does not
        click the final Submit button. Detects external ATS redirects
        (new tabs opening) and skips those jobs.
        """
        page = await self.get_page()

        try:
            # Navigate to the job if needed
            if job.url and job.url not in page.url:
                await page.goto(job.url, wait_until="domcontentloaded")
                await reading_pause(page)

            await self.check_and_abort_on_captcha(page)

            # Record current page count to detect new tabs
            pages_before = len(self.context.pages)
            try:
                url_before_click = page.url
            except Exception:
                url_before_click = ""

            # Pre-click diagnostic: dump the apply-button outerHTML so
            # the log shows exactly what we're about to click. The
            # 2026-05-02 live run produced "0 form fields, no modal,
            # no new tab, body still says Apply Now" — which means
            # the click reported success but didn't trigger Dice's
            # apply flow. Most likely we matched a non-button element
            # (an outer wrapper) or the click is being eaten by a
            # bot-detection guard. This dump tells us which.
            try:
                btn_diag = await page.evaluate(
                    """(selectors) => {
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.offsetParent !== null) {
                                return {
                                    selector: sel,
                                    tag: el.tagName,
                                    text: (el.innerText || '').substring(0, 80),
                                    href: el.getAttribute('href') || '',
                                    target: el.getAttribute('target') || '',
                                    testid: el.getAttribute('data-testid') || '',
                                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                                    outer: (el.outerHTML || '').substring(0, 400),
                                };
                            }
                        }
                        return null;
                    }""",
                    EASY_APPLY_BUTTON_SELECTORS,
                )
            except Exception:
                btn_diag = None
            logger.info(
                "DICE_APPLY_BUTTON job=%s pages_before=%d url=%s "
                "matched=%s",
                job.job_id, pages_before, url_before_click,
                btn_diag,
            )

            # Click the Easy Apply button
            clicked = await self.safe_click(
                page, EASY_APPLY_BUTTON_SELECTORS, timeout=5000
            )
            if not clicked:
                # Dump the apply-button region so the log shows
                # what Dice actually renders. Without this we're
                # guessing at selectors run after run.
                try:
                    snippet = await page.evaluate("""() => {
                        const wc = document.querySelector('apply-button-wc');
                        if (wc) return 'apply-button-wc found: ' + wc.outerHTML.substring(0, 500);
                        const btns = [...document.querySelectorAll('button, a')]
                            .filter(el => el.textContent.toLowerCase().includes('apply'))
                            .map(el => el.outerHTML.substring(0, 200));
                        return 'no apply-button-wc; apply-ish buttons: ' + JSON.stringify(btns.slice(0, 5));
                    }""")
                    logger.info("Dice: apply button diagnostic: %s", snippet)
                except Exception:
                    pass
                return ApplyResult(
                    success=False,
                    failure_reason="Manual apply on company site (no Easy Apply button)",
                    requires_manual_apply=True,
                )

            await random_delay(1.5, 3.0)

            # Post-click diagnostic — did anything actually happen?
            # Compare URL and tab count to pre-click. If both are
            # unchanged AND no modal mounted, the click was eaten
            # (most likely bot-detection guard or wrong-element
            # match). Try a JS-dispatched click on the same anchor
            # as a fallback before giving up — Playwright's click
            # routes through trusted-event handlers but Dice's
            # React onClick may also accept a plain dispatch.
            try:
                url_after_click = page.url
            except Exception:
                url_after_click = ""
            pages_after = len(self.context.pages)
            modal_visible = False
            try:
                modal_el = await page.query_selector(
                    "[role='dialog']:not([aria-hidden='true']), "
                    "[aria-modal='true'], "
                    "[class*='modal']:not([style*='display: none'])"
                )
                if modal_el:
                    modal_visible = await modal_el.is_visible()
            except Exception:
                modal_visible = False
            logger.info(
                "DICE_POST_CLICK job=%s url_changed=%s tabs_added=%d "
                "modal_visible=%s",
                job.job_id,
                url_after_click != url_before_click,
                pages_after - pages_before,
                modal_visible,
            )

            click_had_effect = (
                url_after_click != url_before_click
                or pages_after > pages_before
                or modal_visible
            )

            if not click_had_effect:
                # First attempt produced no observable change. Try
                # a JS dispatch on the same anchor — sometimes Dice's
                # React handler attaches via addEventListener with
                # capture=true and the trusted Playwright click path
                # gets pre-empted. A plain .click() fired from JS
                # bypasses that.
                logger.warning(
                    "Dice: click on apply button had no effect for "
                    "%s — retrying via JS dispatch", job.job_id,
                )
                try:
                    js_clicked = await page.evaluate(
                        """(selectors) => {
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.offsetParent !== null) {
                                    el.click();
                                    return {
                                        selector: sel,
                                        href: el.getAttribute('href') || '',
                                    };
                                }
                            }
                            return null;
                        }""",
                        EASY_APPLY_BUTTON_SELECTORS,
                    )
                    logger.info(
                        "Dice: JS-dispatch click result: %s",
                        js_clicked,
                    )
                except Exception as exc:
                    logger.warning(
                        "Dice: JS-dispatch click raised: %s", exc,
                    )
                await random_delay(1.5, 3.0)

                # If the apply button is an anchor with an href,
                # navigate directly as a final fallback.
                if (
                    btn_diag
                    and btn_diag.get("tag", "").lower() == "a"
                    and btn_diag.get("href")
                ):
                    href = btn_diag["href"]
                    if href.startswith("/"):
                        href = "https://www.dice.com" + href
                    if href.startswith("http"):
                        try:
                            cur_url_check = page.url
                        except Exception:
                            cur_url_check = ""
                        if cur_url_check == url_before_click:
                            # Open the apply URL in a NEW TAB instead
                            # of navigating away on the current page.
                            # The previous goto() left the page reference
                            # stranded on whatever the apply link
                            # redirected to (often a 3rd-party ATS like
                            # apply.teksystems.com), and the engine then
                            # hung trying to find the next job's URL on
                            # an external site.
                            logger.info(
                                "Dice: still on job-detail; opening "
                                "apply href in new tab: %s", href,
                            )
                            try:
                                fallback_tab = await self.context.new_page()
                                await fallback_tab.goto(
                                    href, wait_until="domcontentloaded",
                                )
                                await random_delay(1.5, 3.0)
                            except Exception as exc:
                                logger.warning(
                                    "Dice: direct apply nav failed: %s",
                                    exc,
                                )

            # Dice's apply click sometimes lands on a login gate
            # (/dashboard/login or /register) instead of the apply
            # form. This happens when the browse session's cookie
            # doesn't carry over to the apply subdomain. Detect it
            # and wait for the user to log in manually, just like
            # ensure_logged_in does at the start of the platform run.
            try:
                cur = page.url
            except Exception:
                cur = ""
            if "/login" in cur or "/register" in cur:
                logger.warning(
                    "Dice: apply redirected to login — Dice is requiring "
                    "re-authentication mid-apply (active bot-detection "
                    "signal). Waiting for manual login (timeout=180s). "
                    "If this keeps happening, the platform will be put "
                    "in cooldown."
                )
                logged_back = await self._wait_for_dice_login(
                    page, timeout=180,
                )
                if not logged_back:
                    # Treat the timeout the same as a CAPTCHA — the user
                    # didn't (or couldn't) re-auth, and continuing to
                    # hammer Dice will only deepen the bot-detection
                    # fingerprint. Raising CaptchaDetectedError routes
                    # through the engine's pause logic so dice goes into
                    # 4-hour cooldown automatically.
                    raise CaptchaDetectedError(
                        "Dice apply login gate timed out — Dice required "
                        "re-authentication and the user did not complete "
                        "it within 180s. Platform cooldown engaged."
                    )
                logger.info("Dice: login completed, retrying apply")
                # Re-navigate to the job and retry the apply click
                try:
                    await page.goto(job.url, wait_until="domcontentloaded")
                    await reading_pause(page)
                    clicked = await self.safe_click(
                        page, EASY_APPLY_BUTTON_SELECTORS, timeout=5000
                    )
                    if not clicked:
                        return ApplyResult(
                            success=False,
                            failure_reason="Apply button not found after re-login",
                        )
                    await random_delay(1.5, 3.0)
                except Exception as exc:
                    return ApplyResult(
                        success=False,
                        failure_reason=f"Re-apply after login failed: {exc}",
                    )

            # Check what happened with new tabs after the apply click.
            # Three cases:
            #   None      → no new tab; modal opened on this page (or
            #               click had no effect — handled upstream)
            #   "external"→ new tab was an off-site ATS — already
            #               closed by the helper; bail
            #   Page obj  → new tab is the internal Dice apply form;
            #               continue the walk on THAT page
            tab_result = await self._check_ats_redirect(pages_before)
            if tab_result == "external":
                return ApplyResult(
                    success=False,
                    failure_reason=(
                        "Manual apply on company site (external ATS "
                        "redirect from Dice apply button)"
                    ),
                    requires_manual_apply=True,
                )
            if tab_result is not None:
                # Internal Dice apply opened in a new tab — switch
                # the page reference so the modal walker reads/clicks
                # against the right document. Close the original
                # job-detail tab to prevent unbounded tab growth in
                # continuous-mode runs (one leak per Dice apply
                # otherwise — code-review caught this 2026-05-02).
                original_page = page
                page = tab_result
                if original_page is not tab_result:
                    try:
                        await original_page.close()
                    except Exception as exc:
                        logger.debug(
                            "Dice: failed to close original "
                            "job-detail tab: %s", exc,
                        )

            await self.check_and_abort_on_captcha(page)

            # Walk the multi-step modal
            return await self._walk_easy_apply_modal(
                page, job, resume_path, dry_run
            )

        except CaptchaDetectedError as exc:
            logger.error("Dice: %s", exc)
            return ApplyResult(
                success=False,
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.error("Dice: Apply failed for %s: %s", job.job_id, exc)
            # Try to close the modal to leave a clean state
            await self._close_modal(page)
            return ApplyResult(
                success=False,
                failure_reason=f"Unexpected error: {exc}",
            )

    async def _check_ats_redirect(self, pages_before: int):
        """Check what happened when clicking apply opened a new tab.

        Modern Dice (2026) clicks open the apply form in a NEW TAB
        via target=_blank. The 2026-05-02 live run mistreated every
        Dice internal apply as an external ATS redirect because of
        this — Apply FAILED on every Dice job because we closed the
        new tab before walking it.

        Returns one of:
          - ``None`` if no new tab opened (single-page apply or
            click had no effect)
          - The new ``Page`` object if the new tab is on
            ``dice.com/job-applications`` (internal Dice apply form
            — caller should use this page for the modal walk)
          - ``"external"`` (string) if the new tab is off-site —
            close all new tabs and treat as external ATS

        The caller is responsible for updating its ``page`` reference
        when this method returns a Page.
        """
        await asyncio.sleep(1.0)  # Brief wait for new tab to appear

        pages_after = len(self.context.pages)
        if pages_after <= pages_before:
            return None

        # Check the new tab(s) for an internal Dice apply URL.
        new_pages = list(self.context.pages[pages_before:])
        for new_page in new_pages:
            try:
                # Wait briefly for navigation to settle on the new tab
                # so page.url reflects the destination, not about:blank.
                try:
                    await new_page.wait_for_load_state(
                        "domcontentloaded", timeout=8000,
                    )
                except Exception:
                    pass
                tab_url = new_page.url
            except Exception:
                tab_url = ""
            if (
                "dice.com/job-applications" in tab_url
                or "dice.com/dashboard/apply" in tab_url
                or "/start-apply" in tab_url
            ):
                # Internal Dice apply — close all OTHER new tabs and
                # return this one to the caller.
                for p in new_pages:
                    if p is new_page:
                        continue
                    try:
                        await p.close()
                    except Exception:
                        pass
                logger.info(
                    "Dice: apply opened in new tab (URL=%s); "
                    "switching context to apply tab",
                    tab_url,
                )
                try:
                    await new_page.bring_to_front()
                except Exception:
                    pass
                return new_page

        # All new tabs are off-site — external ATS redirect.
        logger.info(
            "Dice: External ATS redirect detected (new tab URL=%s)",
            new_pages[0].url if new_pages else "?",
        )
        for p in new_pages:
            try:
                await p.close()
            except Exception:
                pass
        return "external"

    async def _wait_for_spinners_to_clear(
        self, page: Page, timeout_ms: int = 3500,
    ) -> None:
        """Wait for loading spinners to disappear before clicking.

        Dice renders a circular loading spinner SVG inside the
        Continue / Submit buttons between modal steps. Playwright
        retries clicks forever while the spinner animates because
        its actionability check fails on "element-not-stable". This
        helper proactively waits for the known spinner markers to
        go away, with a short combined timeout so we don't block
        when no spinner is present.

        Silently returns on timeout — the click attempt will still
        fire; this is a speedup, not a hard requirement.
        """
        # Combined single-selector wait with short timeout. The CSS
        # comma-list matches ANY of the spinner markers; state=hidden
        # resolves as soon as NONE are visible (or timeout fires).
        # The inline-block h6/w6 SVG is the one observed intercepting
        # clicks in prior runs. Others are generic fallbacks.
        try:
            await page.wait_for_selector(
                "div.h6.w6 svg, [role='progressbar'], "
                "svg[class*='animate-spin'], [class*='spinner']",
                state="hidden",
                timeout=timeout_ms,
            )
        except Exception:
            # Timeout (spinner still visible) or no matches — either
            # way, we've waited long enough, proceed with the click.
            pass

    async def _looks_like_auth_page(self, page: Page) -> bool:
        """Detect signup / login / account-creation pages that were
        mistakenly treated as the apply flow.

        Signals:
        1. URL contains /login, /register, /signup, /auth, /account/create
        2. Page has a password input AND a submit button labelled
           'Sign up', 'Create account', 'Register', or 'Log in'

        Both signals are strong — false positives would require an
        actual apply form with password fields, which Dice doesn't use.
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        auth_url_patterns = (
            "/login", "/register", "/signup", "/sign-up",
            "/auth", "/account/create", "/create-account",
        )
        if any(pat in url for pat in auth_url_patterns):
            return True

        # DOM check — password field + signup/login button text
        try:
            has_password = await page.query_selector(
                "input[type='password']"
            )
            if not has_password:
                return False
            buttons = await page.query_selector_all("button, input[type='submit']")
            for btn in buttons:
                try:
                    text = (await btn.inner_text()).strip().lower()
                except Exception:
                    continue
                if any(kw in text for kw in (
                    "sign up", "create account", "register", "log in",
                    "sign in",
                )):
                    return True
        except Exception:
            pass
        return False

    async def _walk_easy_apply_modal(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through each step of the Dice Easy Apply modal.

        Handles: form fields, resume upload, Next/Submit buttons.
        Returns when either Submit is clicked or an error occurs.
        """
        # SAFETY CHECK: if this is actually a signup/login page (not an
        # apply page), bail out before we waste 8 modal-step cycles on
        # a form we can never complete. This catches the common false-
        # positive where Dice redirects to create-account and our
        # login selectors mistakenly matched the "go to dashboard"
        # link in the signup page's nav.
        if await self._looks_like_auth_page(page):
            logger.warning(
                "Dice: apply page looks like a signup/login form — "
                "bailing out. URL=%s", page.url,
            )
            return self._build_result(
                success=False,
                failure_reason=(
                    "Dice redirected to a sign-up / login page instead "
                    "of the apply form. Please log in to Dice manually "
                    "in the browser window and retry."
                ),
            )

        for step in range(MAX_MODAL_STEPS):
            logger.info(
                "Dice: Easy Apply step %d for %s", step + 1, job.job_id
            )

            # Wait for the apply modal / drawer to actually mount in the
            # DOM before we scan it. Live runs 2026-05-02 hit "No
            # navigation button found at step 1" on TWO Dice jobs in a
            # row because find_form_fields ran on the job-detail page
            # while the modal was still rendering. Poll for any of:
            # role=dialog, [aria-modal=true], common modal class names,
            # or a visible Continue/Next/Submit button anywhere on
            # the page (the modal's own nav). 8s ceiling — past that
            # the DOM dump will tell us where the modal actually
            # lives (shadow DOM, iframe, or somewhere else).
            try:
                await page.wait_for_selector(
                    "[role='dialog']:not([aria-hidden='true']), "
                    "[aria-modal='true'], "
                    "[class*='modal'], "
                    "[class*='Modal'], "
                    "[class*='dialog'], "
                    "button[data-testid='next-button'], "
                    "button[data-testid='submit-application'], "
                    "button[data-testid='submit-next'], "
                    "[data-testid*='apply-form']",
                    state="visible",
                    timeout=8000,
                )
            except Exception:
                # Don't fail yet — let find_form_fields run on whatever
                # is on the page. We'll dump diagnostics if it sees 0.
                logger.debug(
                    "Dice: no modal-shaped element appeared within 8s; "
                    "scanning current DOM regardless",
                )

            await self.check_and_abort_on_captcha(page)

            # Mid-walk login-gate detection. Dice's bot-detection
            # bounces the user back to a login / register page mid-flow
            # (often right before Submit). Detecting it inside the
            # walker — not just at apply-click — so the user gets a
            # chance to re-auth, and so a timeout escalates to a
            # platform-wide cooldown via CaptchaDetectedError.
            try:
                cur_url = page.url
            except Exception:
                cur_url = ""
            if "/login" in cur_url or "/register" in cur_url:
                logger.warning(
                    "Dice: modal step %d redirected to login page — "
                    "Dice is requiring re-auth mid-apply. Waiting for "
                    "manual login (timeout=180s).",
                    step + 1,
                )
                logged_back = await self._wait_for_dice_login(
                    page, timeout=180,
                )
                if not logged_back:
                    raise CaptchaDetectedError(
                        f"Dice modal step {step + 1} login gate timed "
                        f"out — Dice required re-auth mid-apply and the "
                        f"user did not complete it within 180s. Platform "
                        f"cooldown engaged."
                    )
                logger.info(
                    "Dice: re-login completed mid-modal, re-navigating "
                    "to job and re-starting apply"
                )
                # Mid-modal re-login almost always loses the modal
                # state. Bail this job — the engine will treat it as a
                # failed apply, but the platform stays usable for the
                # next job (the cookie is now fresh).
                return self._build_result(
                    success=False,
                    failure_reason=(
                        f"Dice required re-auth at modal step {step + 1}. "
                        "Re-login completed; retrying this job in a future "
                        "run."
                    ),
                )

            # Handle resume upload on this step
            await self._handle_resume_upload(page, resume_path)

            # Detect and fill form fields
            if self.form_filler:
                fields = await find_form_fields(page)
                if not fields:
                    # 0 fields when we expected a modal full of them =
                    # the walker is looking at the wrong DOM. Dump the
                    # page structure so the next diagnosis cycle has
                    # ground truth: where IS the modal actually living?
                    await self._dump_dice_modal_shape(page, job, step)
                for field in fields:
                    await self.form_filler.fill_field(
                        page, field, job_id=job.job_id
                    )
                    await random_delay(0.5, 1.5)
                # Dice's modal widgets are React-controlled — commit
                # filled state through the React-aware setter so the
                # Next/Submit button doesn't keep seeing required
                # fields as empty (same root cause as Indeed's
                # "form stuck — all fields filled" bug).
                if fields:
                    await self.form_filler.commit_react_state(page)

            await simulate_organic_behavior(page)

            # Check for success indicators
            if await self._check_success(page):
                logger.info(
                    "Dice: Application success detected for %s", job.job_id
                )
                return self._build_result(success=True, dry_run=False)

            # Determine which button to click: Submit or Next
            is_submit_step = await self._is_submit_step(page)

            if is_submit_step:
                if dry_run:
                    logger.info(
                        "Dice: DRY RUN -- would submit application for %s",
                        job.job_id,
                    )
                    await self._close_modal(page)
                    return self._build_result(success=True, dry_run=True)

                # Wait for any loading spinner to clear before clicking.
                # Dice renders a circular SVG spinner inside the
                # Continue/Submit button between modal steps; Playwright
                # treats it as an "element not stable" condition and
                # retries the click forever until its 30s default
                # timeout. Giving the spinner a moment to resolve first
                # unblocks every subsequent click.
                await self._wait_for_spinners_to_clear(page)

                # Submit the application
                clicked = await self.safe_click(
                    page, MODAL_SUBMIT_SELECTORS, timeout=3000
                )
                if clicked:
                    await random_delay(2.0, 4.0)

                    # Verify submission success
                    if await self._check_success(page):
                        logger.info(
                            "Dice: Submitted application for %s", job.job_id
                        )
                        await self._close_modal(page)
                        return self._build_result(success=True, dry_run=False)

                    # Assume success if no error after clicking submit
                    logger.info(
                        "Dice: Submit clicked for %s (no explicit success indicator)",
                        job.job_id,
                    )
                    return self._build_result(success=True, dry_run=False)
                else:
                    return self._build_result(
                        success=False,
                        failure_reason="Submit button not found on submit step",
                    )

            else:
                # Wait for loading spinner to clear before clicking Next
                # (see comment above the Submit click for rationale).
                await self._wait_for_spinners_to_clear(page)

                # Click Next to advance to the next step
                clicked = await self.safe_click(
                    page, MODAL_NEXT_SELECTORS, timeout=3000
                )
                if not clicked:
                    # Check for visible validation errors before giving up
                    validation_msg = await self._scan_validation_errors(page)
                    reason = f"No navigation button found at step {step + 1}"
                    if validation_msg:
                        reason += f" (validation: {validation_msg})"
                    logger.warning("Dice: %s", reason)
                    return self._build_result(
                        success=False,
                        failure_reason=reason,
                    )
                await random_delay(1.5, 3.0)

        # Exceeded max steps
        logger.warning(
            "Dice: Exceeded %d modal steps for %s", MAX_MODAL_STEPS, job.job_id
        )
        await self._close_modal(page)
        return self._build_result(
            success=False,
            failure_reason=f"Exceeded maximum modal steps ({MAX_MODAL_STEPS})",
        )

    async def _is_submit_step(self, page: Page) -> bool:
        """Check if the current modal step has a Submit button."""
        for sel in MODAL_SUBMIT_SELECTORS[:3]:  # Check specific selectors first
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip().lower()
                    if "submit" in text or "apply" in text:
                        return True
            except Exception:
                continue

        # Check all buttons for submit-like text
        try:
            buttons = await page.query_selector_all("button")
            for btn in buttons:
                text = (await btn.inner_text()).strip().lower()
                if text in (
                    "submit",
                    "submit application",
                    "apply",
                    "apply now",
                ):
                    return True
        except Exception:
            pass

        return False

    async def _dump_dice_modal_shape(
        self, page: Page, job: Job, step: int,
    ) -> None:
        """Diagnostic dump for Dice apply: where is the modal actually?

        Two live runs hit "Detected 0 form fields on page" while the
        URL was unchanged at /job-detail/{id} — meaning the apply
        click happened, no new tab opened, no URL change occurred,
        and yet our scanner found nothing fillable. Possible causes:
          - Modal lives inside a Shadow DOM (custom element)
          - Modal lives in an iframe
          - Modal opened in a sibling element our walker doesn't reach
          - Apply click silently rejected by Dice's bot detection

        This dump captures enough about the page to tell which case
        we're in: count of all input/textarea/select including
        shadow-DOM and iframe descendants, top-level dialog/modal
        elements, iframe URLs, and a body-text snippet. One greppable
        line per stuck case under the DICE_MODAL_SHAPE tag.
        """
        try:
            cur_url = page.url
        except Exception:
            cur_url = "?"
        try:
            shape = await page.evaluate(
                """() => {
                    const out = {
                        url: location.href,
                        title: document.title,
                        forms_visible: [...document.querySelectorAll('form')]
                            .filter(f => f.offsetParent !== null).length,
                        inputs_top: document.querySelectorAll(
                            'input:not([type=hidden])'
                        ).length,
                        textareas_top: document.querySelectorAll('textarea').length,
                        selects_top: document.querySelectorAll('select').length,
                        dialogs: [...document.querySelectorAll(
                            '[role=dialog], [aria-modal=true]'
                        )]
                            .filter(d => d.offsetParent !== null)
                            .map(d => ({
                                cls: (d.className || '').substring(0, 80),
                                testid: d.getAttribute('data-testid') || '',
                                inputs: d.querySelectorAll('input, textarea, select').length,
                            })),
                        iframes: [...document.querySelectorAll('iframe')]
                            .filter(f => f.offsetParent !== null)
                            .map(f => ({
                                src: (f.src || '').substring(0, 120),
                                name: f.name || '',
                            })),
                        modal_class_candidates: [...document.querySelectorAll(
                            '[class*=modal i], [class*=Modal], [class*=drawer i]'
                        )]
                            .filter(d => d.offsetParent !== null)
                            .slice(0, 5)
                            .map(d => (d.className || '').substring(0, 80)),
                        // Probe shadow roots — count fillable elements
                        // inside any visible custom element with a shadowRoot.
                        shadow_inputs: (() => {
                            let count = 0;
                            const visit = (root) => {
                                count += root.querySelectorAll(
                                    'input, textarea, select'
                                ).length;
                                root.querySelectorAll('*').forEach(el => {
                                    if (el.shadowRoot) visit(el.shadowRoot);
                                });
                            };
                            visit(document);
                            return count;
                        })(),
                        body_snippet: (document.body.innerText || '')
                            .substring(0, 300),
                    };
                    return out;
                }"""
            )
        except Exception as exc:
            shape = {"error": str(exc)}
        import json as _json
        logger.warning(
            "DICE_MODAL_SHAPE job=%s step=%d url=%s shape=%s",
            job.job_id, step + 1, cur_url,
            _json.dumps(shape)[:1500],
        )

    async def _check_success(self, page: Page) -> bool:
        """Check if application submission succeeded."""
        el = await self.safe_query(page, SUCCESS_SELECTORS, timeout=2000)
        if el:
            return True

        # Check page text for success phrases
        try:
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
            success_phrases = [
                "application submitted",
                "your application has been submitted",
                "successfully applied",
                "application sent",
                "thank you for applying",
                "you have applied",
            ]
            for phrase in success_phrases:
                if phrase in body_lower:
                    return True
        except Exception:
            pass

        return False

    async def _handle_resume_upload(self, page: Page, resume_path: str) -> None:
        """Upload the resume to the RESUME slot specifically.

        Earlier versions used a generic ``input[type='file']``
        selector that grabbed the first file input on the page.
        On multi-step Dice forms with the resume slot on step 1
        and a separate cover-letter file input on step 2, this
        invocation on step 2 uploaded the resume PDF to the
        cover-letter slot — the user saw their resume filename in
        both places. Now we enumerate every file input, classify
        each via FormFiller.classify_file_input, and only upload
        to one classified as "resume" (or "unknown" as a fallback,
        with a warning).
        """
        if not resume_path:
            return

        from auto_applier.browser.form_filler import FormFiller
        from auto_applier.resume.pdf_converter import ensure_pdf

        # Convert .docx / .txt to PDF once per upload step. PDFs are
        # returned as-is, so this is a no-op when the user already
        # has a PDF resume. Indeed in particular flags non-PDF
        # uploads with "PDF recommended" warnings; some Workday-style
        # forms reject .docx outright.
        try:
            resume_path = str(await ensure_pdf(resume_path))
        except Exception as exc:
            logger.warning(
                "Dice: ensure_pdf failed (%s); using original file.", exc,
            )

        target = await FormFiller.pick_resume_input(page, "Dice")
        if target is None:
            return

        try:
            await target.set_input_files(resume_path)
            await FormFiller.wait_for_upload_complete(
                page, expected_name=resume_path, timeout=15.0,
            )
            logger.info("Dice: Uploaded resume from %s", resume_path)
            await random_delay(1.0, 2.0)
        except Exception as exc:
            logger.warning("Dice: resume upload failed: %s", exc)

    async def _close_modal(self, page: Page) -> None:
        """Close the Easy Apply modal to leave a clean state."""
        await self.safe_click(page, MODAL_CLOSE_SELECTORS, timeout=2000)
        await asyncio.sleep(0.5)
        # Handle any confirmation dialogs
        try:
            confirm_selectors = [
                "button[data-cy='confirm-close']",
                "button.btn-confirm",
                "button.seds-button-primary",
            ]
            await self.safe_click(page, confirm_selectors, timeout=2000)
        except Exception:
            pass

    async def _scan_validation_errors(self, page: Page) -> str:
        """Scan for visible validation error messages on the current page.

        Returns a short string describing the error, or empty string
        if no errors found. This helps diagnose why a form won't
        advance to the next step.
        """
        error_selectors = [
            ".error-message",
            "[class*='error']",
            "[class*='invalid']",
            "[role='alert']",
            ".form-error",
            ".field-error",
            ".validation-error",
        ]
        for sel in error_selectors:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    try:
                        visible = await el.is_visible()
                    except Exception:
                        visible = False
                    if not visible:
                        continue
                    text = (await el.inner_text()).strip()
                    if text and len(text) < 200:
                        return text
            except Exception:
                continue
        return ""

    def _build_result(
        self,
        success: bool,
        dry_run: bool = False,
        failure_reason: str = "",
    ) -> ApplyResult:
        """Build an ApplyResult from the current form_filler state."""
        if self.form_filler:
            result = ApplyResult(
                success=success,
                gaps=list(self.form_filler.gaps),
                resume_used=self.form_filler.resume_label,
                cover_letter_generated=self.form_filler.cover_letter_generated,
                failure_reason=failure_reason,
                fields_filled=self.form_filler.fields_filled,
                fields_total=self.form_filler.fields_total,
                used_llm=self.form_filler.used_llm,
            )
        else:
            result = ApplyResult(
                success=success,
                failure_reason=failure_reason,
            )
        # Log a summary line so dry-run testing shows field fill rates
        if result.success:
            logger.info(
                "Dice: Apply result: success [%d/%d fields, llm=%s]",
                result.fields_filled, result.fields_total, result.used_llm,
            )
        else:
            logger.info(
                "Dice: Apply result: failed: %s", result.failure_reason,
            )
        return result
