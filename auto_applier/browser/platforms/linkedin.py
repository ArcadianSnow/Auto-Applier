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

    # LinkedIn's account-verification and challenge paths. Hitting any
    # of these means LinkedIn is about to ask for 2FA, a phone number,
    # or a CAPTCHA — we should stop before the pipeline makes it worse.
    captcha_url_patterns = [
        "/captcha",
        "/recaptcha",
        "/checkpoint/challenge",
        "/checkpoint/lg/login-submit",
        "/uas/login-submit",
        "/authwall",
    ]

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """Navigate to LinkedIn and verify the user is logged in.

        If not logged in, navigates to the login page and waits up to
        5 minutes for the user to log in manually.
        """
        page = await self.get_page()

        # Navigate to feed to check login state
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await random_delay(2.0, 4.0)

        # Check if already logged in
        logged_in = await self.safe_query(page, LOGGED_IN_SELECTORS, timeout=5000)
        if logged_in:
            logger.info("LinkedIn: Already logged in")
            return True

        # Not logged in -- navigate to login page
        logger.info("LinkedIn: Not logged in. Navigating to login page for manual login...")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        # Wait for user to log in manually (5 minute timeout)
        return await self.wait_for_manual_login(
            page,
            check_url_pattern="/feed",
            check_selector=LOGGED_IN_SELECTORS[0],
            timeout=300,
        )

    # ------------------------------------------------------------------
    # Job Search
    # ------------------------------------------------------------------

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search LinkedIn Jobs with Easy Apply filter enabled.

        Paginates through up to MAX_SEARCH_PAGES pages and parses
        job cards from the results.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

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
            clicked = await self.safe_click(page, EASY_APPLY_BUTTON_SELECTORS, timeout=5000)
            if not clicked:
                return ApplyResult(
                    success=False,
                    failure_reason="Easy Apply button not found -- job may require external application",
                )

            await random_delay(1.5, 3.0)
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
        """
        for step in range(MAX_MODAL_STEPS):
            logger.info("LinkedIn: Easy Apply step %d for %s", step + 1, job.job_id)

            await self.check_and_abort_on_captcha(page)

            # Handle resume upload on this step
            await self._handle_resume_upload(page, resume_path)

            # Detect and fill form fields
            if self.form_filler:
                fields = await find_form_fields(page)
                for field in fields:
                    await self.form_filler.fill_field(page, field, job_id=job.job_id)
                    await random_delay(0.5, 1.5)

            await simulate_organic_behavior(page)

            # Determine which button to click: Submit, Review, or Next
            is_submit_step = await self._is_submit_step(page)
            is_review_step = await self._is_review_step(page)

            if is_submit_step:
                if dry_run:
                    logger.info(
                        "LinkedIn: DRY RUN -- would submit application for %s",
                        job.job_id,
                    )
                    await self._close_modal(page)
                    return self._build_result(success=True, dry_run=True)

                # Submit the application
                clicked = await self.safe_click(page, MODAL_SUBMIT_SELECTORS, timeout=3000)
                if clicked:
                    await random_delay(2.0, 4.0)
                    logger.info("LinkedIn: Submitted application for %s", job.job_id)
                    # Dismiss any post-submit dialog
                    await self.safe_click(page, MODAL_CLOSE_SELECTORS, timeout=2000)
                    return self._build_result(success=True, dry_run=False)
                else:
                    return self._build_result(
                        success=False,
                        failure_reason="Submit button not found on submit step",
                    )

            elif is_review_step:
                clicked = await self.safe_click(page, MODAL_REVIEW_SELECTORS, timeout=3000)
                if not clicked:
                    # Try the generic primary button
                    clicked = await self.safe_click(
                        page, MODAL_NEXT_SELECTORS, timeout=3000
                    )
                if not clicked:
                    return self._build_result(
                        success=False,
                        failure_reason="Review button not found",
                    )
                await random_delay(1.5, 3.0)

            else:
                # Click Next to advance to the next step
                clicked = await self.safe_click(page, MODAL_NEXT_SELECTORS, timeout=3000)
                if not clicked:
                    # Maybe we reached an unknown state
                    logger.warning(
                        "LinkedIn: No Next/Review/Submit button found at step %d",
                        step + 1,
                    )
                    return self._build_result(
                        success=False,
                        failure_reason=f"No navigation button found at step {step + 1}",
                    )
                await random_delay(1.5, 3.0)

        # Exceeded max steps
        logger.warning("LinkedIn: Exceeded %d modal steps for %s", MAX_MODAL_STEPS, job.job_id)
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
            return ApplyResult(
                success=success,
                gaps=list(self.form_filler.gaps),
                resume_used=self.form_filler.resume_label,
                cover_letter_generated=self.form_filler.cover_letter_generated,
                failure_reason=failure_reason,
                fields_filled=self.form_filler.fields_filled,
                fields_total=self.form_filler.fields_total,
                used_llm=self.form_filler.used_llm,
            )
        return ApplyResult(
            success=success,
            failure_reason=failure_reason,
        )
