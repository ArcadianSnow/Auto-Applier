"""Indeed platform adapter for job search and Easy Apply automation.

This adapter handles:
- Manual login detection and prompting
- Job search with Easy Apply ("Easily apply") filter
- Job card parsing from search results
- Application form walking (screener questions)
- Form field filling via FormFiller
- CAPTCHA detection and hard stop
- External redirect detection (skip jobs that leave Indeed)

IMPORTANT: Indeed changes its DOM frequently. Every selector here has
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
    "[data-gnav-element-name='AccountMenu']",
    "#AccountMenu",
    "a[href*='/account']",
    "[data-testid='gnav-AccountMenu']",
    ".gnav-AccountMenu",
    "img.dd-header-image",
    "[aria-label='Account']",
    "a[data-gnav-element-name='Settings']",
]

# Job card selectors on the search results page
JOB_CARD_SELECTORS = [
    ".job_seen_beacon",
    ".resultContent",
    "[data-jk]",
    ".jobsearch-ResultsList .result",
    ".slider_item",
    "div.job_seen_beacon.mosaic-zone",
    "td.resultContent",
    "li .result",
]

# Job title within a card
JOB_TITLE_SELECTORS = [
    "h2.jobTitle a",
    ".jobTitle > a",
    "a[data-jk]",
    ".jcs-JobTitle",
    "h2.jobTitle span[title]",
    ".jobTitle a span",
]

# Company name within a card
JOB_COMPANY_SELECTORS = [
    "[data-testid='company-name']",
    ".companyName",
    ".company_location .companyName",
    "span.css-1x7skt3",
    ".resultContent .company",
    "span[data-testid='company-name']",
]

# Location within a card
JOB_LOCATION_SELECTORS = [
    "[data-testid='text-location']",
    ".companyLocation",
    ".company_location .companyLocation",
    "div.css-1restlb",
    ".resultContent .location",
]

# Apply button on job detail page
APPLY_BUTTON_SELECTORS = [
    "button#indeedApplyButton",
    "button[id*='indeedApply']",
    ".jobsearch-IndeedApplyButton-newDesign",
    "button[class*='IndeedApplyButton']",
    "button.ia-IndeedApplyButton",
    "#applyButtonLinkContainer button",
    "a.ia-IndeedApplyButton",
    "button[aria-label*='Apply']",
    "button[aria-label*='apply']",
]

# Job description on the detail page
JOB_DESCRIPTION_SELECTORS = [
    "#jobDescriptionText",
    ".jobsearch-JobComponent-description",
    ".jobsearch-jobDescriptionText",
    "[id='jobDescriptionText']",
    ".jobDescription",
    "#jobDescription",
]

# Indeed application form -- continue / next / submit buttons
FORM_CONTINUE_SELECTORS = [
    ".ia-continueButton",
    "button.ia-continueButton",
    "button[id*='ia-continueButton']",
    "button[data-testid='ia-continueButton']",
    "button.ia-BasePage-continue",
    "button[type='submit']",
    ".ia-Navigation-continue button",
]

FORM_SUBMIT_SELECTORS = [
    "button[id*='apply']",
    "button.ia-continueButton[type='submit']",
    "button[aria-label*='Submit']",
    "button[aria-label*='submit']",
    "button.ia-Review-submit",
    "[data-testid='submit-button']",
    "button.ia-BasePage-submit",
]

# Resume upload input
RESUME_UPLOAD_SELECTORS = [
    "input[type='file'][name*='resume']",
    "input[type='file'][name*='Resume']",
    "input[type='file'][accept*='.pdf']",
    "input[type='file'][id*='resume']",
    "input[type='file']",
]

# Application success indicators
SUCCESS_SELECTORS = [
    ".ia-PostApply",
    ".ia-PostApply-header",
    "[data-testid='post-apply']",
    ".jobsearch-PostApplyBanner",
    "h1.ia-PostApply-header",
    "[class*='PostApply']",
    "div[class*='application-success']",
]

# External apply indicators (jobs that redirect off-site)
EXTERNAL_APPLY_SELECTORS = [
    "button[class*='ExternalApply']",
    "a[target='_blank'][class*='apply']",
    "[data-tn-element='apply-external']",
    "a[href*='clk?jk=']",
    ".jobsearch-IndeedApplyButton-modalCloseButton",
]

# Max pagination and result limits
MAX_SEARCH_PAGES = 3
MAX_JOBS_PER_SEARCH = 25
MAX_FORM_STEPS = 10  # Safety limit for multi-step forms


class IndeedPlatform(JobPlatform):
    """Indeed job search and Easy Apply adapter.

    Requires the user to be logged in manually -- this adapter will
    never automate credential entry. It navigates to Indeed, checks
    for login indicators, and waits for the user if needed.
    """

    source_id = "indeed"
    display_name = "Indeed"

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """Navigate to Indeed and verify the user is logged in.

        If not logged in, navigates to the auth page and waits up to
        5 minutes for the user to log in manually.
        """
        page = await self.get_page()

        # Navigate to Indeed home to check login state
        await page.goto("https://www.indeed.com/", wait_until="domcontentloaded")
        await random_delay(2.0, 4.0)

        # Check if already logged in
        logged_in = await self.safe_query(page, LOGGED_IN_SELECTORS, timeout=5000)
        if logged_in:
            logger.info("Indeed: Already logged in")
            return True

        # Not logged in -- navigate to auth page
        logger.info(
            "Indeed: Not logged in. Navigating to auth page for manual login..."
        )
        await page.goto(
            "https://secure.indeed.com/auth", wait_until="domcontentloaded"
        )

        # Wait for user to log in manually (5 minute timeout)
        return await self.wait_for_manual_login(
            page,
            check_url_pattern="indeed.com",
            check_selector=LOGGED_IN_SELECTORS[0],
            timeout=300,
        )

    # ------------------------------------------------------------------
    # Job Search
    # ------------------------------------------------------------------

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search Indeed Jobs with Easy Apply filter enabled.

        Uses the ``fromage=14`` param for last 14 days and the
        ``sc=0kf:attr(DSQF7)`` param for Easily Apply filter.
        Paginates through up to MAX_SEARCH_PAGES pages.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

        jobs: list[Job] = []
        encoded_kw = quote_plus(keyword)
        encoded_loc = quote_plus(location)

        for page_num in range(MAX_SEARCH_PAGES):
            start = page_num * 10  # Indeed uses 10 results per page
            search_url = (
                f"https://www.indeed.com/jobs"
                f"?q={encoded_kw}"
                f"&l={encoded_loc}"
                f"&fromage=14"
                f"&sc=0kf%3Aattr(DSQF7)%3B"
                f"&start={start}"
            )

            logger.info(
                "Indeed: Searching page %d -- %s %s",
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
                logger.info("Indeed: No more job cards found, stopping pagination")
                break

            # Organic delay between pages
            await simulate_organic_behavior(page)
            await random_delay(3.0, 6.0)

        logger.info(
            "Indeed: Found %d jobs for '%s' in '%s'", len(jobs), keyword, location
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
            logger.warning("Indeed: No job cards found on page")
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
            # Try getting title from span with title attribute
            try:
                span_el = await card.query_selector("h2 span[title]")
                if span_el:
                    title = (await span_el.get_attribute("title")) or ""
                    title = title.strip()
            except Exception:
                pass

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

        # Get job ID from data-jk attribute or link
        job_id = ""
        job_url = ""

        # Try data-jk attribute (Indeed's job key)
        try:
            jk = await card.get_attribute("data-jk")
            if jk:
                job_id = f"ind-{jk}"
                job_url = f"https://www.indeed.com/viewjob?jk={jk}"
        except Exception:
            pass

        # Fallback: try finding a link with jk in the URL
        if not job_id:
            try:
                link_el = await card.query_selector("a[data-jk]")
                if link_el:
                    jk = await link_el.get_attribute("data-jk")
                    if jk:
                        job_id = f"ind-{jk}"
                        job_url = f"https://www.indeed.com/viewjob?jk={jk}"
            except Exception:
                pass

        # Fallback: try extracting from href
        if not job_id:
            try:
                link_el = await card.query_selector("a[href*='jk=']")
                if not link_el:
                    link_el = await card.query_selector("h2 a")
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    match = re.search(r"jk=([a-f0-9]+)", href)
                    if match:
                        jk = match.group(1)
                        job_id = f"ind-{jk}"
                        job_url = f"https://www.indeed.com/viewjob?jk={jk}"
                    elif href:
                        job_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.indeed.com{href}"
                        )
            except Exception:
                pass

        if not job_id:
            # Generate a fallback ID from title + company
            job_id = f"ind-{hash(title + company) % 10**8}"

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
                "Indeed: No URL for job %s, cannot fetch description", job.job_id
            )
            return ""

        await reading_pause(page)
        await self.check_and_abort_on_captcha(page)

        # Extract description text
        description = await self.safe_get_text(
            page, JOB_DESCRIPTION_SELECTORS, timeout=5000
        )

        if description:
            logger.debug(
                "Indeed: Got description for %s (%d chars)",
                job.job_id,
                len(description),
            )
        else:
            logger.warning(
                "Indeed: Could not extract description for %s", job.job_id
            )

        return description

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """Walk the Indeed application form and fill all fields.

        In dry_run mode, fills fields and navigates steps but does not
        click the final Submit button. Detects and skips external
        redirects (jobs that leave Indeed for a company ATS).
        """
        page = await self.get_page()

        try:
            # Navigate to the job if needed
            if job.url and job.url not in page.url:
                await page.goto(job.url, wait_until="domcontentloaded")
                await reading_pause(page)

            await self.check_and_abort_on_captcha(page)

            # Check for external apply (redirects to company site)
            is_external = await self._is_external_apply(page)
            if is_external:
                logger.info(
                    "Indeed: Job %s redirects to external site, skipping",
                    job.job_id,
                )
                return ApplyResult(
                    success=False,
                    failure_reason="External application -- redirects to company site",
                )

            # Click the Apply / Easy Apply button
            clicked = await self.safe_click(
                page, APPLY_BUTTON_SELECTORS, timeout=5000
            )
            if not clicked:
                return ApplyResult(
                    success=False,
                    failure_reason="Apply button not found -- job may require external application",
                )

            await random_delay(1.5, 3.0)

            # Check if we got redirected externally after clicking
            if await self._check_external_redirect(page):
                return ApplyResult(
                    success=False,
                    failure_reason="Apply click redirected to external site",
                )

            await self.check_and_abort_on_captcha(page)

            # Walk the application form
            return await self._walk_application_form(
                page, job, resume_path, dry_run
            )

        except CaptchaDetectedError as exc:
            logger.error("Indeed: %s", exc)
            return ApplyResult(
                success=False,
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.error(
                "Indeed: Apply failed for %s: %s", job.job_id, exc
            )
            return ApplyResult(
                success=False,
                failure_reason=f"Unexpected error: {exc}",
            )

    async def _is_external_apply(self, page: Page) -> bool:
        """Check if the job has an external apply link instead of Indeed Apply."""
        for sel in EXTERNAL_APPLY_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    return True
            except Exception:
                continue

        # Also check button text for "apply on company site" variations
        try:
            buttons = await page.query_selector_all("button, a.btn")
            for btn in buttons:
                text = (await btn.inner_text()).strip().lower()
                if "company site" in text or "external" in text:
                    return True
        except Exception:
            pass

        return False

    async def _check_external_redirect(self, page: Page) -> bool:
        """Check if clicking apply opened a new tab or left Indeed."""
        # Check if we left indeed.com
        if "indeed.com" not in page.url:
            return True

        # Check if a new page/tab was opened
        pages = self.context.pages
        if len(pages) > 1:
            # Close the external tab and return to the original
            for p in pages[1:]:
                try:
                    await p.close()
                except Exception:
                    pass
            return True

        return False

    async def _walk_application_form(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through Indeed's application form steps.

        Indeed forms are generally simpler than LinkedIn's -- often
        a single page with resume upload and a few screener questions.
        Some jobs have multi-step forms with continue buttons.
        """
        for step in range(MAX_FORM_STEPS):
            logger.info(
                "Indeed: Application form step %d for %s", step + 1, job.job_id
            )

            await self.check_and_abort_on_captcha(page)

            # Handle resume upload on this step
            await self._handle_resume_upload(page, resume_path)

            # Detect and fill form fields
            if self.form_filler:
                fields = await find_form_fields(page)
                for field in fields:
                    await self.form_filler.fill_field(
                        page, field, job_id=job.job_id
                    )
                    await random_delay(0.5, 1.5)

            await simulate_organic_behavior(page)

            # Check for success indicators (already applied)
            if await self._check_success(page):
                logger.info(
                    "Indeed: Application success detected for %s", job.job_id
                )
                return self._build_result(success=True, dry_run=False)

            # Check if this is the submit step
            is_submit = await self._is_submit_step(page)

            if is_submit:
                if dry_run:
                    logger.info(
                        "Indeed: DRY RUN -- would submit application for %s",
                        job.job_id,
                    )
                    return self._build_result(success=True, dry_run=True)

                # Submit the application
                clicked = await self.safe_click(
                    page, FORM_SUBMIT_SELECTORS, timeout=3000
                )
                if clicked:
                    await random_delay(2.0, 4.0)

                    # Verify submission success
                    if await self._check_success(page):
                        logger.info(
                            "Indeed: Submitted application for %s", job.job_id
                        )
                        return self._build_result(success=True, dry_run=False)

                    # Even without explicit success indicator, if we
                    # clicked submit and no error appeared, consider it done
                    logger.info(
                        "Indeed: Submit clicked for %s (no explicit success indicator)",
                        job.job_id,
                    )
                    return self._build_result(success=True, dry_run=False)
                else:
                    return self._build_result(
                        success=False,
                        failure_reason="Submit button not found on submit step",
                    )

            else:
                # Click Continue to advance to the next step
                clicked = await self.safe_click(
                    page, FORM_CONTINUE_SELECTORS, timeout=3000
                )
                if not clicked:
                    # Maybe we're on a single-page form -- try submit directly
                    clicked = await self.safe_click(
                        page, FORM_SUBMIT_SELECTORS, timeout=3000
                    )
                    if clicked:
                        if dry_run:
                            logger.info(
                                "Indeed: DRY RUN -- would submit for %s",
                                job.job_id,
                            )
                            return self._build_result(success=True, dry_run=True)
                        await random_delay(2.0, 4.0)
                        logger.info(
                            "Indeed: Submit clicked (single-page) for %s",
                            job.job_id,
                        )
                        return self._build_result(success=True, dry_run=False)

                    logger.warning(
                        "Indeed: No Continue/Submit button found at step %d",
                        step + 1,
                    )
                    return self._build_result(
                        success=False,
                        failure_reason=f"No navigation button found at step {step + 1}",
                    )
                await random_delay(1.5, 3.0)

        # Exceeded max steps
        logger.warning(
            "Indeed: Exceeded %d form steps for %s", MAX_FORM_STEPS, job.job_id
        )
        return self._build_result(
            success=False,
            failure_reason=f"Exceeded maximum form steps ({MAX_FORM_STEPS})",
        )

    async def _is_submit_step(self, page: Page) -> bool:
        """Check if the current form step has a Submit button."""
        for sel in FORM_SUBMIT_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip().lower()
                    if "submit" in text or "apply" in text:
                        return True
            except Exception:
                continue

        # Check by button text content
        try:
            buttons = await page.query_selector_all("button")
            for btn in buttons:
                text = (await btn.inner_text()).strip().lower()
                if text in ("submit application", "submit", "apply now", "apply"):
                    return True
        except Exception:
            pass

        return False

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
                "you've applied",
                "thank you for applying",
            ]
            for phrase in success_phrases:
                if phrase in body_lower:
                    return True
        except Exception:
            pass

        return False

    async def _handle_resume_upload(self, page: Page, resume_path: str) -> None:
        """Upload resume if a file input is present on the current form step."""
        if not resume_path:
            return

        for sel in RESUME_UPLOAD_SELECTORS:
            try:
                file_input = await page.query_selector(sel)
                if file_input:
                    await file_input.set_input_files(resume_path)
                    logger.info("Indeed: Uploaded resume from %s", resume_path)
                    await random_delay(1.0, 2.0)
                    return
            except Exception:
                continue

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
