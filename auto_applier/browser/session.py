"""Browser lifecycle with persistent context and anti-detection."""
import logging
import shutil
import sys
from pathlib import Path

from auto_applier.config import BROWSER_PROFILE_DIR

logger = logging.getLogger(__name__)


class BrowserSession:
    """Manages a persistent browser that looks like a real user's Chrome.

    Key stealth features:
    - Patchright preferred (patches CDP fingerprint leaks), Playwright fallback
    - Real Chrome via channel param (not bundled Chromium)
    - Persistent user profile preserves cookies/sessions across runs
    - Headed mode always -- never headless
    - Stealth patches applied on standard Playwright pages
    """

    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._using_patchright = False

    async def start(self) -> None:
        """Launch browser with maximum stealth settings."""
        # Prefer patchright (patches CDP leaks that detection services check)
        try:
            from patchright.async_api import async_playwright
            self._using_patchright = True
            logger.info("Using patchright (CDP leak patches active)")
        except ImportError:
            from playwright.async_api import async_playwright
            self._using_patchright = False
            logger.info("Patchright unavailable, falling back to standard Playwright")

        self._playwright = await async_playwright().start()

        chrome_channel = self._detect_chrome_channel()
        if chrome_channel:
            logger.info("Detected real Chrome installation, using channel=%s", chrome_channel)
        else:
            logger.warning(
                "Real Chrome not found -- using bundled Chromium. "
                "Install Chrome for a better browser fingerprint."
            )

        launch_args = {
            "user_data_dir": str(BROWSER_PROFILE_DIR),
            "headless": False,
            "no_viewport": True,  # Let browser use natural viewport size
            # Ensure the sandbox is active — without this, Playwright
            # quietly adds --no-sandbox to Chrome's command line, which
            # pops the 'unsupported command-line flag' warning bar AND
            # is a major automation-detection signal on LinkedIn.
            "chromium_sandbox": True,
            # Strip the two Playwright defaults that LinkedIn fingerprints
            # most aggressively: --enable-automation (flips
            # navigator.webdriver to true) and --no-sandbox. Everything
            # else Playwright needs still passes through.
            "ignore_default_args": [
                "--enable-automation",
                "--no-sandbox",
            ],
            # Keep this list SMALL. Every extra flag is either a
            # Chrome warning trigger, a fingerprint signal, or both:
            #
            # - --disable-blink-features=AutomationControlled was the
            #   classic 'hide webdriver' trick, but recent Chrome
            #   versions reject the flag (showing a warning infobar)
            #   AND treat its presence as a detection signal itself.
            #   Dropped.
            # - --disable-infobars is a legacy flag that does nothing
            #   in current Chrome. Dropped.
            #
            # What's left: only the two flags needed to suppress the
            # first-run dialogs that would otherwise trap the user.
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }

        if chrome_channel:
            launch_args["channel"] = chrome_channel

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_args
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if chrome_channel and (
                "existing browser session" in exc_str
                or "target" in exc_str
                or "closed" in exc_str
            ):
                # Chrome is already running (user's personal browser).
                # Retry with bundled Chromium and a separate profile dir
                # so we don't collide with Chrome's profile lock.
                logger.warning(
                    "Chrome is already running — falling back to bundled "
                    "Chromium. Close personal Chrome for a better fingerprint."
                )
                launch_args.pop("channel", None)
                fallback_dir = BROWSER_PROFILE_DIR.parent / "browser_profile_chromium"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                launch_args["user_data_dir"] = str(fallback_dir)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch_args
                )
            else:
                raise
        logger.info("Browser session started (patchright=%s)", self._using_patchright)

        # Apply stealth patches on existing pages if using standard Playwright
        if not self._using_patchright:
            await self._apply_stealth_to_pages(self._context.pages)

    async def stop(self) -> None:
        """Close browser and playwright gracefully."""
        if self._context:
            try:
                await self._context.close()
            except Exception as exc:
                logger.warning("Error closing browser context: %s", exc)
            self._context = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Error stopping playwright: %s", exc)
            self._playwright = None
        logger.info("Browser session stopped")

    @property
    def context(self):
        """Return the browser context, raising if not started."""
        if not self._context:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def get_page(self):
        """Get the first existing page or create a new one."""
        pages = self.context.pages
        if pages:
            return pages[0]
        return await self.new_page()

    async def new_page(self):
        """Create a new browser tab with stealth patches applied."""
        page = await self.context.new_page()
        if not self._using_patchright:
            await self._apply_stealth_to_pages([page])
        return page

    @property
    def is_running(self) -> bool:
        """Whether the browser context is active."""
        return self._context is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_chrome_channel(self) -> str | None:
        """Check if real Chrome is installed and return the channel name."""
        if sys.platform == "win32":
            chrome_paths = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
            ]
            for p in chrome_paths:
                if p.exists():
                    return "chrome"
        elif sys.platform == "darwin":
            if Path("/Applications/Google Chrome.app").exists():
                return "chrome"
        else:  # Linux
            for name in ["google-chrome", "google-chrome-stable"]:
                if shutil.which(name):
                    return "chrome"
        return None

    async def _apply_stealth_to_pages(self, pages: list) -> None:
        """Apply playwright-stealth patches to pages (standard Playwright only)."""
        try:
            from playwright_stealth import stealth_async
            for page in pages:
                await stealth_async(page)
            if pages:
                logger.debug("Applied stealth patches to %d page(s)", len(pages))
        except ImportError:
            logger.debug("playwright-stealth not installed, skipping stealth patches")
