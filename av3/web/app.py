"""FastAPI app factory + lifespan.

Build the app via :func:`create_app`. The lifespan starts and stops the
:class:`av3.web.service.SchedulerService` (when provided). The web state is
attached to ``app.state.web_state`` so route handlers reach it without a
dependency-injection layer.

Why a factory rather than a module-level app: tests want isolated apps with
their own tmp DB; the CLI wants one production app per process; both should
agree on the same construction path. A module-level ``app = FastAPI(...)``
would force the CLI's settings on the test process and vice versa.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from av3 import __version__
from av3.web.routes import api_router, pages_router
from av3.web.service import SchedulerService
from av3.web.state import WebState

# Package-relative — templates + static ship via setuptools package_data so
# an installed wheel finds them at the same path.
_PKG_DIR = Path(__file__).parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"


def create_app(
    *,
    state: WebState,
    service: SchedulerService | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Parameters
    ----------
    state
        Read-only data plumbing the route handlers need (app.db conn,
        events.db path, settings). Required.
    service
        SchedulerService managing the staged-worker loop. Optional — when
        ``None``, the lifespan is a no-op and read-only endpoints still
        answer. Useful for tests + headless diagnostics (``av3 serve
        --no-scheduler``, future).
    """

    # StaticFiles raises if the directory is missing — create lazily so
    # fresh installs work before (2/M) adds any assets.
    _STATIC_DIR.mkdir(exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if service is not None:
            await service.start()
        try:
            yield
        finally:
            if service is not None:
                await service.stop()

    app = FastAPI(
        title="Auto Applier v3",
        version=__version__,
        lifespan=lifespan,
        # Disable the default /docs and /redoc — the dashboard is the UI; the
        # JSON API is a side door we'd rather not advertise on a localhost
        # bind that may end up exposed via host=0.0.0.0 (spec §3 LAN access).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    # ``app.state`` is the FastAPI/Starlette idiom for app-wide singletons.
    # Route handlers reach for these by name (see ``av3/web/routes.py``).
    app.state.web_state = state
    app.state.scheduler_service = service
    app.state.templates = templates

    app.include_router(api_router, prefix="/api")
    app.include_router(pages_router)

    return app
