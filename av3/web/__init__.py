"""Local web UI + worker service (spec §3, §10).

Phase 4 (1/M) lands the skeleton: FastAPI app factory, lifespan that boots the
staged-worker scheduler as a background asyncio task, read-only JSON API, and a
placeholder index page. The live dashboard, SSE event stream, pause toggle,
login-on-demand UX, and onboarding wizard arrive in sub-phases (2/M)–(6/M).

The package is import-safe even when ``fastapi`` / ``uvicorn`` / ``jinja2``
aren't installed — those imports happen inside :func:`create_app` so the rest
of ``av3.*`` keeps working in a stripped install. ``av3 serve`` and the web
tests are the only entry points that require the extras.
"""

from av3.web.service import SchedulerService
from av3.web.state import WebState

__all__ = ["SchedulerService", "WebState", "create_app"]


def create_app(
    *,
    state: WebState,
    service: SchedulerService | None = None,
):
    """Thin re-export so callers don't import the module path directly. The
    real factory lives in :mod:`av3.web.app` to keep this ``__init__`` light
    (and skip the FastAPI import until the app is actually built)."""
    from av3.web.app import create_app as _build

    return _build(state=state, service=service)
