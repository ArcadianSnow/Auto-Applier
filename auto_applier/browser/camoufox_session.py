"""Camoufox browser session — alternative anti-detect backend.

Phase 1 scaffold (2026-05-03). Per the Phase 1 research pass:

  - Camoufox is the only OSS tool credibly attacking the
    C++/engine-level fingerprint surface (canvas, WebGL, font,
    screen, JA4) that defeats every Chrome-based stealth approach
    on Cloudflare-fronted sites.
  - Maintainer was inactive Q1 2026 (medical emergency, per their
    own announcements) but resumed mid-April 2026 — PRs #576, #579,
    #586 dated 2026-04-24 through 2026-04-29.
  - Latest cut is v146.0.1-beta.25 from January 2026, still
    explicitly experimental.

We ship this scaffold so the integration is ready when a stable
release lands. It is **not** registered in the platform registry
or wired into the engine — that's deferred until either (a)
Camoufox cuts a stable release OR (b) we have a throwaway test
account to validate it against LinkedIn / Cloudflare-fronted
apply pages.

Usage (when ready)
------------------

    pip install -e ".[camoufox]"
    camoufox fetch  # downloads the patched Firefox (~200 MB)

Then the session API mirrors NodriverSession:

    session = CamoufoxSession()
    await session.start()
    page = await session.new_tab("https://example.com")
    ...
    await session.stop()

Why a separate session module
-----------------------------

Camoufox is a Firefox build, not Chromium. It cannot share the
patchright BrowserContext or anti_detect helpers — Firefox has a
different DevTools Protocol surface and different selector quirks.
Adapter pattern: future ``camoufox_*`` platform adapters use this
session, never reach into the patchright session.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from auto_applier.config import BROWSER_PROFILE_DIR

logger = logging.getLogger(__name__)


# Camoufox uses its own profile-data dir to keep cookies / extensions
# / login state separate from the patchright profile and the nodriver
# profile.
CAMOUFOX_PROFILE_DIR = BROWSER_PROFILE_DIR.parent / "camoufox_profile"


if TYPE_CHECKING:  # pragma: no cover - hint-only imports
    from camoufox.async_api import AsyncCamoufox  # noqa: F401


def _camoufox_install_hint() -> str:
    return (
        'Camoufox is an optional dependency (currently experimental). '
        'Install with: pip install -e ".[camoufox]" && camoufox fetch '
        '(then restart Auto Applier). Note that Camoufox is still in '
        'beta as of 2026-05-03 and may not be stable enough for daily '
        'use.'
    )


class CamoufoxSession:
    """Manage a Camoufox-backed browser session.

    Mirrors :class:`NodriverSession`'s shape so platform adapters
    can switch backends with minimal change. Lifecycle:

      - ``await session.start()``   — launches Camoufox + connects
      - ``page = await session.new_tab(url)``  — opens a tab
      - ``await session.stop()``    — closes Camoufox cleanly

    Idempotent — calling ``start()`` twice is a no-op, calling
    ``stop()`` without ``start()`` is a no-op.

    Camoufox's API uses an ``AsyncCamoufox`` async context manager;
    we expose a non-context-manager wrapper so it fits the existing
    BrowserSession lifecycle pattern.
    """

    def __init__(
        self,
        profile_dir: Path | None = None,
        headless: bool = False,
        humanize: bool = True,
    ) -> None:
        self.profile_dir = profile_dir or CAMOUFOX_PROFILE_DIR
        # Headed by default. Camoufox's "humanize" mode adds organic
        # mouse and timing — but we have our own anti_detect.py with
        # Bezier paths, so we leave the choice to the caller.
        self.headless = headless
        self.humanize = humanize
        self._browser: Any = None
        self._cm: Any = None  # The AsyncCamoufox context manager

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            from camoufox.async_api import AsyncCamoufox  # type: ignore
        except ImportError as exc:
            raise ImportError(_camoufox_install_hint()) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Camoufox: starting browser (profile=%s, headless=%s, humanize=%s)",
            self.profile_dir, self.headless, self.humanize,
        )
        # AsyncCamoufox is an async context manager. We open it
        # manually (via __aenter__) so the BrowserSession can hold
        # the lifecycle across many tabs without nesting under a
        # single ``async with`` block.
        self._cm = AsyncCamoufox(
            headless=self.headless,
            humanize=self.humanize,
            persistent_context=True,
            user_data_dir=str(self.profile_dir),
        )
        self._browser = await self._cm.__aenter__()

    async def new_tab(self, url: str = "about:blank") -> Any:
        """Open a new tab. Returns a Camoufox/Playwright page object.

        Camoufox exposes a Playwright-API-compatible page, so any
        ``await page.goto(...)`` / ``page.click(...)`` calls written
        for patchright Just Work.
        """
        if self._browser is None:
            raise RuntimeError(
                "CamoufoxSession not started — call await session.start() first"
            )
        # Camoufox's persistent context exposes ``new_page`` like
        # standard Playwright. URL load via goto.
        page = await self._browser.new_page()
        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def stop(self) -> None:
        if self._browser is None:
            return
        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
        except Exception as exc:
            logger.debug("Camoufox: stop raised (non-fatal): %s", exc)
        finally:
            self._browser = None
            self._cm = None

    @property
    def started(self) -> bool:
        return self._browser is not None


def is_camoufox_available() -> bool:
    """True when the camoufox package can be imported.

    Used by the doctor preflight + adapter lazy error paths. Doesn't
    actually launch a browser; just checks ``import camoufox`` works.
    """
    try:
        import camoufox  # noqa: F401
        return True
    except ImportError:
        return False
