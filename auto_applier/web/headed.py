"""Headed-browser launcher — opens URLs the user needs to interact with (spec §8b, §6a).

Two Phase 4 (4/M) use-cases share this primitive:

1. **Login-on-demand.** When a source flips to ``AUTH_REQUIRED`` the dashboard
   shows a "Log in" button. Clicking it opens the captured ``login_url`` so the
   user can sign back in — *into the persistent Chrome profile the bot uses*
   so the cookies the apply worker needs land in the right jar.
2. **Assisted submit.** When the apply worker pre-fills a form but stops
   short of submitting (``ASSISTED_PENDING``) the per-job page shows an
   "Open application" button. Same launcher: the page opens in the bot's
   profile so the user can review what was typed and click Submit.

**Why open via the running BrowserSession when we have one:** the persistent
profile is what carries logged-in cookies between bot and human. If we open
the login URL in the OS default browser instead, the user can sign in fine —
but the bot's next apply cycle still sees AUTH_REQUIRED because *that*
profile never saw the auth. Opening in the same context guarantees cookie
continuity.

**Why fall back to ``webbrowser.open()``:** in ``--no-scheduler`` /
diagnostics mode there's no BrowserSession to reach for; the user still
wants the URL launched (so they can at least visit it). The fallback is a
best-effort convenience — the dashboard tells the user which mode fired so
they understand whether cookies will end up in the bot's profile.

The launcher carries **no state about pause sources** — pausing the
scheduler while a user is mid-login is a higher-layer concern (login while
the apply worker is still firing against the source is fine; the source is
``AUTH_REQUIRED`` so the worker is already skipping it per
:func:`auto_applier.sources.health.is_paused`).
"""

from __future__ import annotations

import logging
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from auto_applier.web.control import ManualTakeover

logger = logging.getLogger(__name__)

__all__ = ["HeadedBrowserLauncher", "LaunchResult"]


@dataclass(frozen=True)
class LaunchResult:
    """Observable outcome of a launch attempt. The dashboard surfaces ``mode``
    so the user understands whether cookies will end up in the bot's
    persistent profile (``bot_browser``) or only in their OS default browser
    (``default_browser``)."""

    ok: bool
    mode: str          # "bot_browser" | "default_browser" | "unavailable"
    url: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "url": self.url,
            "note": self.note,
        }


class HeadedBrowserLauncher:
    """Open a URL for the user. Prefers the bot's persistent Chrome profile
    (via :class:`auto_applier.sources.browser.session.BrowserSession.new_page`) so
    cookies the apply worker needs land in the right jar; falls back to the
    OS default browser when no session is available.

    Construct with ``new_page`` set to ``BrowserSession.new_page`` (the
    async callable). Tests pass a stub coroutine that records the URL.
    ``new_page=None`` is valid — that's the ``--no-scheduler`` posture; every
    launch goes through the OS default browser.
    """

    def __init__(
        self,
        *,
        new_page: Callable[[], Awaitable[Any]] | None = None,
        fallback_open: Callable[[str], bool] | None = None,
        takeover: ManualTakeover | None = None,
    ):
        self._new_page = new_page
        # Indirection so tests can verify the fallback path was taken without
        # actually launching a browser on the runner. Production passes None
        # and we use ``webbrowser.open``.
        self._fallback_open = fallback_open or webbrowser.open
        # When set, opening a URL in the bot's profile registers a *manual takeover*
        # (engage now, release on the tab's close event) so the scheduler masks the apply
        # stage while the user is hands-on in that shared Chrome window. None → no masking
        # (the apply worker isn't running anyway in the fallback/no-session posture).
        self._takeover = takeover

    @property
    def has_bot_browser(self) -> bool:
        """True iff we can open URLs in the bot's persistent profile.

        Drives the dashboard's "warning: this will open in your default
        browser" copy when False."""
        return self._new_page is not None

    def _register_takeover(self, page: Any) -> None:
        """Engage a manual takeover for ``page`` and release it when the tab closes.

        Best-effort: if no takeover tracker is wired, or the page object doesn't expose a
        Playwright-style ``on('close', ...)`` hook, we still engage (so apply is masked) and
        rely on the takeover's safety timeout to auto-release. Never raises — a takeover that
        can't register must not break opening the page."""
        if self._takeover is None:
            return
        try:
            token = self._takeover.engage()
        except Exception as exc:  # noqa: BLE001 — masking is best-effort, never fatal
            logger.warning("headed launcher: takeover.engage() failed: %s", exc)
            return
        try:
            page.on("close", lambda _p=None: self._takeover.release(token))
        except Exception as exc:  # noqa: BLE001
            # No close hook (stub page / odd driver) → the safety timeout still releases it.
            logger.debug("headed launcher: could not bind page close → relying on "
                         "takeover timeout: %s", exc)

    async def open(self, url: str) -> LaunchResult:
        """Open ``url`` and return a structured result for the API response.

        Never raises — a Playwright error during ``new_page()`` or ``goto()``
        falls through to the OS default browser; only a malformed URL
        (``url is None``) returns ``ok=False`` so the API can 400 cleanly.
        """
        if not url:
            return LaunchResult(
                ok=False,
                mode="unavailable",
                url="",
                note="no URL to open",
            )

        if self._new_page is not None:
            try:
                page = await self._new_page()
                # The user is now hands-on in the bot's shared Chrome window — register a
                # manual takeover so the scheduler masks the apply stage (stops churning
                # tabs underneath them) until this tab closes or the safety timeout fires.
                self._register_takeover(page)
                # Best-effort navigation. ``page.goto`` raises on net errors;
                # we still consider the launch a partial success because the
                # tab IS open and the user can fix the URL by hand.
                try:
                    await page.goto(url)
                    return LaunchResult(
                        ok=True,
                        mode="bot_browser",
                        url=url,
                        note="opened in bot's persistent Chrome profile",
                    )
                except Exception as exc:  # noqa: BLE001 — soft-fail to fallback
                    logger.warning(
                        "headed launcher: goto(%s) failed: %s; falling back",
                        url, exc,
                    )
            except Exception as exc:  # noqa: BLE001 — soft-fail to fallback
                logger.warning(
                    "headed launcher: new_page() failed: %s; falling back",
                    exc,
                )

        # Fallback path: OS default browser. ``webbrowser.open`` returns False
        # if it couldn't find an actionable handler — surface that so the UI
        # can tell the user to navigate manually.
        try:
            ok = bool(self._fallback_open(url))
        except Exception as exc:  # noqa: BLE001
            logger.warning("headed launcher: fallback open(%s) raised: %s",
                          url, exc)
            ok = False

        return LaunchResult(
            ok=ok,
            mode="default_browser" if ok else "unavailable",
            url=url,
            note=(
                "opened in OS default browser (cookies won't reach the bot's "
                "profile)" if ok
                else "could not launch any browser"
            ),
        )
