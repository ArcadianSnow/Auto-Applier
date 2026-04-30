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

# Indeed application form -- continue / next / submit buttons.
#
# Indeed's smartapply flow migrated to a React-based app hosted at
# smartapply.indeed.com/beta/indeedapply/form/* and the old CSS class
# names (.ia-continueButton, .ia-BasePage-continue) no longer match.
# The text-based selectors at the end of each list are stable across
# UI rewrites because they match rendered button content, not DOM
# structure.
FORM_CONTINUE_SELECTORS = [
    # Modern smartapply (React)
    "button[data-testid='continue-button']",
    "button[data-testid='IndeedApplyButton-continue']",
    "button[data-cy='continue-button']",
    "button.css-[class*='continue']",
    # Legacy smartapply
    ".ia-continueButton",
    "button.ia-continueButton",
    "button[id*='ia-continueButton']",
    "button[data-testid='ia-continueButton']",
    "button.ia-BasePage-continue",
    ".ia-Navigation-continue button",
    # Text-based fallbacks — survive any class rename as long as the
    # visible button copy stays in English.
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Save and continue')",
    # Generic last resort
    "button[type='submit']",
]

FORM_SUBMIT_SELECTORS = [
    # Modern smartapply
    "button[data-testid='submit-application']",
    "button[data-testid='submit-button']",
    "button[data-testid='IndeedApplyButton-submit']",
    "button[data-cy='submit-application']",
    # Legacy
    "button[id*='apply']",
    "button.ia-continueButton[type='submit']",
    "button[aria-label*='Submit']",
    "button[aria-label*='submit']",
    "button.ia-Review-submit",
    "[data-testid='submit-button']",
    "button.ia-BasePage-submit",
    # Text-based fallbacks
    "button:has-text('Submit your application')",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
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

    async def _click_nav_button_by_text(
        self, page: Page, patterns: tuple[str, ...],
    ) -> bool:
        """Last-ditch Continue/Submit click by scanning every visible button.

        When the CSS selector list doesn't match (new smartapply
        flows, renamed classes, button outside the form element),
        iterate every ``<button>`` on the page and click the first
        one whose accessible text contains any of ``patterns``
        (case-insensitive). This trades precision for resilience —
        once we've decided the form is ready and all fields are
        filled, any button that reads "Continue" or "Save and
        continue" is almost certainly the navigation button.

        Returns True on a successful click.
        """
        # Scope matters — Indeed's smartapply has a left-rail progress
        # stepper where each step label contains the word "Continue",
        # plus header "Continue as guest" / "Continue with Google"
        # buttons that aren't form navigation at all. Prefer buttons
        # inside a ``<form>`` element first; only fall through to the
        # whole page if none of those match.
        scoped: list = []
        try:
            scoped = await page.query_selector_all(
                "form button, form [role='button']"
            )
        except Exception:
            scoped = []
        try:
            fallback = await page.query_selector_all(
                "button, [role='button']"
            )
        except Exception:
            fallback = []
        buttons = list(scoped) + [b for b in fallback if b not in scoped]
        lowered = tuple(p.lower() for p in patterns)
        for btn in buttons:
            try:
                if not await btn.is_visible():
                    continue
                if not await btn.is_enabled():
                    continue
                text = (await btn.inner_text() or "").strip().lower()
                if not text:
                    # Accessible name fallback
                    text = (await btn.get_attribute("aria-label") or "").lower()
                if not text:
                    continue
                if any(p in text for p in lowered):
                    logger.info(
                        "Indeed: Text-scan click on button '%s'",
                        text[:40],
                    )
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_form_ready(self, page: Page, timeout: int = 5000) -> bool:
        """Wait for the smartapply form shell to actually render.

        The React app on smartapply.indeed.com lazily renders its form
        elements after the URL has already settled, which means a
        naive scan can see an empty DOM. We wait for ANY of these
        signals:

        - A labelled input (``label[for]``)
        - A ``<form>`` element with at least one descendant input
        - A navigation button (Continue / Submit) in any form
        - A resume upload input

        Returns True on success, False on timeout. Caller should
        proceed regardless — a stubborn page still deserves a scan
        attempt with whatever HTML is present.
        """
        # Combined CSS selector list — wait_for_selector returns as
        # soon as ANY one of these matches, so the total wall-clock
        # cost is at most `timeout` once, not per selector.
        combined = (
            "label[for], form input, form button, "
            "button[type='submit'], input[type='file']"
        )
        try:
            await page.wait_for_selector(
                combined, timeout=timeout, state="attached",
            )
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Module-aware dispatch
    # ------------------------------------------------------------------
    #
    # Indeed smartapply URLs follow a fixed vocabulary:
    #   /form/contact-info-module
    #   /form/profile-location
    #   /form/resume-selection-module/resume-selection
    #   /form/work-experience
    #   /form/education
    #   /form/questions
    #   /form/review-module
    #
    # Each page has its own quirks (radio decoys, pre-filled fields,
    # auto-advance on radio click, etc.) that made a single generic
    # walker brittle — every fix revealed the next module's surprise.
    # The dispatcher below routes by URL substring to a handler that
    # owns its page's specifics. Unknown URLs fall through to the
    # generic field-detect + form_filler path, which is what we used
    # to run unconditionally.

    async def _handle_contact_info(self, page: Page, job: Job) -> list:
        """contact-info-module: phone number (name/email pre-filled)."""
        fields = await find_form_fields(page)
        if self.form_filler:
            for field in fields:
                await self.form_filler.fill_field(
                    page, field, job_id=job.job_id,
                )
                await random_delay(0.3, 0.8)
        return fields

    async def _handle_resume_selection(
        self, page: Page, resume_path: str,
    ) -> list:
        """resume-selection-module: pick uploaded file, not the decoy.

        Two variants of this page exist:

        1. Radio picker — "your resume" vs "Build an Indeed Resume"
           decoy. Pre-dispatcher handler. Safe to call on the newer
           variant too (no-op if no radios).
        2. PDF preview — Indeed has already converted the upload to
           a PDF and shows a "Review it before you apply" notice.
           There's no radio; the user just clicks a "Use this" /
           "Continue" button. The walker's generic Continue click
           handles this, but we nudge the scroll position so the
           button is reachable inside the fixed viewport first.
        """
        await self._handle_resume_upload(page, resume_path)
        await self._select_uploaded_resume(page, resume_path)
        # Scroll to the bottom so any "continue-ish" CTA below a
        # PDF preview iframe is in view before safe_click / text-scan
        # run. Cheap, no-op if already at bottom.
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception:
            pass
        # Commit React state — the radio click DOM-set the picker but
        # React's controlled component won't see it until the prototype
        # setter fires through the synthetic event system. Same fix as
        # the questions module, just applied here too.
        if self.form_filler:
            await self.form_filler.commit_react_state(page, settle_seconds=0.4)
        return []

    async def _handle_questions(self, page: Page, job: Job) -> list:
        """questions-module: screener questions with custom widgets.

        Indeed's questions use non-standard DOM: not label/input pairs
        but custom React widgets. If find_form_fields returns 0, try
        broader detection and dump the DOM for debugging.
        """
        fields = await find_form_fields(page)
        if not fields:
            # Indeed questions use hash-based names (q_13ae933...)
            # with the actual question text in a parent wrapper.
            # Walk up from each visible input to find the real label.
            try:
                from auto_applier.browser.selector_utils import FormField, _classify_element
                # Find question containers — Indeed wraps each screener
                # in a container with a question-text element + input(s)
                question_labels = await page.evaluate("""() => {
                    const results = [];
                    const seen = new Set();
                    // Indeed wraps each question in a div whose class
                    // contains 'mosaic-provider-module-apply-questions'.
                    // The question text is in a child div/span BEFORE
                    // the inputs — no semantic label/legend/heading.
                    const wrappers = document.querySelectorAll(
                        '[class*=questions], [class*=question], ' +
                        'fieldset, [role=group], [role=radiogroup]'
                    );
                    for (const wrapper of wrappers) {
                        // querySelectorAll, not querySelector — a single
                        // question wrapper can hold multiple controls
                        // (number input + unit dropdown, address line
                        // 1 + line 2, salary range from + to). Picking
                        // only the first input dropped the rest as
                        // invisible-to-the-form-filler required fields.
                        const inputs = wrapper.querySelectorAll(
                            'input:not([type=hidden]), textarea, select'
                        );
                        if (inputs.length === 0) continue;
                        // Pick the first VISIBLE input as the
                        // representative for this wrapper's question
                        // text; emit one result per name afterwards.
                        let inp = null;
                        for (const candidate of inputs) {
                            if (candidate.offsetParent !== null) {
                                inp = candidate;
                                break;
                            }
                        }
                        if (!inp) continue;
                        const name = inp.name || '';
                        // Use this wrapper's first visible input only
                        // to derive the question text; the per-input
                        // emit loop below adds names to `seen`. If
                        // the first input's name is already seen,
                        // skip the entire wrapper to avoid re-deriving
                        // text we'd never end up using.
                        if (!name || seen.has(name)) continue;
                        // Extract question text: walk child elements
                        // and grab the first one with substantial text
                        // that isn't an option label (Yes/No/etc.)
                        let text = '';
                        const optionTexts = new Set();
                        wrapper.querySelectorAll('label').forEach(l => {
                            const t = l.innerText?.trim();
                            if (t && t.length < 30) optionTexts.add(t.toLowerCase());
                        });
                        // Check all direct and nested children for
                        // question text
                        for (const child of wrapper.querySelectorAll('*')) {
                            if (['INPUT','TEXTAREA','SELECT'].includes(child.tagName)) continue;
                            const t = child.innerText?.trim();
                            if (!t || t.length < 5 || t.length > 300) continue;
                            if (t.startsWith('q_')) continue;
                            if (optionTexts.has(t.toLowerCase())) continue;
                            // Skip if it contains multiple inputs (it's the whole wrapper)
                            if (child.querySelectorAll('input, textarea, select').length > 1) continue;
                            text = t;
                            break;
                        }
                        if (!text) continue;
                        // Filter out section headers that aren't
                        // actual questions — these corrupt the form
                        // when the filler tries to type into them.
                        const headerPhrases = [
                            'answer these questions',
                            'key qualifications',
                            'we\\x27ll save your answers',
                            'required fields are marked',
                        ];
                        if (headerPhrases.some(p => text.toLowerCase().includes(p))) continue;
                        // Emit one entry per UNIQUE named visible input
                        // inside this wrapper. A wrapper holding e.g.
                        // a number input + a unit dropdown both keyed
                        // off the same question gets two entries with
                        // the same shared question text — both routed
                        // through the form filler instead of just one.
                        for (const candidate of inputs) {
                            if (candidate.offsetParent === null) continue;
                            const cName = candidate.name || '';
                            if (!cName || seen.has(cName)) continue;
                            seen.add(cName);
                            results.push({
                                text: text.substring(0, 200),
                                name: cName,
                                type: candidate.type || candidate.tagName.toLowerCase(),
                                selector: '[name="' + cName + '"]',
                            });
                        }
                    }
                    return results;
                }""")
                for q in (question_labels or []):
                    if not q.get("text"):
                        continue
                    sel = q.get("selector")
                    if not sel:
                        continue
                    inp = await page.query_selector(sel)
                    if not inp:
                        continue
                    f = await _classify_element(inp, q["text"], page)
                    if f and f.label.lower() not in {
                        ff.label.lower() for ff in fields
                    }:
                        fields.append(f)
                        logger.debug(
                            "  questions: extracted label=%r for name=%s",
                            q["text"][:60], q.get("name", "?"),
                        )
            except Exception as e:
                logger.debug("questions handler broader detection failed: %s", e)

        # Sweep for any remaining required inputs the broader detection
        # missed. Indeed sometimes splits questions across pages whose
        # text inputs sit OUTSIDE the [class*=question] wrappers — the
        # walker would then click Continue while those fields are still
        # empty, hit "form stuck — required field unfilled", and bail.
        # This sweep grabs anything still missing by [required] /
        # aria-required, classifies it via the standard pipeline, and
        # appends to the fields list so the regular form_filler pass
        # can answer it.
        try:
            already_named: set[str] = set()
            for f in fields:
                try:
                    n = await f.element.get_attribute("name")
                    if n:
                        already_named.add(n)
                except Exception:
                    pass
            missing = await page.evaluate("""(seen) => {
                const seenSet = new Set(seen);
                const els = [...document.querySelectorAll(
                    'input:not([type=hidden]):not([type=button]):not([type=submit]),'
                    + 'textarea, select'
                )].filter(el => el.offsetParent !== null);

                // Pre-compute which radio groups have at least one
                // checked option. Indeed's React validation treats
                // *every* radio group as effectively required: a group
                // with no checked option blocks Continue even when no
                // individual radio carries `required=true`. Treat such
                // groups as missing so the sweep picks them up too.
                const radioGroupChecked = new Map();
                for (const el of els) {
                    if (el.type !== 'radio') continue;
                    const name = el.name || '';
                    if (!name) continue;
                    if (!radioGroupChecked.has(name)) {
                        radioGroupChecked.set(name, false);
                    }
                    if (el.checked) radioGroupChecked.set(name, true);
                }

                const out = [];
                const emittedRadioGroups = new Set();
                for (const el of els) {
                    const required = el.required ||
                        el.getAttribute('aria-required') === 'true';
                    const isUnansweredRadio = el.type === 'radio'
                        && !radioGroupChecked.get(el.name || '');
                    if (!required && !isUnansweredRadio) continue;
                    const elName = el.name || '';
                    const elId = el.id || '';
                    // Dedup key: prefer name, fall back to id. Keep
                    // them distinct in the output so Python builds the
                    // right kind of selector.
                    const dedup = elName || elId;
                    if (!dedup || seenSet.has(dedup)) continue;
                    // For radio groups, only emit ONE entry per group.
                    if (el.type === 'radio') {
                        if (emittedRadioGroups.has(elName)) continue;
                        emittedRadioGroups.add(elName);
                    }
                    // Only sweep empty fields — already-filled ones
                    // would be no-ops and would re-trigger LLM costs.
                    if (el.type === 'radio' || el.type === 'checkbox') {
                        if (el.checked) continue;
                    } else if ((el.value || '').length > 0) {
                        continue;
                    }
                    // Walk up the DOM looking for a label/legend/heading
                    // that names this field — same logic as the questions
                    // extractor, just less restrictive about wrapper class.
                    let label = '';
                    if (el.id) {
                        const lbl = el.ownerDocument.querySelector(
                            'label[for="' + el.id.replace(/"/g, '\\\\"') + '"]'
                        );
                        if (lbl) label = (lbl.innerText || '').trim();
                    }
                    if (!label) {
                        const wrap = el.closest('label');
                        if (wrap) label = (wrap.innerText || '').trim();
                    }
                    if (!label) {
                        // Climb until we hit a substantive text node.
                        let parent = el.parentElement;
                        for (let i = 0; i < 6 && parent && !label; i++) {
                            const t = (parent.innerText || '').trim();
                            if (t && t.length >= 5 && t.length <= 300) {
                                label = t.split('\\n')[0];
                            }
                            parent = parent.parentElement;
                        }
                    }
                    if (!label) label = elName || elId;
                    out.push({
                        name: elName,
                        id: elId,
                        label: label.substring(0, 200),
                    });
                }
                return out;
            }""", list(already_named))
            logger.info(
                "Indeed: questions sweep — %d already-named, %d missing-required candidate(s)",
                len(already_named), len(missing or []),
            )
            from auto_applier.browser.selector_utils import _classify_element
            for q in (missing or []):
                # Prefer the name attribute — Indeed's React-generated
                # IDs use unescaped colons (e.g., #number-input-:r1h:)
                # that break CSS parsing. Names use safe q_<hash> form.
                sel = ""
                if q.get("name"):
                    safe = q["name"].replace('"', '\\"')
                    sel = f'[name="{safe}"]'
                elif q.get("id"):
                    # CSS.escape ID — colons + dots in React IDs need it.
                    safe_id = await page.evaluate(
                        "id => CSS.escape(id)", q["id"],
                    )
                    sel = f"#{safe_id}"
                if not sel:
                    logger.info(
                        "  sweep: no selector for q=%s", q,
                    )
                    continue
                try:
                    inp = await page.query_selector(sel)
                    if not inp:
                        logger.info(
                            "  sweep: query_selector(%s) returned None", sel,
                        )
                        continue
                    f = await _classify_element(inp, q["label"], page)
                    if f is None:
                        logger.info(
                            "  sweep: _classify_element returned None for "
                            "label=%r selector=%s",
                            q["label"][:60], sel,
                        )
                        continue
                    if f.label.lower() in {ff.label.lower() for ff in fields}:
                        logger.info(
                            "  sweep: dropped duplicate label=%r",
                            q["label"][:60],
                        )
                        continue
                    fields.append(f)
                    logger.info(
                        "  sweep: added missed field label=%r name=%s type=%s",
                        q["label"][:60], q.get("name", "?"), f.field_type,
                    )
                except Exception as exc:
                    logger.info(
                        "  sweep: classify failed for %s: %s", sel, exc,
                    )
                    continue
        except Exception as exc:
            logger.info("questions sweep failed: %s", exc)

        if not fields:
            # Diagnostic dump — log what the questions page looks like
            try:
                diag = await page.evaluate("""() => {
                    const inputs = [...document.querySelectorAll(
                        'input, textarea, select, [role=radio], [role=checkbox]'
                    )].filter(el => el.offsetParent !== null).slice(0, 10);
                    return inputs.map(el => ({
                        tag: el.tagName,
                        type: el.type || '',
                        name: el.name || '',
                        aria: el.getAttribute('aria-label') || '',
                        role: el.getAttribute('role') || '',
                        parent: el.parentElement?.className?.substring(0, 60) || '',
                    }));
                }""")
                import json as _json
                logger.info(
                    "Indeed: questions-module diagnostic (0 fields): %s",
                    _json.dumps(diag, indent=2)[:1500],
                )
            except Exception:
                pass

        if self.form_filler:
            for field in fields:
                await self.form_filler.fill_field(
                    page, field, job_id=job.job_id,
                )
                await random_delay(0.5, 1.5)
            # Indeed's questions widgets are React-controlled — defer
            # to the shared FormFiller helper to commit state. See
            # FormFiller.commit_react_state for the full rationale.
            await self.form_filler.commit_react_state(page)
        return fields

    async def _handle_review(self, page: Page, job: Job) -> list:
        """review-module: nothing to fill, let submit logic fire."""
        return []

    async def _handle_generic(self, page: Page, job: Job) -> list:
        """Default path for unknown modules (profile-location, screeners)."""
        fields = await find_form_fields(page)
        if self.form_filler:
            for field in fields:
                await self.form_filler.fill_field(
                    page, field, job_id=job.job_id,
                )
                await random_delay(0.5, 1.5)
        return fields

    async def _dispatch_module(
        self, page: Page, job: Job, resume_path: str, url: str,
    ) -> tuple[str, list]:
        """Route the current page to the right module handler.

        Returns a ``(module_name, fields_filled)`` tuple. The module
        name goes into the stuck-loop signature and the log output
        so progress across modules is observable at a glance.
        """
        if "contact-info-module" in url:
            return "contact-info", await self._handle_contact_info(page, job)
        if "resume-selection" in url:
            return "resume-selection", await self._handle_resume_selection(
                page, resume_path,
            )
        if "review-module" in url:
            return "review", await self._handle_review(page, job)
        if "intervention" in url:
            # Indeed's screening intervention — "you may not qualify
            # but you can apply anyway". Click "Apply anyway" to
            # proceed, or bail if it's a hard block.
            logger.info(
                "Indeed: intervention page — clicking 'Apply anyway' if available"
            )
            return "intervention", []
        if "questions-module" in url or "questions/" in url:
            return "questions", await self._handle_questions(page, job)
        return "generic", await self._handle_generic(page, job)

    async def _click_continue_and_wait(
        self, page: Page, start_url: str, timeout: int = 8000,
    ) -> tuple[bool, str]:
        """Click Continue and wait for the page to actually advance.

        Returns ``(advanced, reason)`` where ``advanced`` is True if
        the URL or the visible step content changed, and ``reason``
        carries a human-readable description of the validation error
        if we can find one. Much better signal than "click returned
        True but form didn't move".

        Three advance signals:
        1. ``page.url`` changes
        2. ``[aria-current='step']`` stepper index increments
        3. A new form field appears that wasn't in our last scan

        On no-advance, scans ``[role='alert']``, ``.css-error``,
        ``[aria-invalid='true']`` for visible validation error text
        and returns it as the reason.
        """
        # Scroll to bottom before clicking — Indeed's PDF preview
        # pages and some questions pages push Continue below the
        # viewport, and safe_click waits for state="visible" which
        # requires the element to be in the visible viewport region.
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        clicked = await self.safe_click(
            page, FORM_CONTINUE_SELECTORS, timeout=500,
        )
        if not clicked:
            clicked = await self._click_nav_button_by_text(
                page,
                (
                    "continue",
                    "next",
                    "save and continue",
                    "use this resume",
                    "use this",
                    "looks good",
                    "review and submit",
                    "review your application",
                    "apply anyway",
                ),
            )
        if not clicked:
            # Dump all visible buttons so the log shows what's
            # actually clickable on pages where our selectors miss.
            try:
                btns = await page.evaluate("""() => {
                    return [...document.querySelectorAll('button, a[role=button], [role=button]')]
                        .filter(el => el.offsetParent !== null)
                        .slice(0, 10)
                        .map(el => ({
                            tag: el.tagName,
                            text: el.innerText?.trim().substring(0, 50),
                            class: el.className?.substring(0, 60),
                            testid: el.getAttribute('data-testid') || '',
                        }));
                }""")
                import json as _json
                logger.info(
                    "Indeed: no continue button — visible buttons: %s",
                    _json.dumps(btns, indent=2)[:1000],
                )
            except Exception:
                pass
            return False, "no continue button clickable"

        # Wait for the URL to differ. If that fires, we advanced.
        try:
            await page.wait_for_url(
                lambda u: u != start_url, timeout=timeout,
            )
            return True, ""
        except Exception:
            pass

        # URL didn't change despite a successful click — dump the
        # visible buttons so the log shows what we actually hit.
        # Without this we can't tell whether Continue was clicked but
        # silently rejected (validation), or whether we hit the wrong
        # element entirely (label vs underlying button).
        try:
            btns = await page.evaluate("""() => {
                return [...document.querySelectorAll('button, a[role=button], [role=button]')]
                    .filter(el => el.offsetParent !== null)
                    .slice(0, 8)
                    .map(el => ({
                        text: el.innerText?.trim().substring(0, 50),
                        disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                        testid: el.getAttribute('data-testid') || '',
                    }));
            }""")
            import json as _json
            logger.info(
                "Indeed: clicked but no URL change — visible buttons: %s",
                _json.dumps(btns)[:800],
            )
        except Exception:
            pass

        # Also dump every visible REQUIRED form field with its current
        # value/checked state. If the form's silently rejecting
        # Continue, the cause is almost always a required field whose
        # state we believe is committed but the form's React state
        # disagrees. Seeing the actual DOM state side-by-side with our
        # logs makes the mismatch obvious.
        try:
            inputs = await page.evaluate("""() => {
                const els = [...document.querySelectorAll(
                    'input:not([type=hidden]):not([type=button]):not([type=submit]),'
                    + 'textarea, select'
                )].filter(el => el.offsetParent !== null);
                return els.slice(0, 25).map(el => {
                    const required = el.required ||
                        el.getAttribute('aria-required') === 'true';
                    const summary = {
                        tag: el.tagName,
                        type: el.type || '',
                        name: (el.name || '').substring(0, 40),
                        required,
                    };
                    if (el.type === 'radio' || el.type === 'checkbox') {
                        summary.checked = !!el.checked;
                    } else {
                        const v = el.value || '';
                        summary.empty = v.length === 0;
                        summary.len = v.length;
                    }
                    return summary;
                });
            }""")
            import json as _json
            unfilled = [
                i for i in inputs
                if i.get("required") and (
                    (i.get("type") in ("radio", "checkbox") and not i.get("checked"))
                    or (i.get("type") not in ("radio", "checkbox") and i.get("empty"))
                )
            ]
            # Also check for radio GROUPS where no option is checked.
            # Indeed marks the group as answered when any one radio in
            # the group is :checked, regardless of `required`. So if a
            # group has zero checked radios, that's effectively unanswered.
            groups: dict[str, dict] = {}
            for i in inputs:
                if i.get("type") != "radio":
                    continue
                name = i.get("name", "")
                if not name:
                    continue
                g = groups.setdefault(name, {"any_checked": False})
                if i.get("checked"):
                    g["any_checked"] = True
            unanswered_radio_groups = [
                name for name, g in groups.items() if not g["any_checked"]
            ]
            if unfilled:
                logger.info(
                    "Indeed: STUCK — %d required field(s) unfilled: %s",
                    len(unfilled), _json.dumps(unfilled),
                )
            elif unanswered_radio_groups:
                logger.info(
                    "Indeed: STUCK — %d radio group(s) with no checked option: %s",
                    len(unanswered_radio_groups),
                    _json.dumps(unanswered_radio_groups),
                )
            else:
                logger.info(
                    "Indeed: STUCK with all required fields filled — "
                    "full form state: %s",
                    _json.dumps(inputs),
                )
        except Exception:
            pass

        # URL didn't change — scan for validation errors we can
        # actually read back to the user / log.
        #
        # Some Indeed pages stash informational notices inside
        # role='alert' elements (e.g., "We created a PDF of your
        # resume... review it before you apply"). Those are NOT
        # real errors — the page is fine, we just haven't clicked
        # the right button yet. Filter them out so the log shows
        # the actual problem ("no continue button matched") instead
        # of misleading the user into thinking validation failed.
        notice_phrases = (
            "we created a pdf",
            "review it before",
            "share with employers",
        )
        error_selectors = [
            "[aria-invalid='true']",
            ".css-error",
            "[class*='error']:not([class*='container'])",
            "[role='alert']",
            "[aria-live='assertive']",
        ]
        for sel in error_selectors:
            try:
                els = await page.query_selector_all(sel)
            except Exception:
                continue
            for el in els:
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text() or "").strip()
                    if not text or len(text) >= 300:
                        continue
                    lowered = text.lower()
                    if any(p in lowered for p in notice_phrases):
                        continue  # informational, not an error
                    return False, f"validation: {text}"
                except Exception:
                    continue
        return False, "no URL change, no visible error"

    async def _walk_application_form(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through Indeed's application form steps.

        Indeed forms are generally simpler than LinkedIn's -- often
        a single page with resume upload and a few screener questions.
        Some jobs have multi-step forms with continue buttons.
        """
        # Module-aware walker. Each iteration:
        # 1. capture the start-of-step URL (before anything can change it)
        # 2. wait for the form shell to render
        # 3. dispatch by URL to the right handler
        # 4. click continue (or submit if this is a review page) and
        #    wait for the URL to actually change
        # 5. log validation errors on no-advance
        last_module: str = ""
        for step in range(MAX_FORM_STEPS):
            await self.check_and_abort_on_captcha(page)

            try:
                step_start_url = page.url
            except Exception:
                step_start_url = ""

            await self._wait_for_form_ready(page)

            module, fields_on_step = await self._dispatch_module(
                page, job, resume_path, step_start_url,
            )
            logger.info(
                "Indeed: step %d module=%s fields=%d url=%s",
                step + 1, module, len(fields_on_step),
                step_start_url.rsplit("/", 1)[-1],
            )

            await simulate_organic_behavior(page)

            if await self._check_success(page):
                logger.info(
                    "Indeed: Application success detected for %s", job.job_id,
                )
                return self._build_result(success=True, dry_run=False)

            # Review module → submit.
            if module == "review" or await self._is_submit_step(page):
                if dry_run:
                    logger.info(
                        "Indeed: DRY RUN -- would submit application for %s",
                        job.job_id,
                    )
                    return self._build_result(success=True, dry_run=True)
                clicked = await self.safe_click(
                    page, FORM_SUBMIT_SELECTORS, timeout=500,
                )
                if not clicked:
                    clicked = await self._click_nav_button_by_text(
                        page, ("submit application", "submit your application", "submit"),
                    )
                if not clicked:
                    return self._build_result(
                        success=False,
                        failure_reason="submit button not found on review page",
                    )
                await random_delay(2.0, 4.0)
                if await self._check_success(page):
                    logger.info(
                        "Indeed: Submitted application for %s", job.job_id,
                    )
                    return self._build_result(success=True, dry_run=False)
                logger.warning(
                    "Indeed: Submit clicked for %s but no success "
                    "indicator appeared — treating as failed",
                    job.job_id,
                )
                return self._build_result(
                    success=False,
                    failure_reason="submit clicked but no success confirmation",
                )

            # Regular step — click Continue and wait for the URL to
            # actually change. If it doesn't, surface any visible
            # validation error so we know WHY.
            advanced, reason = await self._click_continue_and_wait(
                page, step_start_url,
            )
            if advanced:
                last_module = module
                continue

            # No advance — single-page forms sometimes have only a
            # Submit button, no Continue. Try that before bailing.
            clicked = await self.safe_click(
                page, FORM_SUBMIT_SELECTORS, timeout=500,
            )
            if clicked:
                if dry_run:
                    logger.info(
                        "Indeed: DRY RUN -- would submit (single-page) for %s",
                        job.job_id,
                    )
                    return self._build_result(success=True, dry_run=True)
                await random_delay(2.0, 4.0)
                if await self._check_success(page):
                    return self._build_result(success=True, dry_run=False)
                return self._build_result(
                    success=False,
                    failure_reason="single-page submit, no success confirmation",
                )

            logger.warning(
                "Indeed: form not advancing at %s (module=%s) — %s",
                step_start_url, module, reason,
            )
            return self._build_result(
                success=False,
                failure_reason=(
                    f"form stuck on {step_start_url.rsplit('/', 1)[-1]} "
                    f"({module}): {reason}"
                ),
            )

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

    async def _select_uploaded_resume(self, page: Page, resume_path: str) -> bool:
        """Pick the uploaded-resume option on resume-selection-module.

        Indeed's new smartapply shows a radio picker here with at
        least two options: the file you just uploaded AND "Build an
        Indeed Resume" (create from scratch). Our generic form_filler
        sees only the latter as a labelled radio and picks it, which
        is the wrong choice — the form then sits stuck because the
        real selection state is empty.

        This method searches for the uploaded-file option specifically
        and clicks it. Heuristics, in order of preference:

        1. A radio/card whose label contains the resume filename stem
        2. A radio/card whose label contains "upload" or "use this"
        3. The first radio/card that is NOT the "Build an Indeed Resume"
           decoy

        Returns True if something was clicked, False otherwise.
        """
        import os
        stem = os.path.splitext(os.path.basename(resume_path))[0].lower()

        try:
            # Indeed renders resume choices as label-wrapped radios OR
            # clickable cards with role=radio/button. Cast a wide net.
            candidates = await page.query_selector_all(
                "label, [role='radio'], [role='button'], "
                "div[data-testid*='resume'], li[data-testid*='resume']"
            )
        except Exception:
            candidates = []

        best = None
        fallback = None
        for el in candidates:
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text() or "").strip().lower()
                if not text:
                    continue
                if "build an indeed resume" in text or "create" in text:
                    continue  # skip the decoy
                if stem and stem in text:
                    best = el
                    break
                if "upload" in text or "use this" in text or ".docx" in text or ".pdf" in text:
                    if best is None:
                        best = el
                if fallback is None:
                    fallback = el
            except Exception:
                continue

        target = best or fallback
        if target is None:
            return False
        try:
            await target.click()
            logger.info(
                "Indeed: Selected uploaded-resume option (%s)",
                "match" if best else "fallback",
            )
            await random_delay(0.5, 1.5)
        except Exception:
            return False

        # Verify the click actually committed a radio selection. The
        # match path's label-click sometimes lands on the wrapper div
        # without firing React's onChange — visually highlighted, but
        # form state empty, so Continue won't advance. If no radio is
        # :checked after the click, force a JS-dispatched change event
        # on the first non-decoy radio so the form's internal state
        # actually updates.
        try:
            checked = await page.evaluate(
                "() => !!document.querySelector("
                "'input[type=radio]:checked, [role=radio][aria-checked=true]')"
            )
        except Exception:
            checked = True  # benefit of the doubt
        if not checked:
            logger.info(
                "Indeed: resume-selection radio not committed after click — "
                "forcing JS change event"
            )
            try:
                await page.evaluate("""() => {
                    const radios = [...document.querySelectorAll('input[type=radio]')]
                        .filter(r => r.offsetParent !== null);
                    for (const r of radios) {
                        const lbl = (r.closest('label')?.innerText || '').toLowerCase();
                        if (lbl.includes('build an indeed resume') || lbl.includes('create')) continue;
                        r.checked = true;
                        r.dispatchEvent(new Event('input', {bubbles: true}));
                        r.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }""")
                await random_delay(0.4, 0.8)
            except Exception:
                pass
        return True

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
