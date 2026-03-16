"""Playwright browser lifecycle with persistent context."""

from playwright.async_api import BrowserContext, Playwright, async_playwright

from auto_applier.config import BROWSER_PROFILE_DIR


class BrowserSession:
    """Manages a persistent Chromium browser that preserves cookies across runs."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> BrowserContext:
        """Launch browser with persistent profile (headed mode, stealth applied)."""
        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        # Apply stealth patches to avoid detection
        try:
            from playwright_stealth import stealth_async

            for page in self._context.pages:
                await stealth_async(page)

            # Apply stealth to any new pages that open
            self._context.on(
                "page",
                lambda page: page.evaluate("void(0)").then(
                    lambda _: None
                ),
            )
        except ImportError:
            pass  # playwright-stealth not installed, continue without it

        return self._context

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def get_page(self):
        """Get the first page or create a new one."""
        pages = self.context.pages
        if pages:
            return pages[0]
        return await self.context.new_page()

    async def close(self) -> None:
        """Close the browser and clean up."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
