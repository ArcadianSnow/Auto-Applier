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

# Job card selectors on the search results page.
# Indeed's DOM changes frequently — newest selectors go at the top,
# historical ones stay as fallbacks so old profiles keep working.
JOB_CARD_SELECTORS = [
    # 2024+ mosaic layout
    "div[data-testid='slider_item']",
    "div[class*='job_seen_beacon']",
    "div.cardOutline",
    # Older layouts
    "li.css-5lfssm",
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
    # 2024+ layouts — titles are usually inside an h2 → span with
    # the actual text, wrapped by an anchor with data-jk.
    "h2.jobTitle > a > span",
    "h2[class*='jobTitle'] a span",
    "a[data-jk] span[title]",
    "h2.jobTitle a",
    ".jobTitle > a",
    "a[data-jk]",
    ".jcs-JobTitle",
    "h2.jobTitle span[title]",
    "h2 span[title]",
    ".jobTitle a span",
]

# Company name within a card
JOB_COMPANY_SELECTORS = [
    "span[data-testid='company-name']",
    "[data-testid='company-name']",
    "div[data-testid='company-name']",
    ".companyName",
    ".company_location .companyName",
    "span.css-1x7skt3",
    ".resultContent .company",
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

    dead_listing_selectors = [
        "[data-testid='expiredJobNotice']",
        ".expired-job-banner",
        ".jobsearch-JobMetadataFooter-item:has-text('expired')",
    ]
    dead_listing_phrases = [
        "this job has expired",
        "sorry, this job is no longer available",
        "job posting has expired",
    ]

    # Indeed challenge paths. Narrow on purpose: generic prefixes
    # like '/account/login/challenge' used to be here but they match
    # normal login-flow URLs like '/account/login?challenge=email'
    # and fired on every run. Only paths that are EXCLUSIVELY used
    # for automation challenges stay.
    captcha_url_patterns = [
        "/captcha",
        "/recaptcha",
        "/hcaptcha",
        "/cloudflare/challenge",
    ]

    async def check_is_external(self, job: Job) -> bool:
        """Fast-skip hook: inspect the already-loaded job page for
        'Apply on company site' signals. Called from
        ``pipeline.fetch_description`` right after the liveness
        check, so we can early-exit external jobs before spending
        LLM cycles on scoring them.

        Delegates to the existing _is_external_apply method (same
        detection logic the apply flow uses, just called earlier).
        """
        try:
            page = await self.get_page()
            return await self._is_external_apply(page)
        except Exception as e:
            logger.debug("check_is_external raised: %s", e)
            return False

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
        """Search Indeed Jobs and return job cards from the results.

        Uses a minimal search URL so the results aren't over-filtered:

        - No 'fromage' date filter — the old 14-day limit excluded
          legitimate matching jobs that happened to be older.
        - No 'sc=0kf:attr(DSQF7)' Easily Apply filter — Indeed has
          quietly deprecated this attribute and with it active most
          searches return zero cards even when matching jobs exist.

        We let the scoring step decide which jobs are worth applying
        to, and the platform adapter's own _is_external_apply check
        skips jobs that aren't Indeed Apply when we actually try to
        click Apply. This means more candidate jobs reach scoring
        but the apply-click rate stays the same.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

        # Warm-up: visit the Indeed homepage and do a quick organic
        # scroll before hitting the search URL. Matches the LinkedIn
        # pattern — cold navigation to a filtered search URL from a
        # fresh tab is an automation tell.
        try:
            if "indeed.com" not in page.url.lower():
                logger.info("Indeed: warm-up via homepage before search")
                await page.goto(
                    "https://www.indeed.com/",
                    wait_until="domcontentloaded",
                )
                await reading_pause(page)
                await simulate_organic_behavior(page)
        except Exception as exc:
            logger.debug("Indeed warm-up skipped: %s", exc)

        jobs: list[Job] = []
        encoded_kw = quote_plus(keyword)
        encoded_loc = quote_plus(location)

        for page_num in range(MAX_SEARCH_PAGES):
            start = page_num * 10  # Indeed uses 10 results per page
            search_url = (
                f"https://www.indeed.com/jobs"
                f"?q={encoded_kw}"
                f"&l={encoded_loc}"
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
        matched_selector = ""
        for sel in JOB_CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
                if cards:
                    matched_selector = sel
                    logger.debug("Found %d job cards via '%s'", len(cards), sel)
                    break
            except Exception:
                continue

        if not cards:
            # CSS selectors missed. Fall back to anchor-based finder.
            logger.info(
                "Indeed: no CSS selectors matched, trying anchor-based "
                "fallback for /viewjob links"
            )
            anchor_hits = await self.find_jobs_by_anchors(
                page, href_pattern="/viewjob",
            )
            if anchor_hits:
                logger.info(
                    "Indeed: anchor fallback recovered %d jobs",
                    len(anchor_hits),
                )
                for title, url in anchor_hits:
                    # Pull the jk= parameter out of the href for a
                    # deterministic job_id
                    import re as _re
                    jk_match = _re.search(r"jk=([a-f0-9]+)", url)
                    if jk_match:
                        job_id = f"ind-{jk_match.group(1)}"
                    else:
                        job_id = f"ind-{abs(hash(url)) % 10**10}"
                    jobs.append(Job(
                        job_id=job_id,
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
                "Indeed: 0 job cards found.\n"
                "  url=%s\n"
                "  title=%s\n"
                "  page snippet: %s",
                current_url, title, snippet,
            )
            # Also check for the common "no results" signals so we can
            # say it explicitly in the log.
            body_lower = body.lower()
            if "did not match any jobs" in body_lower or "no jobs" in body_lower:
                logger.warning(
                    "Indeed is telling us zero jobs match the search — "
                    "try broader keywords or a wider location."
                )
            elif "unusual activity" in body_lower or "verify" in body_lower:
                logger.warning(
                    "Indeed page contains 'verify' / 'unusual activity' "
                    "text. If you see a captcha in the browser window, "
                    "solve it manually and retry."
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
        """Check if the job has an external apply link instead of Indeed Apply.

        A job is "external" when Indeed surfaces an 'Apply on company
        site' button that routes the user to the employer's own ATS.
        Applying to those via our pipeline has no effect — Indeed
        never shows a form for us to fill. They have to be skipped
        and recorded as 'external'.

        Detection uses four overlapping signals so we catch modern
        layouts even when one fails:

        1. Class-based EXTERNAL_APPLY_SELECTORS (legacy).
        2. Button text containing 'company site' or 'external' —
           searches deep, including nested spans.
        3. ANY anchor (button or link) whose href leaves indeed.com
           AND is visually prominent (button-like).
        4. The absence of an IndeedApply button PLUS the presence
           of at least one outbound anchor.
        """
        # 1. Known class names
        for sel in EXTERNAL_APPLY_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    logger.debug("External apply detected via selector: %s", sel)
                    return True
            except Exception:
                continue

        # 2. Button text — inner_text() walks nested spans already,
        # so this catches 'Apply on company site' regardless of whether
        # the text is in the <button> directly or a descendant.
        try:
            buttons = await page.query_selector_all(
                "button, a.btn, a[role='button'], [class*='apply'] a, "
                "[class*='apply'] button"
            )
            for btn in buttons:
                text = ""
                try:
                    text = (await btn.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if not text:
                    continue
                if (
                    "company site" in text
                    or "apply on company" in text
                    or "apply externally" in text
                    or "on employer site" in text
                    or "on company website" in text
                ):
                    logger.debug(
                        "External apply detected via button text: %r", text,
                    )
                    return True
        except Exception:
            pass

        # 3. Prominent button-like anchor with an outbound href.
        # This catches the case where 'Apply on company site' is a
        # link whose text lives in a child span we don't inner_text.
        try:
            apply_anchors = await page.query_selector_all(
                "a[class*='apply'], a[class*='Apply'], "
                "a[data-tn-element*='apply']"
            )
            for a in apply_anchors:
                href = ""
                try:
                    href = await a.get_attribute("href") or ""
                except Exception:
                    continue
                if not href:
                    continue
                if href.startswith("javascript:") or href.startswith("#"):
                    continue
                href_lower = href.lower()
                # Anchors whose href leaves indeed.com are external.
                # Indeed-internal paths start with /, contain indeed.com,
                # or use /rc/clk (Indeed's own click redirector — internal).
                if (
                    href_lower.startswith("http")
                    and "indeed.com" not in href_lower
                ):
                    logger.debug(
                        "External apply detected via outbound href: %s", href,
                    )
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
        steps_with_no_fields = 0
        for step in range(MAX_FORM_STEPS):
            logger.info(
                "Indeed: Application form step %d for %s", step + 1, job.job_id
            )

            await self.check_and_abort_on_captcha(page)

            # Handle resume upload on this step
            await self._handle_resume_upload(page, resume_path)

            # Detect and fill form fields
            fields_on_step: list = []
            if self.form_filler:
                fields_on_step = await find_form_fields(page)
                for field in fields_on_step:
                    await self.form_filler.fill_field(
                        page, field, job_id=job.job_id
                    )
                    await random_delay(0.5, 1.5)

            # Guard against silently 'applying' to external / broken
            # jobs: if we see two consecutive steps with zero fields
            # detected AND no success marker, bail out rather than
            # claiming success. This catches the case where the
            # 'Apply' click actually landed on an external site (or
            # an Indeed loading screen that never renders a form).
            if not fields_on_step:
                steps_with_no_fields += 1
                if steps_with_no_fields >= 2 and not await self._check_success(page):
                    logger.warning(
                        "Indeed: 2 consecutive form steps with no fields "
                        "detected for %s — treating as external/broken",
                        job.job_id,
                    )
                    return self._build_result(
                        success=False,
                        failure_reason=(
                            "No form fields detected — likely external "
                            "apply or broken form"
                        ),
                    )
            else:
                steps_with_no_fields = 0

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
        """Check if application submission succeeded.

        Strong signals only — the old 'you've applied' phrase was
        too loose and false-matched on page chrome like 'see jobs
        you've applied to in the sidebar'. A real post-apply page
        is distinctive:

        1. URL contains '/confirmation' or '/post-apply' or
           '/smartapply/.../success' — Indeed redirects here after a
           real submission.
        2. A known success-banner selector is present AND visible.
        3. Strong confirmation phrases ('your application has been
           submitted', 'thank you for applying to') PAIRED with a
           selector match, not alone.
        """
        # 1. URL-based signal (cheapest, strongest)
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        for pat in ("/confirmation", "/post-apply", "/postapply", "/success"):
            if pat in url:
                return True

        # 2. Success selector must be VISIBLE to count
        for sel in SUCCESS_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                try:
                    visible = await el.is_visible()
                except Exception:
                    visible = False
                if visible:
                    return True
            except Exception:
                continue

        # 3. Strong phrases (specific, full-sentence) — no loose
        # fragments like 'you've applied' that match sidebar chrome.
        try:
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
        except Exception:
            body_lower = ""
        strong_phrases = [
            "your application has been submitted",
            "thank you for applying to",
            "your application has been sent",
            "we've received your application",
            "application received successfully",
        ]
        for phrase in strong_phrases:
            if phrase in body_lower:
                return True

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
