"""Event sink — the always-local observability spine (spec §9, §4).

Every ``@stage`` emits start/ok/error/skip rows here, into a SEPARATE ``events.db`` so
the high-write event log never contends with app.db and can be pruned on its own cadence.
``cli errors`` / ``cli stats`` (and any debugging session) read straight from SQL — no
log files. The opt-in Turso mirror (Phase 5) consumes a scrubbed subset of these rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from av3.domain.models import utcnow_iso

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    ts          TEXT NOT NULL,
    stage       TEXT NOT NULL,          -- 'discover' | 'score' | 'apply' | ...
    platform    TEXT,
    job_id      TEXT,
    status      TEXT NOT NULL,          -- 'start' | 'ok' | 'error' | 'skip'
    duration_ms INTEGER,
    error_type  TEXT,
    error_msg   TEXT,                   -- full detail locally; scrubbed before any mirror
    context_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run    ON events (run_id);
CREATE INDEX IF NOT EXISTS ix_events_status ON events (status);
CREATE INDEX IF NOT EXISTS ix_events_stage  ON events (stage);
CREATE INDEX IF NOT EXISTS ix_events_ts     ON events (ts);
"""


class EventSink:
    """Owns its own connection to ``events.db``. Thread-/task-safe enough for the v3
    single-process worker via WAL + busy_timeout; one sink instance per process."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_EVENTS_DDL)

    def emit(
        self,
        *,
        stage: str,
        status: str,
        run_id: str | None = None,
        platform: str | None = None,
        job_id: str | None = None,
        duration_ms: int | None = None,
        error_type: str | None = None,
        error_msg: str | None = None,
        context: dict | None = None,
    ) -> int:
        """Write one event row (full local detail). Returns the row id."""
        cur = self.conn.execute(
            """INSERT INTO events (run_id, ts, stage, platform, job_id, status,
                   duration_ms, error_type, error_msg, context_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, utcnow_iso(), stage, platform, job_id, status,
                duration_ms, error_type, error_msg,
                json.dumps(context) if context else None,
            ),
        )
        return cur.lastrowid

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def errors(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE status = 'error' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def query_errors(
        self,
        *,
        since_iso: str | None = None,
        stage: str | None = None,
        platform: str | None = None,
        run_id: str | None = None,
        limit: int = 25,
    ) -> list[sqlite3.Row]:
        """Filtered errors view for ``cli errors`` (Phase 5 1/M).

        Everything is optional and composable; every filter goes through a
        parameterized clause so the resulting query is injection-safe.
        ``since_iso`` is an ISO-8601 UTC timestamp produced by the CLI (it
        owns the ``30m|24h|7d`` parsing) — keeps the sink pure-DB.
        """
        clauses = ["status = 'error'"]
        params: list = []
        if since_iso is not None:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        params.append(int(limit))
        sql = (
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?"
        )
        return self.conn.execute(sql, params).fetchall()

    def stage_stats(self) -> list[sqlite3.Row]:
        """Per-stage counts + median-ish timing for ``cli stats``."""
        return self.conn.execute(
            """SELECT stage,
                      SUM(status='ok')    AS ok,
                      SUM(status='error') AS error,
                      SUM(status='skip')  AS skip,
                      AVG(duration_ms)    AS avg_ms
               FROM events GROUP BY stage ORDER BY stage"""
        ).fetchall()

    def query_stats(
        self,
        *,
        since_iso: str | None = None,
        platform: str | None = None,
        run_id: str | None = None,
    ) -> list[sqlite3.Row]:
        """Filtered per-stage aggregate for ``cli stats`` (Phase 5 1/M).

        Same composable-filter shape as :meth:`query_errors`. Unlike that
        method, ``stage`` is NOT a filter here — the whole point of the
        command is the by-stage breakdown.
        """
        clauses: list[str] = []
        params: list = []
        if since_iso is not None:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT stage, "
            "       SUM(status='ok')    AS ok, "
            "       SUM(status='error') AS error, "
            "       SUM(status='skip')  AS skip, "
            "       AVG(duration_ms)    AS avg_ms "
            f"FROM events {where} GROUP BY stage ORDER BY stage"
        )
        return self.conn.execute(sql, params).fetchall()

    def prune(self, keep_days: int) -> int:
        """Delete events older than ``keep_days`` (spec §4: events prune on a short
        window). Returns rows deleted."""
        cur = self.conn.execute(
            "DELETE FROM events WHERE ts < datetime('now', ?)",
            (f"-{int(keep_days)} days",),
        )
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
