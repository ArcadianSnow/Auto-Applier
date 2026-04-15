"""LinkedIn platform adapter for job search and Easy Apply automation.

This adapter handles:
- Manual login detection and prompting
- Job search with Easy Apply filter
- Job card parsing from search results
- Easy Apply modal walking (multi-step forms)
- Form field filling via FormFiller
- CAPTCHA detection and hard stop

IMPORTANT: LinkedIn changes its DOM frequently. Every selector here has
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

# Indicators that the user is logged in
LOGGED_IN_SELECTORS = [
    "img.global-nav__me-photo",
    ".global-nav__me-photo",
    "[data-control-name='nav.settings']",
    ".feed-identity-module",
    ".global-nav__primary-link--active",
    "nav.global-nav",
    "#global-nav",
    ".scaffold-layout__main",
]

# Job card selectors on the search results page
JOB_CARD_SELECTORS = [
    ".job-card-container",
    ".jobs-search-results__list-item",
    ".scaffold-layout__list-item",
    "li.jobs-search-results-list__list-item",
    "[data-occludable-job-id]",
    ".job-card-list__entity-lockup",
]

# Job title within a card
JOB_TITLE_SELECTORS = [
    ".job-card-list__title",
    ".job-card-container__link",
    "a.job-card-list__title--link",
    ".job-card-list__title--link",
    "a[data-control-name='jobPosting_title']",
    ".artdeco-entity-lockup__title a",
]

# Company name within a card
JOB_COMPANY_SELECTORS = [
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
    ".artdeco-entity-lockup__subtitle",
    ".job-card-list__company-name",
]

# Easy Apply button on job detail
EASY_APPLY_BUTTON_SELECTORS = [
    "button.jobs-apply-button",
    ".jobs-apply-button--top-card",
    "button[aria-label*='Easy Apply']",
    "button[aria-label*='easy apply']",
    ".jobs-s-apply button",
    "button.jobs-apply-button[data-control-name='jobdetails_topcard_inapply']",
]

# Job description on the detail panel
JOB_DESCRIPTION_SELECTORS = [
    ".jobs-description__content",
    ".jobs-description-content__text",
    ".jobs-box__html-content",
    "#job-details",
    "[class*='jobs-description']",
    ".jobs-unified-top-card__description",
]

# Easy Apply modal navigation buttons
MODAL_NEXT_SELECTORS = [
    "button[aria-label='Continue to next step']",
    "button[aria-label='Next']",
    "footer button.artdeco-button--primary",
    ".jobs-easy-apply-footer button.artdeco-button--primary",
]

MODAL_REVIEW_SELECTORS = [
    "button[aria-label='Review your application']",
    "button[aria-label='Review']",
    "footer button.artdeco-button--primary",
]

MODAL_SUBMIT_SELECTORS = [
    "button[aria-label='Submit application']",
    "button[aria-label='Submit']",
    "footer button.artdeco-button--primary",
]

MODAL_CLOSE_SELECTORS = [
    "button[aria-label='Dismiss']",
    "button[aria-label='Close']",
    "button.artdeco-modal__dismiss",
    "[data-test-modal-close-btn]",
]

# File upload input in Easy Apply modal
RESUME_UPLOAD_SELECTORS = [
    "input[type='file'][name*='resume']",
    "input[type='file'][name*='file']",
    "input[type='file']",
]

# Max results pages to paginate through
MAX_SEARCH_PAGES = 3
MAX_JOBS_PER_SEARCH = 25
MAX_MODAL_STEPS = 8  # Safety limit for multi-step modals


class LinkedInPlatform(JobPlatform):
    """LinkedIn job search and Easy Apply adapter.

    Requires the user to be logged in manually -- this adapter will
    never automate credential entry. It navigates to LinkedIn, checks
    for login indicators, and waits for the user if needed.
    """

    source_id = "linkedin"
    display_name = "LinkedIn"

    dead_listing_selectors = [
        ".jobs-details-top-card__apply-error",
        ".jobs-details__no-longer-accepting",
        "[data-test-modal='job-closed']",
    ]
    dead_listing_phrases = [
        "no longer accepting applications",
        "this job is no longer",
        "the job you were looking for has been closed",
    ]

    # LinkedIn challenge paths. Be careful here — several URLs that
    # look like challenges are actually normal login-flow endpoints:
    #
    #   /checkpoint/lg/login-submit  → POST target for the login form
    #   /uas/login-submit            → legacy login POST endpoint
    #   /authwall                    → the page shown to logged-out
    #                                   users who click a job/profile
    #                                   link (they'll manually log in
    #                                   from there — NOT a challenge)
    #
    # Only flag URLs that are exclusively used for real challenges.
    # /checkpoint/challenge is the actual CAPTCHA / 2FA / phone
    # verification path.
    captcha_url_patterns = [
        "/captcha",
        "/recaptcha",
        "/checkpoint/challenge",
    ]

    # URL substring patterns indicating the user is on a LinkedIn
    # auth / not-logged-in page (not a logged-in page). When any of
    # these appears in the URL, the user MUST log in first regardless
    # of what DOM elements happen to be present.
    #
    # /authwall shows when a logged-out user clicks a job/profile
    # link. /checkpoint and /uas are the login / 2FA flow. /signup is
    # the account creation form. Watching for these keeps us from
    # walking a login/signup form as if it were an apply modal.
    _AUTH_URL_PATTERNS = (
        "/login",
        "/signup",
        "/checkpoint",
        "/uas/login",
        "/authwall",
        "/m/login",  # mobile login redirect
    )

    # Labels that belong to LinkedIn PAGE CHROME (persistent header
    # search bar, messaging drawer, notification filters) — NOT the
    # application form. LinkedIn has FAR more chrome than ZR: the
    # site-wide "Search" is on every page, plus the jobs page has its
    # own filter bar. The chrome-field filter from ZR ported here
    # keeps the form_filler from wasting LLM calls on these during
    # modal walks that bleed back to the main page.
    _CHROME_LABEL_PATTERNS = (
        "search by title",
        "search by skill",
        "search by company",
        "search jobs",
        "search for job",
        "search messages",
        "search people",
        "jobs search",
        "messaging search",
    )

    def _is_chrome_field(self, label: str) -> bool:
        """True if a field label looks like LinkedIn page chrome."""
        lower = (label or "").lower().strip()
        chrome_exact = {
            "search",
            "search by title, skill, or company",
            "city, state, or zip code",  # jobs location filter
        }
        if lower in chrome_exact:
            return True
        if lower.startswith("search "):
            return True
        return any(pat in lower for pat in self._CHROME_LABEL_PATTERNS)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """Navigate to LinkedIn and verify the user is logged in.

        Uses URL-first detection: the user is logged in when they're
        on linkedin.com but NOT on /login, /signup, /authwall, or
        /checkpoint. DOM selectors are too fragile for LinkedIn —
        their feed page markup changes constantly and single-selector
        matches are either too broad (false-positive on login page's
        nav) or too narrow (miss the logged-in UI after a redesign).
        """
        page = await self.get_page()

        # Navigate to feed to check login state
        await page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await random_delay(2.0, 4.0)

        if await self._url_indicates_logged_in(page):
            logger.info(
                "LinkedIn: Already logged in (URL=%s)", page.url,
            )
            return True

        # Not logged in — navigate to login page
        logger.info(
            "LinkedIn: Not logged in. Navigating to login page for "
            "manual login..."
        )
        await page.goto(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
        )

        return await self._wait_for_manual_login_url(page, timeout=300)

    async def _url_indicates_logged_in(self, page: Page) -> bool:
        """True when the page URL is on linkedin.com but NOT on an
        auth / challenge page."""
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        if "linkedin.com" not in url:
            return False
        if any(pat in url for pat in self._AUTH_URL_PATTERNS):
            return False
        return True

    async def _wait_for_manual_login_url(
        self, page: Page, timeout: int = 300,
    ) -> bool:
        """Poll every 2s for URL-based login confirmation."""
        import time
        logger.info(
            "Waiting for manual login on LinkedIn "
            "(URL-based, timeout=%ds)...",
            timeout,
        )
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if await self._url_indicates_logged_in(page):
                logger.info(
                    "LinkedIn: Login detected (URL=%s)", page.url,
                )
                return True
            await asyncio.sleep(2.0)
        logger.warning(
            "LinkedIn: Manual login timed out after %ds", timeout,
        )
        return False

    async def _looks_like_auth_page(self, page: Page) -> bool:
        """Detect signup / login / challenge pages that were
        mistakenly treated as the apply flow.

        URL patterns + DOM password-field check, same two-signal
        pattern as the Dice adapter. Bails early so we don't walk an
        auth form as if it were an Easy Apply modal.
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        if any(pat in url for pat in self._AUTH_URL_PATTERNS):
            return True

        # DOM signal: password input + sign-in/signup-style button
        try:
            has_password = await page.query_selector(
                "input[type='password']"
            )
            if not has_password:
                return False
            buttons = await page.query_selector_all(
                "button, input[type='submit']",
            )
            for btn in buttons:
                try:
                    text = (await btn.inner_text()).strip().lower()
                except Exception:
                    continue
                if any(kw in text for kw in (
                    "sign in", "sign up", "join now",
                    "log in", "continue with password",
                )):
                    return True
        except Exception:
            pass
        return False

    async def _wait_for_spinners_to_clear(
        self, page: Page, timeout_ms: int = 3500,
    ) -> None:
        """Wait for loading spinners to disappear before clicking.

        LinkedIn's modals show a brief progressbar or loading
        indicator during step transitions; Playwright retries clicks
        for 30s if a spinner intercepts. Same pattern as Dice.
        Short combined wait, pass-through on timeout.
        """
        try:
            await page.wait_for_selector(
                "[role='progressbar'], .artdeco-loader, "
                "svg[class*='animate-spin'], [class*='spinner']",
                state="hidden",
                timeout=timeout_ms,
            )
        except Exception:
            pass

    async def _scan_validation_errors(self, page: Page) -> str:
        """Return a short description of any visible validation error.

        LinkedIn surfaces required-field warnings with specific
        classes; catching them lets us surface the real reason a
        form stuck instead of the generic "no navigation button".
        """
        error_selectors = [
            ".artdeco-inline-feedback--error",
            "[role='alert']",
            ".fb-form-element__error-text",
            "[class*='error-text']",
            "[aria-invalid='true'] + *",
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

    # ------------------------------------------------------------------
    # Job Search
    # ------------------------------------------------------------------

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search LinkedIn Jobs with Easy Apply filter enabled.

        Anti-detection strategy:
        1. Warm up by landing on the feed and doing human-like scrolling
           before hitting the jobs search URL. A cold navigation directly
           to /jobs/search from a fresh tab is one of LinkedIn's biggest
           automation signals.
        2. Use longer inter-page delays than the other platforms.
        3. Organic noise (scroll, hover, mouse wander) between pages.

        Paginates through up to MAX_SEARCH_PAGES pages and parses
        job cards from the results.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

        # Warm-up: visit the feed first and scroll a bit so the
        # session looks like a logged-in user browsing normally
        # before they decide to check job postings.
        try:
            if "/feed" not in page.url.lower():
                logger.info("LinkedIn: warm-up via feed before jobs search")
                await page.goto(
                    "https://www.linkedin.com/feed/",
                    wait_until="domcontentloaded",
                )
                await reading_pause(page)
                await simulate_organic_behavior(page)
        except Exception as exc:
            logger.debug("LinkedIn warm-up skipped: %s", exc)

        jobs: list[Job] = []
        encoded_kw = quote_plus(keyword)
        encoded_loc = quote_plus(location)

        for page_num in range(MAX_SEARCH_PAGES):
            start = page_num * 25
            # f_AL=true enables Easy Apply filter
            search_url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={encoded_kw}"
                f"&location={encoded_loc}"
                f"&f_AL=true"
                f"&start={start}"
            )

            logger.info(
                "LinkedIn: Searching page %d -- %s %s",
                page_num + 1,
                keyword,
                location,
            )
            await page.goto(search_url, wait_until="domcontentloaded")
            await reading_pause(page)
            await simulate_organic_behavior(page)
            await self.check_and_abort_on_captcha(page)

            # Parse job cards from this page
            page_jobs = await self._parse_job_cards(page, keyword)
            jobs.extend(page_jobs)

            if len(jobs) >= MAX_JOBS_PER_SEARCH:
                break

            # Check if there are more pages
            if not page_jobs:
                logger.info("LinkedIn: No more job cards found, stopping pagination")
                break

            # Organic delay between pages
            await simulate_organic_behavior(page)
            await random_delay(3.0, 6.0)

        logger.info("LinkedIn: Found %d jobs for '%s' in '%s'", len(jobs), keyword, location)
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
            logger.warning("LinkedIn: No job cards found on page")
            return jobs

        for card in cards:
            try:
                job = await self._parse_single_card(card, page, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug("Failed to parse a job card: %s", exc)
                continue

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

        # Get company
        company = ""
        for sel in JOB_COMPANY_SELECTORS:
            try:
                company_el = await card.query_selector(sel)
                if company_el:
                    company = (await company_el.inner_text()).strip()
                    break
            except Exception:
                continue

        # Get job URL / ID
        job_url = ""
        job_id = ""
        try:
            link_el = await card.query_selector("a[href*='/jobs/view/']")
            if not link_el:
                link_el = await card.query_selector("a")
            if link_el:
                href = await link_el.get_attribute("href") or ""
                if "/jobs/view/" in href:
                    job_url = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                    # Extract numeric job ID from URL
                    match = re.search(r"/jobs/view/(\d+)", href)
                    if match:
                        job_id = f"li-{match.group(1)}"
        except Exception:
            pass

        # Fallback: try data attribute for job ID
        if not job_id:
            try:
                data_id = await card.get_attribute("data-occludable-job-id")
                if data_id:
                    job_id = f"li-{data_id}"
                    job_url = f"https://www.linkedin.com/jobs/view/{data_id}/"
            except Exception:
                pass

        if not job_id:
            # Generate a fallback ID from title + company
            job_id = f"li-{hash(title + company) % 10**8}"

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
            logger.warning("LinkedIn: No URL for job %s, cannot fetch description", job.job_id)
            return ""

        await reading_pause(page)
        await self.check_and_abort_on_captcha(page)

        # Try to expand "See more" in the description
        await self.safe_click(
            page,
            [
                "button[aria-label='Click to see more description']",
                "button.jobs-description__footer-button",
                "button[aria-label='Show more']",
                ".jobs-description__content button",
            ],
            timeout=2000,
        )
        await random_delay(0.5, 1.5)

        # Extract description text
        description = await self.safe_get_text(page, JOB_DESCRIPTION_SELECTORS, timeout=5000)

        if description:
            logger.debug(
                "LinkedIn: Got description for %s (%d chars)",
                job.job_id,
                len(description),
            )
        else:
            logger.warning("LinkedIn: Could not extract description for %s", job.job_id)

        return description

    async def check_is_external(self, job: Job) -> bool:
        """LinkedIn: no Easy Apply button visible means external-only.

        Runs on the already-loaded job detail panel (same navigation
        as get_job_description). If none of the Easy Apply button
        selectors match, the only way to apply is through a third-party
        ATS, so the orchestrator should skip before running any LLM
        scoring cycles.
        """
        try:
            page = await self.get_page()
            btn = await self.safe_query(
                page, EASY_APPLY_BUTTON_SELECTORS, timeout=1500,
            )
            return btn is None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Easy Apply
    # ------------------------------------------------------------------

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """Walk the LinkedIn Easy Apply modal and fill all form fields.

        In dry_run mode, fills fields and navigates steps but does not
        click the final Submit button.
        """
        page = await self.get_page()

        try:
            # Navigate to the job if needed
            if job.url and job.url not in page.url:
                await page.goto(job.url, wait_until="domcontentloaded")
                await reading_pause(page)

            await self.check_and_abort_on_captcha(page)

            # Click the Easy Apply button
            clicked = await self.safe_click(
                page, EASY_APPLY_BUTTON_SELECTORS, timeout=5000,
            )
            if not clicked:
                # Diagnostic: what apply-like buttons ARE on the page?
                # LinkedIn changes button markup often; this dump tells
                # us which selector to add next time.
                try:
                    snippet = await page.evaluate("""() => {
                        const btns = [...document.querySelectorAll('button, a')]
                            .filter(el => /apply|save/i.test(el.textContent || ''))
                            .map(el => el.outerHTML.substring(0, 200));
                        return 'apply-ish buttons: ' + JSON.stringify(btns.slice(0, 5));
                    }""")
                    logger.info(
                        "LinkedIn: apply button diagnostic: %s", snippet,
                    )
                except Exception:
                    pass
                return ApplyResult(
                    success=False,
                    failure_reason=(
                        "Easy Apply button not found — job may require "
                        "external application"
                    ),
                )

            await random_delay(1.5, 3.0)

            # Apply click can redirect to login if session cookie is
            # stale. Bail out cleanly instead of walking the login form.
            if await self._looks_like_auth_page(page):
                logger.warning(
                    "LinkedIn: apply redirected to auth page (URL=%s)",
                    page.url,
                )
                return ApplyResult(
                    success=False,
                    failure_reason=(
                        "LinkedIn redirected to a login / challenge "
                        "page. Please log in to LinkedIn manually in "
                        "the browser window and retry."
                    ),
                )

            await self.check_and_abort_on_captcha(page)

            # Walk the multi-step modal
            return await self._walk_easy_apply_modal(page, job, resume_path, dry_run)

        except CaptchaDetectedError as exc:
            logger.error("LinkedIn: %s", exc)
            return ApplyResult(
                success=False,
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.error("LinkedIn: Apply failed for %s: %s", job.job_id, exc)
            # Try to close the modal to leave a clean state
            await self._close_modal(page)
            return ApplyResult(
                success=False,
                failure_reason=f"Unexpected error: {exc}",
            )

    async def _walk_easy_apply_modal(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through each step of the Easy Apply modal.

        Handles: form fields, resume upload, Next/Review/Submit buttons.
        Returns when either Submit is clicked or an error occurs.

        Uses the hardened patterns from Dice/ZR: chrome-field filter
        (LinkedIn headers are noisier than ZR's), spinner-wait before
        navigation clicks, validation-error scan when forms get stuck.
        """
        any_real_fields_filled = False

        for step in range(MAX_MODAL_STEPS):
            logger.info(
                "LinkedIn: Easy Apply step %d for %s",
                step + 1, job.job_id,
            )

            await self.check_and_abort_on_captcha(page)

            # Handle resume upload on this step
            await self._handle_resume_upload(page, resume_path)

            # Detect and fill form fields — filter out page chrome
            real_fields_on_step = 0
            if self.form_filler:
                fields = await find_form_fields(page)
                real_fields = [
                    f for f in fields if not self._is_chrome_field(f.label)
                ]
                for field in real_fields:
                    await self.form_filler.fill_field(
                        page, field, job_id=job.job_id,
                    )
                    await random_delay(0.5, 1.5)
                real_fields_on_step = len(real_fields)
                if real_fields_on_step > 0:
                    any_real_fields_filled = True
                logger.debug(
                    "LinkedIn: step %d — %d real fields, %d chrome fields",
                    step + 1, real_fields_on_step,
                    len(fields) - real_fields_on_step,
                )

            await simulate_organic_behavior(page)

            # Determine which button to click: Submit, Review, or Next
            is_submit_step = await self._is_submit_step(page)
            is_review_step = await self._is_review_step(page)

            if is_submit_step:
                if dry_run:
                    logger.info(
                        "LinkedIn: DRY RUN -- would submit application "
                        "for %s",
                        job.job_id,
                    )
                    await self._close_modal(page)
                    return self._build_result(success=True, dry_run=True)

                # Wait for spinner, then submit
                await self._wait_for_spinners_to_clear(page)
                clicked = await self.safe_click(
                    page, MODAL_SUBMIT_SELECTORS, timeout=3000,
                )
                if clicked:
                    await random_delay(2.0, 4.0)
                    logger.info(
                        "LinkedIn: Submitted application for %s",
                        job.job_id,
                    )
                    # Dismiss any post-submit dialog
                    await self.safe_click(
                        page, MODAL_CLOSE_SELECTORS, timeout=2000,
                    )
                    return self._build_result(success=True, dry_run=False)
                else:
                    return self._build_result(
                        success=False,
                        failure_reason="Submit button not found on submit step",
                    )

            elif is_review_step:
                await self._wait_for_spinners_to_clear(page)
                clicked = await self.safe_click(
                    page, MODAL_REVIEW_SELECTORS, timeout=3000,
                )
                if not clicked:
                    # Try the generic primary button
                    clicked = await self.safe_click(
                        page, MODAL_NEXT_SELECTORS, timeout=3000,
                    )
                if not clicked:
                    validation_msg = await self._scan_validation_errors(page)
                    reason = "Review button not found"
                    if validation_msg:
                        reason += f" (validation: {validation_msg})"
                    return self._build_result(
                        success=False, failure_reason=reason,
                    )
                await random_delay(1.5, 3.0)

            else:
                await self._wait_for_spinners_to_clear(page)
                clicked = await self.safe_click(
                    page, MODAL_NEXT_SELECTORS, timeout=3000,
                )
                if not clicked:
                    # Scan for validation errors before giving up
                    validation_msg = await self._scan_validation_errors(page)
                    reason = f"No navigation button found at step {step + 1}"
                    if validation_msg:
                        reason += f" (validation: {validation_msg})"
                    logger.warning("LinkedIn: %s", reason)
                    return self._build_result(
                        success=False, failure_reason=reason,
                    )
                await random_delay(1.5, 3.0)

        # Exceeded max steps
        logger.warning(
            "LinkedIn: Exceeded %d modal steps for %s",
            MAX_MODAL_STEPS, job.job_id,
        )
        await self._close_modal(page)
        return self._build_result(
            success=False,
            failure_reason=f"Exceeded maximum modal steps ({MAX_MODAL_STEPS})",
        )

    async def _is_submit_step(self, page: Page) -> bool:
        """Check if the current modal step has a Submit button."""
        for sel in MODAL_SUBMIT_SELECTORS[:2]:  # Check specific selectors first
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip().lower()
                    if "submit" in text:
                        return True
            except Exception:
                continue
        return False

    async def _is_review_step(self, page: Page) -> bool:
        """Check if the current modal step has a Review button."""
        for sel in MODAL_REVIEW_SELECTORS[:2]:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip().lower()
                    if "review" in text:
                        return True
            except Exception:
                continue
        return False

    async def _handle_resume_upload(self, page: Page, resume_path: str) -> None:
        """Upload resume if a file input is present on the current modal step."""
        if not resume_path:
            return

        for sel in RESUME_UPLOAD_SELECTORS:
            try:
                file_input = await page.query_selector(sel)
                if file_input:
                    await file_input.set_input_files(resume_path)
                    logger.info("LinkedIn: Uploaded resume from %s", resume_path)
                    await random_delay(1.0, 2.0)
                    return
            except Exception:
                continue

    async def _close_modal(self, page: Page) -> None:
        """Close the Easy Apply modal to leave a clean state."""
        await self.safe_click(page, MODAL_CLOSE_SELECTORS, timeout=2000)
        await asyncio.sleep(0.5)
        # Handle the "Discard application?" confirmation dialog
        try:
            discard_selectors = [
                "button[data-control-name='discard_application_confirm_btn']",
                "button[data-test-dialog-primary-btn]",
                "button.artdeco-modal__confirm-dialog-btn",
            ]
            await self.safe_click(page, discard_selectors, timeout=2000)
        except Exception:
            pass

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
                "LinkedIn: Apply result: success [%d/%d fields, llm=%s]",
                result.fields_filled, result.fields_total, result.used_llm,
            )
        else:
            logger.info(
                "LinkedIn: Apply result: failed: %s", result.failure_reason,
            )
        return result
