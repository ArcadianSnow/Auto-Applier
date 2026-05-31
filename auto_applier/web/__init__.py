"""Local web UI + worker service (spec §3, §10).

Phase 4 (1/M) lands the skeleton: FastAPI app factory, lifespan that boots the
staged-worker scheduler as a background asyncio task, read-only JSON API, and a
placeholder index page. The live dashboard, SSE event stream, pause toggle,
login-on-demand UX, and onboarding wizard arrive in sub-phases (2/M)–(6/M).

The package is import-safe even when ``fastapi`` / ``uvicorn`` / ``jinja2``
aren't installed — those imports happen inside :func:`create_app` so the rest
of ``auto_applier.*`` keeps working in a stripped install. ``av3 serve`` and the web
tests are the only entry points that require the extras.
"""

from auto_applier.web.service import SchedulerService
from auto_applier.web.state import WebState

__all__ = ["SchedulerService", "WebState", "create_app"]


def create_app(
    *,
    state: WebState,
    service: SchedulerService | None = None,
    watchers=None,
    launcher=None,
):
    """Thin re-export so callers don't import the module path directly. The
    real factory lives in :mod:`auto_applier.web.app` to keep this ``__init__`` light
    (and skip the FastAPI import until the app is actually built).

    ``watchers`` (3/M) are control-handoff daemons (F6 + idle-detect) the
    lifespan manages alongside the scheduler service. ``None`` is the test
    default — the dashboard pause button still works without them.

    ``launcher`` (4/M) is the :class:`HeadedBrowserLauncher` used by
    login-on-demand + assisted submit endpoints. ``None`` builds a launcher
    with no bot-browser binding (URLs open in the OS default browser).
    """
    from auto_applier.web.app import create_app as _build

    return _build(
        state=state,
        service=service,
        watchers=watchers,
        launcher=launcher,
    )
