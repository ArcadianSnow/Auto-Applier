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

# Indicators that the user is logged in
LOGGED_IN_SELECTORS = [
    "[data-cy='user-menu']",
    ".user-menu",
    "img.user-avatar",
    "[data-cy='header-user-menu']",
    "a[href*='/dashboard']",
    ".header-user-info",
    "[aria-label='User Menu']",
    "a[href*='/profile']",
    ".avatar-img",
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

    async def ensure_logged_in(self) -> bool:
        """Navigate to Dice and verify the user is logged in.

        If not logged in, navigates to the login page and waits up to
        5 minutes for the user to log in manually.
        """
        page = await self.get_page()

        # Navigate to Dice home to check login state
        await page.goto("https://www.dice.com/", wait_until="domcontentloaded")
        await random_delay(2.0, 4.0)

        # Check if already logged in
        logged_in = await self.safe_query(page, LOGGED_IN_SELECTORS, timeout=5000)
        if logged_in:
            logger.info("Dice: Already logged in")
            return True

        # Not logged in -- navigate to login page
        logger.info(
            "Dice: Not logged in. Navigating to login page for manual login..."
        )
        await page.goto(
            "https://www.dice.com/dashboard/login", wait_until="domcontentloaded"
        )

        # Wait for user to log in manually (5 minute timeout)
        return await self.wait_for_manual_login(
            page,
            check_url_pattern="dice.com",
            check_selector=LOGGED_IN_SELECTORS[0],
            timeout=300,
        )

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
                    failure_reason="Easy Apply button not found -- job may require external application",
                )

            await random_delay(1.5, 3.0)

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
                logger.info(
                    "Dice: apply redirected to login — waiting for "
                    "manual login (timeout=120s)..."
                )
                try:
                    await page.wait_for_url(
                        lambda u: "/login" not in u and "/register" not in u,
                        timeout=120000,
                    )
                    logger.info("Dice: login completed, continuing apply flow")
                    await random_delay(1.0, 2.0)
                except Exception:
                    return ApplyResult(
                        success=False,
                        failure_reason="apply login gate timed out — please log in to Dice",
                    )

            # Check for external ATS redirect (new tab opened)
            if await self._check_ats_redirect(pages_before):
                return ApplyResult(
                    success=False,
                    failure_reason="External ATS redirect detected -- new tab opened to company site",
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

    async def _check_ats_redirect(self, pages_before: int) -> bool:
        """Check if clicking apply opened a new tab (external ATS).

        If a new tab was opened, close it and return True.
        """
        await asyncio.sleep(1.0)  # Brief wait for new tab to appear

        pages_after = len(self.context.pages)
        if pages_after > pages_before:
            logger.info("Dice: External ATS redirect detected (new tab opened)")
            # Close the new tab(s) to leave a clean state
            for p in self.context.pages[pages_before:]:
                try:
                    await p.close()
                except Exception:
                    pass
            return True
        return False

    async def _walk_easy_apply_modal(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through each step of the Dice Easy Apply modal.

        Handles: form fields, resume upload, Next/Submit buttons.
        Returns when either Submit is clicked or an error occurs.
        """
        for step in range(MAX_MODAL_STEPS):
            logger.info(
                "Dice: Easy Apply step %d for %s", step + 1, job.job_id
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
        """Upload resume if a file input is present on the current modal step."""
        if not resume_path:
            return

        for sel in RESUME_UPLOAD_SELECTORS:
            try:
                file_input = await page.query_selector(sel)
                if file_input:
                    await file_input.set_input_files(resume_path)
                    logger.info("Dice: Uploaded resume from %s", resume_path)
                    await random_delay(1.0, 2.0)
                    return
            except Exception:
                continue

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
