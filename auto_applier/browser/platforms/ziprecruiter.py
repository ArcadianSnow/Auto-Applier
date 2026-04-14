"""ZipRecruiter platform adapter for job search and 1-Click Apply automation.

This adapter handles:
- Manual login detection and prompting
- Job search with Quick Apply filter
- Job card parsing from search results
- 1-Click Apply and short-form application walking
- Form field filling via FormFiller
- CAPTCHA detection and hard stop
- "Apply on company site" link detection (skip with failure_reason)

IMPORTANT: ZipRecruiter changes its DOM frequently. Every selector here
has multiple fallbacks. When selectors break, add new ones to the lists --
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
    "[data-testid='user-menu']",
    ".user-menu",
    "a[href*='/profile']",
    "a[href*='/candidate/dashboard']",
    ".navbar-user",
    "[data-testid='header-profile']",
    "img.profile-photo",
    ".header-account-menu",
    "[aria-label='Account menu']",
]

# Job card selectors on the search results page. Newest ZR layouts
# at the top, historical ones kept as fallbacks. We cast a wide net
# because ZR changes its class names every few months — see
# _parse_job_cards for the anchor-based fallback that kicks in when
# none of these match.
JOB_CARD_SELECTORS = [
    # 2024+ layouts
    "article[data-testid='job-card']",
    "div[data-testid='job-card']",
    "article[data-testid*='job']",
    "article.jobList-item",
    "li[class*='JobList'] article",
    # Broad semantic fallbacks — ZR uses <article> for each card
    "article:has(h2)",
    "article:has(a[href*='/jobs/'])",
    # Historical
    ".job_content",
    ".job-listing",
    "article[data-job-id]",
    "[data-testid='job-listing']",
    ".job_result",
    ".jobList-item",
    "article.job-listing",
    ".job-card",
]

# Job title within a card
JOB_TITLE_SELECTORS = [
    # 2024+
    "h2[data-testid='job-title'] a",
    "a[data-testid='job-title']",
    "h2 a[class*='job_title']",
    # Historical
    ".job_title a",
    "h2.job_title a",
    "[data-testid='job-title'] a",
    "a.job_link",
    ".jobList-title a",
    "h2 a.job_title_link",
    ".job-title a",
]

# Company name within a card
JOB_COMPANY_SELECTORS = [
    "a[data-testid='job-card-company']",
    "[data-testid='job-card-company']",
    "[data-testid='company-name']",
    ".job_company",
    "a.company-name",
    ".jobList-company",
    ".job-company-name",
    "span.company_name",
    ".t_org_link",
]

# Location within a card
JOB_LOCATION_SELECTORS = [
    ".job_location",
    "[data-testid='job-location']",
    ".jobList-location",
    ".job-location",
    "span.location",
]

# Apply button on job detail / card
APPLY_BUTTON_SELECTORS = [
    "button[data-testid='apply-button']",
    "button.apply-button",
    ".job_apply button",
    "a.apply-button",
    "button[data-testid='quick-apply']",
    "[data-testid='1-click-apply']",
    "button.quick-apply-button",
    "button[aria-label*='Apply']",
    "button[aria-label*='apply']",
    ".apply_now button",
    "a.quick_apply_button",
]

# Job description on the detail page
JOB_DESCRIPTION_SELECTORS = [
    ".job_description",
    ".jobDescriptionSection",
    "[data-testid='job-description']",
    "#job-description",
    ".job-description",
    ".jobDescription",
    "div.job_details",
]

# Form elements in the application
FORM_FIELD_SELECTORS = [
    ".form-group",
    ".application-form-field",
    "[data-testid*='form-field']",
    "fieldset",
    ".form-field",
]

# Form navigation / submit buttons
FORM_CONTINUE_SELECTORS = [
    "button[data-testid='continue-button']",
    "button.continue-button",
    "button[type='submit']",
    "button.btn-primary",
    "button.next-button",
]

FORM_SUBMIT_SELECTORS = [
    "button[data-testid='submit-application']",
    "button[data-testid='submit-button']",
    "button.submit-application",
    "button[aria-label*='Submit']",
    "button[type='submit']",
    "button.btn-primary",
]

# Application success indicators
SUCCESS_SELECTORS = [
    "[data-testid='application-success']",
    ".application-success",
    ".apply-success",
    "[data-testid='success-message']",
    ".success-message",
    ".application-confirmation",
    "[class*='success']",
    ".congratulations",
]

# "Apply on company site" indicators (external apply)
EXTERNAL_APPLY_SELECTORS = [
    "a[data-testid='apply-on-company-site']",
    "a[href*='apply'][target='_blank']",
    "button[data-testid='external-apply']",
    ".apply-on-company-site",
    "a.external-apply",
]

# Resume upload input
RESUME_UPLOAD_SELECTORS = [
    "input[type='file'][name*='resume']",
    "input[type='file'][name*='Resume']",
    "input[type='file'][accept*='.pdf']",
    "input[type='file'][id*='resume']",
    "input[type='file']",
]

# Max pagination and result limits
MAX_SEARCH_PAGES = 3
MAX_JOBS_PER_SEARCH = 25
MAX_FORM_STEPS = 8  # Safety limit for multi-step forms


class ZipRecruiterPlatform(JobPlatform):
    """ZipRecruiter job search and 1-Click / Quick Apply adapter.

    Requires the user to be logged in manually -- this adapter will
    never automate credential entry. It navigates to ZipRecruiter,
    checks for login indicators, and waits for the user if needed.
    """

    source_id = "ziprecruiter"
    display_name = "ZipRecruiter"

    dead_listing_selectors = [
        ".job_unavailable",
        "[data-testid='job-expired']",
        ".expired-job-notice",
    ]
    dead_listing_phrases = [
        "this job is no longer available",
        "this job has been filled",
        "this posting is no longer accepting",
    ]

    captcha_url_patterns = [
        "/captcha",
        "/recaptcha",
        "/challenge",
        "/verify",
    ]

    async def check_is_external(self, job: Job) -> bool:
        """On ZipRecruiter, external jobs redirect to the company's
        ATS and don't show a Quick Apply button. Detect by absence
        of APPLY_BUTTON_SELECTORS on the already-loaded job page.
        """
        try:
            page = await self.get_page()
            for sel in APPLY_BUTTON_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible():
                            return False
                    except Exception:
                        return False
            return True
        except Exception as e:
            logger.debug("ZipRecruiter check_is_external raised: %s", e)
            return False

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> bool:
        """Navigate to ZipRecruiter and verify the user is logged in.

        If not logged in, navigates to the login page and waits up to
        5 minutes for the user to log in manually.
        """
        page = await self.get_page()

        # Navigate to ZipRecruiter home to check login state
        await page.goto(
            "https://www.ziprecruiter.com/", wait_until="domcontentloaded"
        )
        await random_delay(2.0, 4.0)

        # Check if already logged in
        logged_in = await self.safe_query(page, LOGGED_IN_SELECTORS, timeout=5000)
        if logged_in:
            logger.info("ZipRecruiter: Already logged in")
            return True

        # Not logged in -- navigate to login page
        logger.info(
            "ZipRecruiter: Not logged in. Navigating to login page for manual login..."
        )
        await page.goto(
            "https://www.ziprecruiter.com/authn/login",
            wait_until="domcontentloaded",
        )

        # Wait for user to log in manually (5 minute timeout).
        # Pass ALL logged-in selectors — any match confirms login.
        # URL pattern "ziprecruiter.com" is false-positive on login page.
        return await self.wait_for_manual_login(
            page,
            check_selector=LOGGED_IN_SELECTORS,
            timeout=300,
        )

    # ------------------------------------------------------------------
    # Job Search
    # ------------------------------------------------------------------

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search ZipRecruiter Jobs and return job cards from results.

        Uses a minimal search URL — the older days=14 + Quick Apply
        filter combo was over-narrow and produced zero cards on most
        real queries. Scoring and _is_external_apply downstream still
        filter non-applicable jobs at apply time.
        """
        page = await self.get_page()
        await self.check_and_abort_on_captcha(page)

        # Warm-up via homepage before hitting the search URL.
        try:
            if "ziprecruiter.com" not in page.url.lower():
                logger.info("ZipRecruiter: warm-up via homepage before search")
                await page.goto(
                    "https://www.ziprecruiter.com/",
                    wait_until="domcontentloaded",
                )
                await reading_pause(page)
                await simulate_organic_behavior(page)
        except Exception as exc:
            logger.debug("ZipRecruiter warm-up skipped: %s", exc)

        jobs: list[Job] = []
        encoded_kw = quote_plus(keyword)
        encoded_loc = quote_plus(location)

        for page_num in range(MAX_SEARCH_PAGES):
            # ZipRecruiter uses 1-based page numbering
            search_url = (
                f"https://www.ziprecruiter.com/jobs-search"
                f"?search={encoded_kw}"
                f"&location={encoded_loc}"
                f"&page={page_num + 1}"
            )

            logger.info(
                "ZipRecruiter: Searching page %d -- %s %s",
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
                logger.info(
                    "ZipRecruiter: No more job cards found, stopping pagination"
                )
                break

            # Organic delay between pages
            await simulate_organic_behavior(page)
            await random_delay(3.0, 6.0)

        logger.info(
            "ZipRecruiter: Found %d jobs for '%s' in '%s'",
            len(jobs),
            keyword,
            location,
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
            # CSS selectors missed. Fall back to the anchor-based
            # finder before giving up — ZR can rebuild its DOM
            # without our selectors catching up, but job cards always
            # link to /jobs/<id> so we can locate them structurally.
            logger.info(
                "ZipRecruiter: no CSS selectors matched, trying "
                "anchor-based fallback for /jobs/ links"
            )
            anchor_hits = await self.find_jobs_by_anchors(
                page, href_pattern="/jobs/",
            )
            if anchor_hits:
                logger.info(
                    "ZipRecruiter: anchor fallback recovered %d jobs",
                    len(anchor_hits),
                )
                for title, url in anchor_hits:
                    jobs.append(Job(
                        job_id=f"zr-{abs(hash(url)) % 10**10}",
                        title=title,
                        company="",  # Filled in by get_job_description later
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
                "ZipRecruiter: 0 job cards found and anchor fallback "
                "also came up empty.\n"
                "  url=%s\n"
                "  title=%s\n"
                "  page snippet: %s",
                current_url, title, snippet,
            )
            body_lower = body.lower()
            if "no results" in body_lower or "no jobs" in body_lower:
                logger.warning(
                    "ZipRecruiter shows zero results for this search — "
                    "try broader keywords or location."
                )
            elif "verify" in body_lower or "unusual" in body_lower:
                logger.warning(
                    "ZipRecruiter page contains 'verify' / 'unusual' text "
                    "— may need manual captcha solve in the browser window."
                )
            return jobs

        # One-time diagnostic: dump the first card's full structure so
        # we can see all data attributes, anchors, and clickable
        # elements. Remove once ZR selectors are stable.
        try:
            diag = await cards[0].evaluate("""el => {
                const attrs = [...el.attributes].map(a => a.name + '=' + a.value).join(', ');
                const anchors = [...el.querySelectorAll('a')].map(a =>
                    '{text: ' + JSON.stringify(a.innerText.trim().substring(0,50)) +
                    ', href: ' + JSON.stringify((a.getAttribute('href')||'').substring(0,100)) +
                    ', data: ' + JSON.stringify(Object.keys(a.dataset).join(',')) + '}'
                ).join('\\n  ');
                const buttons = [...el.querySelectorAll('button, [role=button]')].map(b =>
                    '{text: ' + JSON.stringify(b.innerText.trim().substring(0,50)) +
                    ', data: ' + JSON.stringify(Object.keys(b.dataset).join(',')) + '}'
                ).join('\\n  ');
                return 'CARD attrs: ' + attrs +
                       '\\nANCHORS:\\n  ' + (anchors || 'none') +
                       '\\nBUTTONS:\\n  ' + (buttons || 'none') +
                       '\\nHTML (500 chars): ' + el.outerHTML.substring(0, 500);
            }""")
            logger.info("ZipRecruiter: CARD DIAGNOSTIC:\\n%s", diag)
        except Exception as e:
            logger.debug("ZipRecruiter: card diagnostic failed: %s", e)

        for card in cards:
            try:
                job = await self._parse_single_card(card, page, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug("Failed to parse a job card: %s", exc)
                continue

        # Cards detected but parser returned nothing — ZR ships new
        # class names faster than the per-card parser can track.
        # Dump the first card's HTML so the next log review shows
        # exactly what ZR changed, instead of guessing at selectors.
        if not jobs and cards:
            try:
                sample = await cards[0].evaluate(
                    "el => el.outerHTML.substring(0, 1500)"
                )
                logger.debug(
                    "ZipRecruiter: sample unparsed card HTML:\n%s", sample,
                )
            except Exception:
                pass
            logger.info(
                "ZipRecruiter: %d cards detected but none parsed, "
                "falling back to anchor-based finder", len(cards),
            )
            # Try multiple href patterns. ZR has historically used
            # /jobs/<slug>, /job/<slug>, /ec/<tracking-id>, /k/
            # depending on layout. Loop until something sticks.
            anchor_hits: list[tuple[str, str]] = []
            for pattern in ("/jobs/", "/job/", "/ec/", "/k/", "/apply/"):
                anchor_hits = await self.find_jobs_by_anchors(
                    page, href_pattern=pattern,
                )
                if anchor_hits:
                    logger.info(
                        "ZipRecruiter: anchor pattern %r recovered %d jobs",
                        pattern, len(anchor_hits),
                    )
                    break
            for title, url in anchor_hits:
                jobs.append(Job(
                    job_id=f"zr-{abs(hash(url)) % 10**10}",
                    title=title,
                    company="",
                    url=url,
                    search_keyword=keyword,
                    source=self.source_id,
                ))

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

        # Broad fallback: if none of the specific title selectors
        # match, pull the title from the first <h2> directly (ZR has
        # historically wrapped titles in <a> inside <h2>, but recent
        # layouts sometimes put the text in <h2> with no inner link).
        if not title:
            try:
                h2_el = await card.query_selector("h2")
                if h2_el:
                    title = (await h2_el.inner_text()).strip()
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

        # Get job URL / ID
        job_url = ""
        job_id = ""

        # Try data-job-id attribute on the card (article element)
        try:
            data_id = await card.get_attribute("data-job-id")
            if data_id:
                job_id = f"zr-{data_id}"
        except Exception:
            pass

        # Try the title link for URL
        for sel in JOB_TITLE_SELECTORS:
            try:
                link_el = await card.query_selector(sel)
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    if href:
                        job_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.ziprecruiter.com{href}"
                        )
                        # Extract job ID from URL if not found via attribute
                        if not job_id:
                            # ZipRecruiter URLs often contain job ID in path
                            match = re.search(r"/jobs?/([a-f0-9-]+)", href)
                            if match:
                                job_id = f"zr-{match.group(1)}"
                        break
            except Exception:
                continue

        # ZR's current layout has ZERO anchors or buttons inside
        # cards — the entire <article> is clickable via JS. The job
        # token lives in the card's id attribute:
        #   id="job-card-F_aJIgQr8c5c8qYgC5i5ow"
        # We store this token and use it to click into the job later.
        if not job_id:
            try:
                card_id = await card.get_attribute("id") or ""
                if card_id.startswith("job-card-"):
                    token = card_id.removeprefix("job-card-")
                    job_id = f"zr-{token}"
            except Exception:
                pass

        # Fallback: try other data attributes
        if not job_id:
            try:
                data_id = await card.get_attribute("data-id")
                if data_id:
                    job_id = f"zr-{data_id}"
            except Exception:
                pass

        if not job_id:
            # Generate a fallback ID from title + company
            job_id = f"zr-{hash(title + company) % 10**8}"

        # Construct a direct URL using the search page + lk= token.
        # ZR's cards have zero <a> links — the only way to view a
        # job is to load the search page with the lk= parameter,
        # which opens the detail side panel for that specific listing.
        if not job_url and job_id.startswith("zr-"):
            token = job_id.removeprefix("zr-")
            from urllib.parse import quote_plus, urlparse, parse_qs
            # Extract the actual location from the current search URL
            # instead of hardcoding "Remote".
            try:
                parsed = urlparse(page.url)
                loc = parse_qs(parsed.query).get("location", ["Remote"])[0]
            except Exception:
                loc = "Remote"
            job_url = (
                f"https://www.ziprecruiter.com/jobs-search"
                f"?search={quote_plus(keyword)}"
                f"&location={quote_plus(loc)}"
                f"&lk={token}"
            )

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
        """Click into the job listing and extract description.

        ZR's 2026 card layout uses pure JS click handlers — cards
        have zero ``<a>`` or ``<button>`` elements. The ``<article>``
        is clickable and the job token lives in
        ``id="job-card-{token}"``. Clicking the card either:

        a) Opens a side-panel / detail drawer on the search page
        b) Navigates to a new page

        Either way, we wait for a description-sized text block to
        appear in the DOM after clicking.
        """
        page = await self.get_page()

        if not job.url:
            logger.warning(
                "ZipRecruiter: No URL for job %s", job.job_id,
            )
            return ""

        # Navigate to the job's search URL with lk= parameter. ZR
        # opens a side-panel / detail view for the specific listing.
        # Wait for the DOM to settle after the SPA renders.
        await page.goto(job.url, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

        await reading_pause(page)
        await self.check_and_abort_on_captcha(page)

        # Extract description — try known selectors first, then
        # scan for the largest visible text block on the page as a
        # fallback. ZR's description container class changes often.
        description = await self.safe_get_text(
            page, JOB_DESCRIPTION_SELECTORS, timeout=5000
        )
        if not description:
            # ZR's side-panel / detail view doesn't use stable CSS
            # classes. Instead of grabbing the largest div (which
            # includes the entire search page), look for the
            # description content specifically:
            #
            # 1. First try: element whose text starts with typical JD
            #    headings (About, Overview, Responsibilities, etc.)
            # 2. Second try: the detail panel that appeared AFTER the
            #    search results — usually a sibling of the job list
            # 3. Last resort: largest block that does NOT contain the
            #    search header ("X jobs in Y")
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

                    // Strategy 2: side panel — look for elements that
                    // DON'T contain the search results list
                    const candidates = [...allEls].filter(el => {
                        const text = (el.innerText || '').trim();
                        if (text.length < 200 || text.length > 15000) return false;
                        // Skip elements that contain search results chrome
                        if (/\\d+ .*(jobs|results)/i.test(text.substring(0, 100))) return false;
                        // Skip elements that contain many job titles (search list)
                        const h2Count = el.querySelectorAll('h2').length;
                        if (h2Count > 3) return false;
                        return true;
                    }).sort((a, b) => b.innerText.length - a.innerText.length);

                    return candidates.length > 0 ? candidates[0].innerText.substring(0, 8000) : '';
                }""")
                if description:
                    logger.debug(
                        "ZipRecruiter: extracted description via text-block "
                        "fallback (%d chars)", len(description),
                    )
            except Exception:
                pass

        if description:
            logger.debug(
                "ZipRecruiter: Got description for %s (%d chars)",
                job.job_id,
                len(description),
            )
        else:
            # Dump the visible text around the job detail area so the
            # log shows what ZR actually renders, instead of guessing
            # at selectors. The side panel DOM structure changes
            # frequently — this diagnostic shortens the debug cycle.
            try:
                snippet = await page.evaluate("""() => {
                    // Look for any large text block that looks like a JD
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
                    "ZipRecruiter: description diagnostic for %s: %s",
                    job.job_id, snippet,
                )
            except Exception:
                pass
            logger.warning(
                "ZipRecruiter: Could not extract description for %s",
                job.job_id,
            )

        return description

    # ------------------------------------------------------------------
    # Apply (1-Click + Short Form)
    # ------------------------------------------------------------------

    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """Apply via ZipRecruiter's 1-Click Apply or short form.

        Many ZipRecruiter jobs support true 1-click apply. Others have
        a short form with a few screener questions. This method handles
        both cases. In dry_run mode, fills fields but does not submit.

        Detects and skips "Apply on company site" links.
        """
        page = await self.get_page()

        try:
            # Navigate to the job if needed
            if job.url and job.url not in page.url:
                await page.goto(job.url, wait_until="domcontentloaded")
                await reading_pause(page)

            await self.check_and_abort_on_captcha(page)

            # Check for external apply link (company site redirect)
            if await self._is_external_apply(page):
                logger.info(
                    "ZipRecruiter: Job %s has 'Apply on company site' link, skipping",
                    job.job_id,
                )
                return ApplyResult(
                    success=False,
                    failure_reason="Apply on company site -- external redirect",
                )

            # Click the Apply button
            clicked = await self.safe_click(
                page, APPLY_BUTTON_SELECTORS, timeout=5000
            )
            if not clicked:
                # Dump the apply button region so the log shows what
                # ZR actually renders, helping us update selectors.
                try:
                    snippet = await page.evaluate("""() => {
                        const btns = [...document.querySelectorAll('button, a')]
                            .filter(el => el.textContent.toLowerCase().includes('apply'))
                            .map(el => el.outerHTML.substring(0, 200));
                        return 'apply-ish buttons: ' + JSON.stringify(btns.slice(0, 5));
                    }""")
                    logger.info(
                        "ZipRecruiter: apply button diagnostic: %s", snippet,
                    )
                except Exception:
                    pass
                return ApplyResult(
                    success=False,
                    failure_reason="Apply button not found",
                )

            await random_delay(1.5, 3.0)

            # Login gate detection: ZR's apply click sometimes
            # redirects to a login/signup page instead of the form.
            # Wait for the user to log in manually if this happens.
            try:
                cur = page.url.lower()
            except Exception:
                cur = ""
            if "/login" in cur or "/signup" in cur or "/register" in cur:
                logger.info(
                    "ZipRecruiter: apply redirected to login — waiting "
                    "for manual login (timeout=120s)..."
                )
                try:
                    await page.wait_for_url(
                        lambda u: "/login" not in u
                        and "/signup" not in u
                        and "/register" not in u,
                        timeout=120000,
                    )
                    logger.info(
                        "ZipRecruiter: login completed, continuing apply flow"
                    )
                    await random_delay(1.0, 2.0)
                except Exception:
                    return ApplyResult(
                        success=False,
                        failure_reason="apply login gate timed out — please log in to ZipRecruiter",
                    )

            # Check if we got redirected externally after clicking
            if await self._check_external_redirect(page):
                return ApplyResult(
                    success=False,
                    failure_reason="Apply click redirected to external site",
                )

            await self.check_and_abort_on_captcha(page)

            # Check for immediate success (1-click apply)
            if await self._check_success(page):
                logger.info(
                    "ZipRecruiter: 1-click apply succeeded for %s", job.job_id
                )
                return self._build_result(success=True, dry_run=False)

            # Walk the short application form if present
            return await self._walk_application_form(
                page, job, resume_path, dry_run
            )

        except CaptchaDetectedError as exc:
            logger.error("ZipRecruiter: %s", exc)
            return ApplyResult(
                success=False,
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.error(
                "ZipRecruiter: Apply failed for %s: %s", job.job_id, exc
            )
            return ApplyResult(
                success=False,
                failure_reason=f"Unexpected error: {exc}",
            )

    async def _is_external_apply(self, page: Page) -> bool:
        """Check if the job has an external apply link."""
        for sel in EXTERNAL_APPLY_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    return True
            except Exception:
                continue

        # Check button/link text for external apply indicators
        try:
            links = await page.query_selector_all("a.apply-button, button.apply-button")
            for link in links:
                text = (await link.inner_text()).strip().lower()
                if "company site" in text or "external" in text:
                    return True
        except Exception:
            pass

        return False

    async def _check_external_redirect(self, page: Page) -> bool:
        """Check if clicking apply left ZipRecruiter or opened a new tab."""
        if "ziprecruiter.com" not in page.url:
            return True

        # Check if a new page/tab was opened
        pages = self.context.pages
        if len(pages) > 1:
            for p in pages[1:]:
                try:
                    await p.close()
                except Exception:
                    pass
            return True

        return False

    # Labels that belong to the ZipRecruiter page CHROME (persistent
    # header search bar, footer, etc.) — NOT the application form.
    # Stepping through the form loop and only finding these means
    # the apply form has closed / submitted and we should stop looping.
    _CHROME_LABEL_PATTERNS = (
        "search for job title",
        "search job title",
        "search jobs",
        "search keyword",
        "location",  # ONLY when paired with "search"
        "what",
        "where",
    )

    def _is_chrome_field(self, label: str) -> bool:
        """True if a field label looks like page chrome (header search bar)."""
        lower = label.lower().strip()
        # Exact match to known chrome labels
        chrome_exact = {
            "search for job title or keyword",
            "search job title and location",
            "search job title",
            "search",
            "zip code and/or city, state",  # ZR header location input
        }
        if lower in chrome_exact:
            return True
        # "Search ..." at the start is always chrome
        if lower.startswith("search for") or lower.startswith("search job"):
            return True
        return False

    async def _walk_application_form(
        self, page: Page, job: Job, resume_path: str, dry_run: bool
    ) -> ApplyResult:
        """Walk through ZipRecruiter's short application form.

        ZipRecruiter forms are typically shorter than LinkedIn or
        Indeed -- often just a resume upload and 1-3 screener questions.

        Tracks whether each step found real application-form fields
        vs. only page chrome (header search bar). If we previously
        filled real fields but a later step only sees chrome, the
        apply form is gone — treat as submission complete.
        """
        any_real_fields_filled = False

        for step in range(MAX_FORM_STEPS):
            logger.info(
                "ZipRecruiter: Application form step %d for %s",
                step + 1,
                job.job_id,
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
                        page, field, job_id=job.job_id
                    )
                    await random_delay(0.5, 1.5)
                real_fields_on_step = len(real_fields)
                if real_fields_on_step > 0:
                    any_real_fields_filled = True
                logger.debug(
                    "ZipRecruiter: step %d — %d real fields, %d chrome fields",
                    step + 1, real_fields_on_step,
                    len(fields) - real_fields_on_step,
                )

            # If we've previously filled real fields but this step has
            # none, the apply form is gone. Assume success — ZR's apply
            # flow often closes the modal silently after submit.
            if any_real_fields_filled and real_fields_on_step == 0 and step > 0:
                logger.info(
                    "ZipRecruiter: apply form closed (only chrome fields "
                    "remain) — treating as submission complete for %s",
                    job.job_id,
                )
                return self._build_result(
                    success=True, dry_run=dry_run,
                )

            await simulate_organic_behavior(page)

            # Check for success indicators
            if await self._check_success(page):
                logger.info(
                    "ZipRecruiter: Application success detected for %s",
                    job.job_id,
                )
                return self._build_result(success=True, dry_run=False)

            # Check if this is the submit step
            is_submit = await self._is_submit_step(page)

            if is_submit:
                if dry_run:
                    logger.info(
                        "ZipRecruiter: DRY RUN -- would submit application for %s",
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
                            "ZipRecruiter: Submitted application for %s",
                            job.job_id,
                        )
                        return self._build_result(success=True, dry_run=False)

                    # Assume success if submit clicked without error
                    logger.info(
                        "ZipRecruiter: Submit clicked for %s (no explicit success indicator)",
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
                    # Try submit as fallback (single-page form)
                    clicked = await self.safe_click(
                        page, FORM_SUBMIT_SELECTORS, timeout=3000
                    )
                    if clicked:
                        if dry_run:
                            logger.info(
                                "ZipRecruiter: DRY RUN -- would submit for %s",
                                job.job_id,
                            )
                            return self._build_result(success=True, dry_run=True)
                        await random_delay(2.0, 4.0)
                        logger.info(
                            "ZipRecruiter: Submit clicked (single-page) for %s",
                            job.job_id,
                        )
                        return self._build_result(success=True, dry_run=False)

                    # Check for visible validation errors
                    validation_msg = await self._scan_validation_errors(page)
                    reason = f"No navigation button found at step {step + 1}"
                    if validation_msg:
                        reason += f" (validation: {validation_msg})"
                    logger.warning("ZipRecruiter: %s", reason)
                    return self._build_result(
                        success=False,
                        failure_reason=reason,
                    )
                await random_delay(1.5, 3.0)

        # Exceeded max steps
        logger.warning(
            "ZipRecruiter: Exceeded %d form steps for %s",
            MAX_FORM_STEPS,
            job.job_id,
        )
        return self._build_result(
            success=False,
            failure_reason=f"Exceeded maximum form steps ({MAX_FORM_STEPS})",
        )

    async def _is_submit_step(self, page: Page) -> bool:
        """Check if the current form step has a Submit button."""
        for sel in FORM_SUBMIT_SELECTORS[:3]:
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
                    "1-click apply",
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
                "you've applied",
                "thank you for applying",
                "congratulations",
                "application received",
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
                    logger.info(
                        "ZipRecruiter: Uploaded resume from %s", resume_path
                    )
                    await random_delay(1.0, 2.0)
                    return
            except Exception:
                continue

    async def _scan_validation_errors(self, page: Page) -> str:
        """Scan for visible validation error messages on the current page.

        Returns a short description of the error, or empty string.
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
                "ZipRecruiter: Apply result: success [%d/%d fields, llm=%s]",
                result.fields_filled, result.fields_total, result.used_llm,
            )
        else:
            logger.info(
                "ZipRecruiter: Apply result: failed: %s", result.failure_reason,
            )
        return result
