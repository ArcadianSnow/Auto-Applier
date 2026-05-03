"""Nodriver browser session — alternative anti-detect backend.

Per the 2026-05-03 Tier 4 research pass, patchright (our primary
anti-detect Playwright fork) is defeated by LinkedIn's TLS/JA4
fingerprinting. The community consensus is that **Nodriver** —
the async-first successor to undetected-chromedriver, by the same
author — has the best chance of getting past LinkedIn because:

  1. It drives Chrome via the raw Chrome DevTools Protocol (CDP),
     without Playwright's wire-protocol layer that LinkedIn flags.
  2. It avoids the ``Runtime.enable`` CDP method that LinkedIn's
     detection script keys on.
  3. It runs Real Chrome (channel="chrome") with the user's actual
     profile, so ``navigator.webdriver`` and the noisy automation
     plugin manifest don't appear.

This module wraps Nodriver's lifecycle in a small class that
mirrors :class:`auto_applier.browser.session.BrowserSession`'s
shape so platform adapters can swap backends with minimal change.
The browser pool is **separate** from the patchright pool — Nodriver
adapters get their own browser process; the engine is responsible
for starting/stopping it independently.

Optional dependency
-------------------

``nodriver`` is **not** in the default install. Users who want to
try it install with::

    pip install -e ".[nodriver]"

If the package isn't installed, importing this module is fine
(the import is lazy) but :meth:`NodriverSession.start` raises
:class:`ImportError` with a clear remediation message.

This module is intentionally minimal. Real-world LinkedIn testing
hasn't been done from this branch yet — the goal is to give the
adapter a concrete shape that works locally for the discovery-only
scrape we plan to do, without committing to anti-detect choices
that won't survive contact with the real site.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from auto_applier.config import BROWSER_PROFILE_DIR

logger = logging.getLogger(__name__)


# Nodriver uses its own profile-data dir to avoid colliding with
# the patchright session. Same parent dir, different subfolder so
# each install is self-contained.
NODRIVER_PROFILE_DIR = BROWSER_PROFILE_DIR.parent / "nodriver_profile"


if TYPE_CHECKING:  # pragma: no cover - hint-only imports
    import nodriver  # noqa: F401


def _nodriver_install_hint() -> str:
    return (
        'Nodriver is an optional dependency. Install with: '
        'pip install -e ".[nodriver]" '
        '(then restart Auto Applier).'
    )


class NodriverSession:
    """Manage a Nodriver-backed browser session.

    Lifecycle:
      - ``await session.start()``   — launches Chrome + connects.
      - ``page = await session.new_tab(url)``  — opens a tab.
      - ``await session.stop()``    — closes Chrome cleanly.

    Each call is idempotent — calling ``start()`` twice is a no-op,
    calling ``stop()`` without ``start()`` is a no-op.
    """

    def __init__(
        self,
        profile_dir: Path | None = None,
        headless: bool = False,
    ) -> None:
        self.profile_dir = profile_dir or NODRIVER_PROFILE_DIR
        # Headed by default — matches our overall anti-detect policy.
        # Headless Chrome trips a different fingerprint surface that
        # we don't want to fight.
        self.headless = headless
        self._browser: Any = None  # nodriver.Browser; typed Any to keep import lazy

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            import nodriver  # type: ignore
        except ImportError as exc:
            raise ImportError(_nodriver_install_hint()) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Nodriver: starting browser (profile=%s, headless=%s)",
            self.profile_dir, self.headless,
        )
        # nodriver.start() returns a Browser singleton. Pass our
        # profile dir so cookies / login state survive across runs
        # — same model as the patchright BrowserSession.
        self._browser = await nodriver.start(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            # Use Real Chrome rather than the bundled chromium to
            # match the patchright posture. nodriver auto-detects
            # the system chrome binary when ``browser_executable_path``
            # is not provided.
            browser_args=[
                # Mute Chrome's "controlled by automation" infobar.
                # Note: nodriver already strips most of these CDP
                # flags, but it doesn't hurt to be explicit.
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ],
        )

    async def new_tab(self, url: str = "about:blank") -> Any:
        """Open a new tab. Returns a nodriver Tab object.

        Caller is responsible for the tab's lifecycle — Nodriver
        doesn't auto-close tabs on browser shutdown if they were
        explicitly opened by user code. Adapters that open tabs
        for short discovery cycles should ``await tab.close()``
        when done; we don't manage tabs here.
        """
        if self._browser is None:
            raise RuntimeError(
                "NodriverSession not started — call await session.start() first"
            )
        return await self._browser.get(url)

    async def stop(self) -> None:
        if self._browser is None:
            return
        try:
            self._browser.stop()
        except Exception as exc:
            logger.debug("Nodriver: stop raised (non-fatal): %s", exc)
        finally:
            self._browser = None

    @property
    def started(self) -> bool:
        return self._browser is not None


def is_nodriver_available() -> bool:
    """True when the nodriver package can be imported.

    Used by the doctor preflight + the platform adapter's lazy
    error path. Doesn't actually launch a browser; just checks
    that ``import nodriver`` succeeds.
    """
    try:
        import nodriver  # noqa: F401
        return True
    except ImportError:
        return False
