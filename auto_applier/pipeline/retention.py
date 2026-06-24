"""Retention pruning + backup rotation (spec section 4).

> "Auto-prune ephemera (SKIPPED/FILTERED jobs and stale discovery rows after a
>  configurable window, e.g. 30d) to keep the DB lean; keep APPLIED history
>  indefinitely (it's the record of what you applied to, and the dedup source
>  of truth); take periodic local backup snapshots of the SQLite file
>  (timestamped, rotated). events.db errors prune on a shorter window."

This module owns the four maintenance operations the v3 always-on operating
model needs to keep running for months without manual intervention:

  * :func:`prune_ephemeral` - delete jobs in EPHEMERAL_STATES older than the
    retention window. APPLIED jobs are kept forever (dedup source of truth).
  * :func:`prune_events` - shorter window for events.db (higher write volume).
  * :func:`backup_app_db` / :func:`backup_events_db` - timestamped snapshot
    via SQLite's online backup API (safe while the DB is in use, WAL included).
  * :func:`run_backup_cycle` - one-shot "back up both + rotate" used by the
    CLI and the scheduler's maintenance hook.

**Atomicity** matters here: a partial prune that deletes half the matched
rows is worse than no prune. Each prune runs inside an explicit transaction
via :func:`auto_applier.db.engine.tx` so a crash mid-delete rolls back.

**Cascade behavior:** ``jobs.id`` is the parent FK for ``job_scores`` and
``applications`` (``ON DELETE CASCADE``). So deleting a SKIPPED/FILTERED job
also removes its score and application rows. SKIPPED/FILTERED never reach
APPLIED, so no real application history is lost — only the audit of "we
tried to score this and rejected it" gets pruned, which matches the spec
intent ("ephemera").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_applier.config.settings import Settings
from auto_applier.db.engine import backup_db, rotate_backups, tx
from auto_applier.domain.state import EPHEMERAL_STATES, TERMINAL_STATES
from auto_applier.telemetry import EventSink, get_sink

__all__ = [
    "BackupResult",
    "BackupRunSummary",
    "PruneResult",
    "backup_app_db",
    "backup_events_db",
    "prune_ephemeral",
    "prune_events",
    "prune_proposed_artifacts",
    "run_backup_cycle",
]


# --------------------------------------------------------------- summaries

@dataclass
class PruneResult:
    """One prune operation's outcome. ``deleted`` is the row count actually
    removed; ``cutoff_iso`` is the UTC threshold used so the caller can log
    "deleted N rows older than 2026-05-01"."""

    table: str
    deleted: int
    cutoff_iso: str


@dataclass
class BackupResult:
    """One DB's backup snapshot. ``snapshot_path`` is the destination; ``rotated``
    is the count of older snapshots deleted by the keep-N policy."""

    db_label: str          # 'app' | 'events'
    snapshot_path: Path
    rotated: int = 0


@dataclass
class BackupRunSummary:
    """One ``run_backup_cycle`` invocation's results. Both backups are attempted
    even if one fails; the failure is recorded in ``errors`` so a transient
    issue on events.db doesn't skip app.db."""

    backups: list[BackupResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # db_label -> error str

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------- prune

def _utc_cutoff(days: int) -> tuple[str, str]:
    """Returns (cutoff_iso, sql_literal) for "older than ``days`` ago in UTC."

    The SQL literal is the exact string we want in ``WHERE updated_at <``
    rather than relying on SQLite's ``datetime('now', '-N days')`` math —
    we want the cutoff computed in Python so tests can drive it deterministically
    (the SQLite datetime function reads the wall clock at execute time, not
    callable from a controlled now()).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    iso = cutoff.isoformat(timespec="seconds")
    return iso, iso


def prune_ephemeral(conn: sqlite3.Connection, retention_days: int) -> PruneResult:
    """Delete EPHEMERAL_STATES jobs older than ``retention_days``.

    ``EPHEMERAL_STATES = {SKIPPED, FILTERED}`` (spec §4). APPLIED is excluded
    — it's the dedup source of truth and is kept indefinitely. The matching
    ``job_scores`` + ``applications`` rows are removed by FK cascade.

    Runs inside an explicit transaction so a crash mid-delete rolls back —
    a partial prune that orphans cascades or splits a state's history is
    worse than no prune.

    Returns the count actually deleted + the cutoff used.
    """
    cutoff_iso, _ = _utc_cutoff(retention_days)
    states = tuple(s.value for s in EPHEMERAL_STATES)
    placeholders = ",".join("?" * len(states))
    with tx(conn):
        cur = conn.execute(
            f"DELETE FROM jobs WHERE state IN ({placeholders}) AND updated_at < ?",
            (*states, cutoff_iso),
        )
        deleted = cur.rowcount
    _emit("prune", status="ok", context={
        "table": "jobs", "deleted": deleted, "cutoff": cutoff_iso,
        "scope": "EPHEMERAL_STATES",
    })
    return PruneResult(table="jobs", deleted=deleted, cutoff_iso=cutoff_iso)


def prune_events(events_db_path: Path | str, retention_days: int) -> PruneResult:
    """Delete events older than ``retention_days``. Events get a shorter
    window than app data because they're the highest-write table and ageing
    rows have no operational use after a few days (spec §4).

    Opens its own short-lived :class:`EventSink` connection so prune can be
    called from a CLI / scheduler hook without holding a long-lived events
    connection in the caller.
    """
    cutoff_iso, _ = _utc_cutoff(retention_days)
    sink = EventSink(events_db_path)
    try:
        # The sink's existing ``prune`` uses SQLite's datetime math; we replicate
        # the same shape but pass the Python-computed cutoff for testability.
        cur = sink.conn.execute(
            "DELETE FROM events WHERE ts < ?", (cutoff_iso,)
        )
        deleted = cur.rowcount
    finally:
        sink.close()
    _emit("prune", status="ok", context={
        "table": "events", "deleted": deleted, "cutoff": cutoff_iso,
    })
    return PruneResult(table="events", deleted=deleted, cutoff_iso=cutoff_iso)


def prune_proposed_artifacts(
    conn: sqlite3.Connection, settings: Settings, retention_days: int
) -> PruneResult:
    """Delete stale ``artifacts/proposed/<job_id>.json`` files (batched assisted review).

    Each prepared job persists a per-job "complete proposed application" JSON for the
    "In Progress" page (``auto_applier.resume.proposed``). The page only renders the
    artifact while the job is a live batch member awaiting the owner, so once the job is
    **dispositioned** the file is dead weight. The retention rule (keyed to disposition,
    not file age) prunes a proposed file when EITHER:

      * its ``<job_id>`` is no longer a row in ``jobs`` (**orphan** — the job was pruned
        by :func:`prune_ephemeral` or never existed), regardless of age; OR
      * its job is in a **terminal** state (``APPLIED`` / ``SKIPPED`` / ``FILTERED``)
        **and** the job's ``updated_at`` is older than ``retention_days`` — i.e. the
        owner dealt with it and the grace window has passed.

    A file whose job is still in flight (REVIEW, QUEUED_APPLY, …) is **kept** regardless
    of age — the owner may still act on it on the page. This mirrors :func:`prune_ephemeral`
    (terminal + same window) while never touching an in-flight review.

    File deletes aren't transactional; a per-file ``OSError`` is logged-and-skipped so one
    locked/vanished file never aborts the sweep. No DB writes — read-only on ``jobs``.
    """
    cutoff_iso, _ = _utc_cutoff(retention_days)
    proposed_dir = settings.artifacts_dir / "proposed"
    if not proposed_dir.exists():
        return PruneResult(table="proposed_artifacts", deleted=0, cutoff_iso=cutoff_iso)

    # job_id -> (state, updated_at) for every job still present. Absent ⇒ orphan.
    job_info: dict[str, tuple[str, str]] = {
        str(row[0]): (str(row[1]), str(row[2] or ""))
        for row in conn.execute("SELECT id, state, updated_at FROM jobs")
    }
    terminal = {s.value for s in TERMINAL_STATES}

    deleted = 0
    for path in proposed_dir.glob("*.json"):
        job_id = path.stem
        info = job_info.get(job_id)
        if info is None:
            remove = True  # orphan — nothing left to render
        else:
            state, updated_at = info
            remove = state in terminal and updated_at < cutoff_iso
        if not remove:
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:  # noqa: PERF203 — log-and-skip one bad file, keep sweeping
            _emit("prune", status="error", context={
                "table": "proposed_artifacts", "path": str(path), "error": str(exc),
            })

    _emit("prune", status="ok", context={
        "table": "proposed_artifacts", "deleted": deleted, "cutoff": cutoff_iso,
    })
    return PruneResult(table="proposed_artifacts", deleted=deleted, cutoff_iso=cutoff_iso)


# --------------------------------------------------------------- backups

def backup_app_db(settings: Settings, keep: int) -> BackupResult:
    """Snapshot app.db + rotate older snapshots to ``keep`` newest."""
    dest = backup_db(settings.app_db_path, settings.backups_dir)
    deleted = rotate_backups(settings.backups_dir, settings.app_db_path.stem, keep=keep)
    _emit("backup", status="ok", context={
        "db": "app", "snapshot": str(dest), "rotated": len(deleted),
    })
    return BackupResult(db_label="app", snapshot_path=dest, rotated=len(deleted))


def backup_events_db(settings: Settings, keep: int) -> BackupResult:
    """Snapshot events.db + rotate. Smaller ``keep`` than app.db usually
    (events are higher-volume and less retention-valuable), but the knob is
    the caller's."""
    dest = backup_db(settings.events_db_path, settings.backups_dir)
    deleted = rotate_backups(settings.backups_dir, settings.events_db_path.stem, keep=keep)
    _emit("backup", status="ok", context={
        "db": "events", "snapshot": str(dest), "rotated": len(deleted),
    })
    return BackupResult(db_label="events", snapshot_path=dest, rotated=len(deleted))


def run_backup_cycle(settings: Settings) -> BackupRunSummary:
    """Back up app.db + events.db; rotate both. ``settings.retention.backup_keep``
    is the rotation policy (same for both DBs — keeping the knob simple for v3.0).

    Both backups are attempted even if one fails; the failure is recorded so
    a transient event-DB issue (e.g. WAL checkpoint contention) doesn't skip
    the app-DB snapshot, which is the load-bearing one.
    """
    summary = BackupRunSummary()
    keep = settings.retention.backup_keep

    try:
        summary.backups.append(backup_app_db(settings, keep))
    except Exception as exc:  # noqa: BLE001 — record + continue
        summary.errors["app"] = f"{type(exc).__name__}: {exc}"
        _emit("backup", status="error", context={"db": "app", "error": str(exc)})

    try:
        summary.backups.append(backup_events_db(settings, keep))
    except Exception as exc:  # noqa: BLE001
        summary.errors["events"] = f"{type(exc).__name__}: {exc}"
        _emit("backup", status="error", context={"db": "events", "error": str(exc)})

    return summary


# --------------------------------------------------------------- telemetry

def _emit(stage: str, *, status: str, context: dict) -> None:
    """Emit a maintenance event. Drops silently when no sink is configured."""
    sink = get_sink()
    if sink is None:
        return
    sink.emit(stage=stage, status=status, context=context)
