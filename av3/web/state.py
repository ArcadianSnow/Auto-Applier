"""WebState — the data-access surface for FastAPI route handlers.

The handlers only read v3 state; they never mutate. This module wraps the
pieces they reach for (paths to app.db / events.db, settings) so:

  * tests can inject a stubbed WebState with their own tmp DB paths without
    spinning up a real scheduler.
  * the CLI's ``av3 serve`` builds one production WebState that the
    SchedulerService also reuses.

**Why a per-request connection** rather than one shared :class:`sqlite3.Connection`:
SQLite forbids sharing a connection across threads by default. The web app's
ASGI dispatch (uvicorn in prod, TestClient in tests) can hop threads, and the
scheduler workers run on the asyncio loop. Opening a short-lived read
connection in each request handler is cheap (~1 ms on a WAL'd SQLite file)
and sidesteps the thread issue cleanly. Writers still go through the
scheduler workers' own connections — the web layer is read-only.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from av3.config import Settings


@dataclass
class WebState:
    """Read-only data plumbing for the dashboard."""

    settings: Settings
    app_db_path: Path
    events_db_path: Path

    @contextmanager
    def app_conn(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived read-write connection to app.db.

        Read-write because the (4/M) "resume source" endpoint will mark
        sources healthy via the same path; the (1/M) handlers only read.
        WAL means readers never block writers, so this is safe regardless of
        the scheduler workers' own connections.

        Always close — caller uses ``with state.app_conn() as conn:`` so the
        connection is released back to the OS on every request.
        """
        conn = sqlite3.connect(str(self.app_db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL is per-file (set by the scheduler's init); we don't re-pragma
        # it here because that touches the file. busy_timeout matters because
        # we WILL contend with the scheduler's writes on a hot pipeline.
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def events_conn(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived read-only connection to events.db.

        SQLite's URI form lets us request ``mode=ro`` cleanly — fails fast if
        the file is missing (better than silently creating a new events.db
        from a typo). WAL readers never block writers, so this is safe to
        call from inside a request handler without coordinating with the
        sink.
        """
        conn = sqlite3.connect(
            f"file:{self.events_db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
