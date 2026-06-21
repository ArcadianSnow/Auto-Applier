"""Browser session for v3 (ported from v2 ``browser/session.py``, spec §8c).

Async Playwright (spec §3) with maximal stealth: patchright preferred (patches CDP leaks),
real Chrome via ``channel`` (matching JA4), persistent shared profile (logins survive
restarts), headed always. The JA4 hygiene notes below are load-bearing — re-audit if you
change launch flags or override the UA.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_chrome_channel() -> str | None:
    """Return ``"chrome"`` if a real Chrome binary is installed, else ``None``.

    Filesystem-only and synchronous (never launches a browser), so it's safe to
    call from preflight (``doctor.check_browser``) as well as from the session.
    """
    if sys.platform == "win32":
        for p in (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
        ):
            if p.exists():
                return "chrome"
    elif sys.platform == "darwin":
        if Path("/Applications/Google Chrome.app").exists():
            return "chrome"
    else:
        for name in ("google-chrome", "google-chrome-stable"):
            if shutil.which(name):
                return "chrome"
    return None


class BrowserSession:
    """A persistent, stealthy Chrome context. One shared profile across all sites."""

    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir)
        self._playwright = None
        self._context = None
        self._using_patchright = False

    async def start(self) -> None:
        try:
            from patchright.async_api import async_playwright
            self._using_patchright = True
        except ImportError:
            from playwright.async_api import async_playwright
            self._using_patchright = False
        logger.info("browser backend: %s", "patchright" if self._using_patchright else "playwright")

        self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        channel = self._detect_chrome_channel()

        # JA4 hygiene (see v2 session.py): real Chrome channel → JA4 matches the real
        # binary; no UA override; strip --enable-automation/--no-sandbox; do NOT set
        # --disable-blink-features=AutomationControlled (itself a signal); natural viewport.
        launch_args: dict = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "no_viewport": True,
            "chromium_sandbox": True,
            "ignore_default_args": ["--enable-automation", "--no-sandbox"],
            "args": ["--no-first-run", "--no-default-browser-check"],
        }
        if channel:
            launch_args["channel"] = channel

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(**launch_args)
        except Exception as exc:
            s = str(exc).lower()
            if channel and any(k in s for k in ("existing browser session", "target", "closed")):
                logger.warning("real Chrome busy — falling back to bundled Chromium profile")
                launch_args.pop("channel", None)
                fb = self.profile_dir.parent / (self.profile_dir.name + "_chromium")
                fb.mkdir(parents=True, exist_ok=True)
                launch_args["user_data_dir"] = str(fb)
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_args)
            else:
                raise

        if not self._using_patchright:
            await self._apply_stealth(self._context.pages)

    async def new_page(self):
        page = await self._context.new_page()
        if not self._using_patchright:
            await self._apply_stealth([page])
        return page

    async def stop(self) -> None:
        if self._context:
            try:
                await asyncio.wait_for(self._context.close(), timeout=10.0)
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.warning("context.close() issue: %s", exc)
            self._context = None
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=10.0)
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.warning("playwright.stop() issue: %s", exc)
            self._playwright = None

    @property
    def context(self):
        if not self._context:
            raise RuntimeError("BrowserSession not started")
        return self._context

    def _detect_chrome_channel(self) -> str | None:
        return detect_chrome_channel()

    async def _apply_stealth(self, pages: list) -> None:
        try:
            from playwright_stealth import stealth_async
            for page in pages:
                await stealth_async(page)
        except ImportError:
            pass
