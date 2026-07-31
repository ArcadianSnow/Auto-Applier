"""Where the Chromium binary lives — one definition, shared by every consumer.

Three places need to agree on this or the apply path breaks in confusing ways: ``doctor``
(does a browser exist?), ``setup_ops.install_browser`` (put one there), and the **frozen**
runtime (find the one that was installed). They used to agree only by coincidence.

The frozen build is why this module exists. Playwright/patchright resolve browsers through
their own per-user registry, but inside a PyInstaller bundle the package directory is the
extracted ``_internal/patchright`` — so the driver looks for Chromium *inside the bundle*,
where it will never be, and reports::

    BrowserType.launch: Executable doesn't exist at …\\_internal\\patchright\\driver\\
    package\\.local-browsers\\chromium-…

Measured on a probe exe 2026-07-31. Pointing ``PLAYWRIGHT_BROWSERS_PATH`` at the ordinary
per-user cache fixes it (verified: browser launches from the frozen exe). That's also the
right answer for ``build.py``'s deliberate "Chromium is NOT bundled — fetched on first run"
design: the fetch and the launch then use the same directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["browser_registry_dirs", "default_browsers_path", "ensure_browsers_path"]

#: Env var Playwright/patchright honour to override the browser cache location.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def _registry_roots() -> list[Path]:
    """Per-OS cache roots for BOTH registries (patchright is a fork with its own cache)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return [base / "ms-playwright", base / "patchright"]


def browser_registry_dirs() -> list[Path]:
    """Candidate browser-cache roots, honouring an explicit ``PLAYWRIGHT_BROWSERS_PATH``.

    ``"0"`` is Playwright's documented "keep browsers inside the package" value, so it is NOT
    treated as a path override.
    """
    override = os.environ.get(BROWSERS_PATH_ENV)
    if override and override != "0":
        return [Path(override)]
    return _registry_roots()


def _has_chromium(root: Path) -> bool:
    if not root.exists():
        return False
    return any(root.glob("chromium-*")) or any(root.glob("chromium_headless_shell-*"))


def default_browsers_path() -> Path:
    """The directory the frozen app should use: the first root that already HAS a Chromium,
    else the platform default (so a subsequent install lands somewhere predictable)."""
    roots = _registry_roots()
    for root in roots:
        if _has_chromium(root):
            return root
    return roots[0]


def ensure_browsers_path() -> str:
    """Set ``PLAYWRIGHT_BROWSERS_PATH`` for a frozen app, unless already set. Returns the path.

    No-op when not frozen (a pip install resolves browsers correctly on its own) and when the
    user has set the variable themselves — an explicit choice always wins.
    """
    if not getattr(sys, "frozen", False):
        return os.environ.get(BROWSERS_PATH_ENV, "")
    existing = os.environ.get(BROWSERS_PATH_ENV)
    if existing:
        return existing
    path = str(default_browsers_path())
    os.environ[BROWSERS_PATH_ENV] = path
    return path
