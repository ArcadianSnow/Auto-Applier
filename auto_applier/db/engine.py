"""SQLite engine: connection factory, pragmas, schema init, backups (spec §4).

SQLite is the v3 system of record (replaces v2's CSV). We enable WAL (concurrent
reader + writer — the always-on worker writes while the web UI reads) and enforce
foreign keys. ``init_app_db`` applies ``schema.sql`` idempotently.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

_SCHEMA_PACKAGE = "auto_applier.db"
_SCHEMA_RESOURCE = "schema.sql"


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a tuned connection. Creates the parent dir if missing.

    Pragmas: WAL (concurrency), foreign_keys ON (referential integrity),
    busy_timeout (tolerate the worker/UI write overlap), row factory = Row.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we manage transactions explicitly via `tx`
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (autocommit is off via isolation_level=None).

    Commits on success, rolls back on any exception — gives the atomic writes
    that v2's CSV layer lacked (the continuous-run + GUI race root cause).
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _load_schema_sql() -> str:
    return resources.files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_RESOURCE).read_text(
        encoding="utf-8"
    )


def init_app_db(db_path: Path | str) -> sqlite3.Connection:
    """Create/upgrade the main app DB from ``schema.sql`` (idempotent) and return a
    connection. ``CREATE TABLE IF NOT EXISTS`` makes this forward-only and safe to
    re-run on every startup (the doctor relies on this)."""
    conn = connect(db_path)
    conn.executescript(_load_schema_sql())
    return conn


def backup_db(db_path: Path | str, backups_dir: Path | str) -> Path:
    """Take a timestamped, consistent snapshot of a SQLite file (spec §4 backups).

    Uses the online backup API so it is safe while the DB is in use (WAL included).
    Returns the snapshot path.
    """
    db_path = Path(db_path)
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups_dir / f"{db_path.stem}.{ts}{db_path.suffix}"

    src = connect(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def rotate_backups(backups_dir: Path | str, stem: str, keep: int = 10) -> list[Path]:
    """Keep the newest ``keep`` snapshots for ``stem``; delete older ones. Returns deleted."""
    backups_dir = Path(backups_dir)
    if not backups_dir.exists():
        return []
    snaps = sorted(
        backups_dir.glob(f"{stem}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted: list[Path] = []
    for old in snaps[keep:]:
        old.unlink()
        deleted.append(old)
    return deleted
