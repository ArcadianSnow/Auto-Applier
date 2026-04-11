"""Base class for all job platform adapters.

Each job site (LinkedIn, Indeed, Dice, etc.) implements a subclass of
:class:`JobPlatform` that provides site-specific selectors and workflows
while sharing anti-detection helpers and common form utilities.

Key design principles:
- Manual login ONLY -- never automate credential entry
- Hard stop on CAPTCHA detection
- Multiple fallback selectors for everything (DOM changes frequently)
- Anti-detect helpers between every action
"""
import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod

from playwright.async_api import Page

from auto_applier.browser.anti_detect import (
    human_move,
    random_delay,
    reading_pause,
    simulate_organic_behavior,
)
from auto_applier.storage.models import ApplyResult, Job, SkillGap

logger = logging.getLogger(__name__)


class CaptchaDetectedError(Exception):
    """Raised when a CAPTCHA or bot-detection challenge is detected."""


class JobPlatform(ABC):
    """Abstract base for job site adapters.

    Subclasses must define ``source_id`` and ``display_name`` as class
    attributes, and implement the four abstract methods below.
    """

    source_id: str  # "linkedin", "indeed", etc.
    display_name: str  # "LinkedIn", "Indeed", etc.

    # Liveness detection — subclasses override to tighten detection.
    # See browser/liveness.py for how these are used.
    dead_listing_selectors: list[str] = []
    dead_listing_phrases: list[str] = []

    # URL patterns (substring-matched, case-insensitive) that signal
    # the user has been redirected to a CAPTCHA or account-verification
    # flow. Subclasses add platform-specific paths.
    captcha_url_patterns: list[str] = [
        "/captcha",
        "/recaptcha",
    ]

    def __init__(self, context, config: dict, form_filler=None) -> None:
        self.context = context  # Browser context from BrowserSession
        self.config = config  # Platform-specific config dict
        self.form_filler = form_filler
        self._page = None

    async def get_page(self) -> Page:
        """Get or create the page for this platform."""
        if self._page is None or self._page.is_closed():
            pages = self.context.pages
            self._page = pages[0] if pages else await self.context.new_page()
        return self._page

    # ------------------------------------------------------------------
    # Required Methods (subclasses must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    async def ensure_logged_in(self) -> bool:
        """Check if logged in. If not, prompt user for manual login.

        Returns True if successfully logged in. Must NEVER automate
        credential entry -- only navigate to the login page and wait.
        """
        ...

    @abstractmethod
    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search for jobs matching the keyword and location.

        Returns a list of Job objects parsed from the search results.
        """
        ...

    @abstractmethod
    async def get_job_description(self, job: Job) -> str:
        """Fetch the full job description text for a given job."""
        ...

    @abstractmethod
    async def apply_to_job(
        self, job: Job, resume_path: str, dry_run: bool = False
    ) -> ApplyResult:
        """Apply to a job. Returns rich result with gaps and metadata.

        If dry_run is True, walk through the form but do not submit.
        """
        ...

    # ------------------------------------------------------------------
    # Shared Helpers -- Selector Utilities
    # ------------------------------------------------------------------

    async def safe_query(
        self, page: Page, selectors: list[str], timeout: int = 3000
    ):
        """Try multiple selectors, return first visible match or None.

        This is essential because job sites change their DOM frequently.
        Always provide multiple fallback selectors.
        """
        for sel in selectors:
            try:
                el = await page.wait_for_selector(
                    sel, timeout=timeout, state="visible"
                )
                if el:
                    return el
            except Exception:
                continue
        return None

    async def safe_click(
        self, page: Page, selectors: list[str], timeout: int = 3000
    ) -> bool:
        """Try clicking first matching selector with human-like movement.

        Returns True if an element was found and clicked.
        """
        el = await self.safe_query(page, selectors, timeout)
        if el:
            box = await el.bounding_box()
            if box:
                tx = box["x"] + random.uniform(
                    box["width"] * 0.2, box["width"] * 0.8
                )
                ty = box["y"] + random.uniform(
                    box["height"] * 0.2, box["height"] * 0.8
                )
                await human_move(page, tx, ty)
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await page.mouse.click(tx, ty)
                return True
            # Fallback if no bounding box
            await el.click()
            return True
        return False

    async def safe_get_text(
        self, page: Page, selectors: list[str], timeout: int = 3000
    ) -> str:
        """Get inner text from first matching selector, or empty string."""
        el = await self.safe_query(page, selectors, timeout)
        if el:
            try:
                return (await el.inner_text()).strip()
            except Exception:
                return ""
        return ""

    # ------------------------------------------------------------------
    # Shared Helpers -- Detection
    # ------------------------------------------------------------------

    async def check_liveness(self, job: Job, navigate: bool = True) -> str:
        """Return "live", "dead", or "unknown" for a job listing.

        When ``navigate`` is True (default) the platform loads
        ``job.url`` before inspecting. Pass ``navigate=False`` when
        the caller has already navigated to the job page (e.g. the
        pipeline calls this right after ``get_job_description`` which
        already loaded it) — this avoids a wasted round-trip and
        stays anti-detection friendly.

        Delegates to :func:`browser.liveness.check_liveness_on_page`
        using the subclass's ``dead_listing_selectors`` and
        ``dead_listing_phrases`` class attributes. Platforms with
        unusual flows may override this entirely.

        Sets ``job.liveness`` in place and also returns the string.
        """
        from auto_applier.browser.liveness import (
            Liveness, check_liveness_on_page,
        )

        try:
            page = await self.get_page()
            status: int | None = None
            if navigate:
                try:
                    response = await page.goto(
                        job.url, wait_until="domcontentloaded", timeout=15000,
                    )
                    status = response.status if response else None
                except Exception:
                    pass
            result = await check_liveness_on_page(
                page,
                self.dead_listing_selectors,
                self.dead_listing_phrases,
                response_status=status,
            )
        except Exception as e:
            logger.debug("check_liveness raised, returning UNKNOWN: %s", e)
            result = Liveness.UNKNOWN

        job.liveness = result.value
        return result.value

    async def detect_captcha(self, page: Page) -> bool:
        """Check if a CAPTCHA or bot-detection challenge is present.

        Uses a layered evidence model to avoid false positives on
        legitimate pages that happen to contain ambiguous phrases
        ('security check' appears in LinkedIn's cookie banner, etc.):

        1. **Strong signals** (any ONE triggers a stop):
           - A real CAPTCHA iframe (recaptcha, hcaptcha, arkose, px).
           - URL matches a known challenge path for the current
             platform (e.g. LinkedIn's ``/checkpoint/challenge``).

        2. **Weak signals** (text phrases) only trigger a stop when
           combined with an explicit challenge container element.
           On their own they're evidence of nothing — those words
           appear in privacy footers, cookie banners, and login help
           text all the time.

        Subclasses can extend the URL pattern list via the
        ``captcha_url_patterns`` class attribute.
        """
        # 1a. Strong signal: real CAPTCHA iframes / containers
        strong_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='arkoselabs']",
            "iframe[src*='funcaptcha']",
            "#captcha-challenge",
            "#px-captcha",
            ".g-recaptcha",
            "[data-testid='captcha-challenge']",
        ]
        for sel in strong_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    logger.warning("CAPTCHA detected via selector: %s", sel)
                    return True
            except Exception:
                continue

        # 1b. Strong signal: challenge URL
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        for pat in self.captcha_url_patterns:
            if pat in url:
                logger.warning("CAPTCHA detected via URL pattern: %s (url=%s)", pat, url)
                return True

        # 2. Weak signal: phrase in page text PLUS a challenge container
        # element. Phrases alone are too noisy — they fire on normal
        # LinkedIn / Indeed pages that talk about security in their
        # cookie or privacy text.
        weak_container_selectors = [
            "[class*='challenge']",
            "[class*='captcha']",
            "[id*='challenge']",
            "[id*='captcha']",
        ]
        has_challenge_container = False
        for sel in weak_container_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    has_challenge_container = True
                    break
            except Exception:
                continue
        if not has_challenge_container:
            return False

        try:
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
        except Exception:
            return False
        strong_phrases = [
            "verify you are a human",
            "verify you're a human",
            "prove you are not a robot",
            "complete the security check",
            "unusual activity from your",
        ]
        for phrase in strong_phrases:
            if phrase in body_lower:
                logger.warning(
                    "CAPTCHA detected via phrase+container: '%s'", phrase,
                )
                return True

        return False

    async def check_and_abort_on_captcha(self, page: Page) -> None:
        """Raise CaptchaDetectedError if a CAPTCHA is present.

        Call this between major actions to implement the hard-stop rule.
        """
        if await self.detect_captcha(page):
            raise CaptchaDetectedError(
                f"CAPTCHA detected on {self.display_name}. "
                "Stopping immediately to protect the account."
            )

    # ------------------------------------------------------------------
    # Shared Helpers -- Manual Login
    # ------------------------------------------------------------------

    async def wait_for_manual_login(
        self,
        page: Page,
        check_url_pattern: str = "",
        check_selector: str = "",
        timeout: int = 300,
    ) -> bool:
        """Wait for the user to manually log in via the headed browser.

        Polls every 2 seconds for login indicators (URL pattern or
        DOM selector). Times out after ``timeout`` seconds (default 5 min).
        """
        logger.info(
            "Waiting for manual login on %s (timeout=%ds)...",
            self.display_name,
            timeout,
        )
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if check_url_pattern and check_url_pattern in page.url:
                logger.info("Login detected via URL pattern")
                return True
            if check_selector:
                try:
                    el = await page.query_selector(check_selector)
                    if el:
                        logger.info("Login detected via selector")
                        return True
                except Exception:
                    pass
            await asyncio.sleep(2.0)

        logger.warning("Manual login timed out after %ds", timeout)
        return False
