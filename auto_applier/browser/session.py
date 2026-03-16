"""Browser lifecycle with persistent context and anti-detection.

Uses patchright (undetected Playwright fork) when available, falls back
to standard Playwright. Always runs in headed mode with a persistent
user profile to preserve cookies/sessions across runs.
"""

import shutil

from auto_applier.config import BROWSER_PROFILE_DIR


class BrowserSession:
    """Manages a persistent browser that looks like a real user's Chrome."""

    def __init__(self) -> None:
        self._playwright = None
        self._context = None

    async def start(self):
        """Launch browser with maximum stealth settings."""

        # Prefer patchright (patches CDP leaks that Playwright exposes)
        try:
            from patchright.async_api import async_playwright
            self._using_patchright = True
        except ImportError:
            from playwright.async_api import async_playwright
            self._using_patchright = False

        self._playwright = await async_playwright().start()

        # Detect if real Chrome is installed — its fingerprint is much
        # better than the bundled Chromium binary
        chrome_channel = self._detect_chrome_channel()

        launch_args = {
            "user_data_dir": str(BROWSER_PROFILE_DIR),
            "headless": False,
            # Let the browser use its natural viewport — fixed sizes are detectable
            "no_viewport": True,
            "locale": "en-US",
            "color_scheme": "light",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--enable-webgl",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }

        # Use real Chrome if available (its TLS/JA3 fingerprint is genuine)
        if chrome_channel:
            launch_args["channel"] = chrome_channel

        # Do NOT set a custom user_agent — let the real browser provide its
        # own UA string so it matches the TLS fingerprint and other signals

        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_args
        )

        # Apply stealth patches if using standard Playwright (patchright
        # handles this internally)
        if not self._using_patchright:
            try:
                from playwright_stealth import Stealth
                stealth = Stealth()
                await stealth.apply_stealth_async(self._context)
            except ImportError:
                pass

        return self._context

    @staticmethod
    def _detect_chrome_channel() -> str | None:
        """Check if a real Chrome installation exists."""
        import os
        from pathlib import Path

        # Common Chrome locations on Windows
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for path in candidates:
            if path.exists():
                return "chrome"

        # Check if chrome is on PATH (Linux/macOS)
        if shutil.which("google-chrome") or shutil.which("chrome"):
            return "chrome"

        return None

    @property
    def context(self):
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
