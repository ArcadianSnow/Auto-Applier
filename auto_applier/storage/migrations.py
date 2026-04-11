"""Forward-only CSV schema migrations.

Auto Applier v2 stores records in plain CSV so users can open them in
Excel. That means when dataclass models gain new fields between
versions, on-disk files fall out of sync. This module detects that
drift and rewrites the file in place, while preserving existing data
and backing up the previous version.

Design principles:

- **Forward-only.** We never support downgrading. Old columns dropped
  from the dataclass are archived in the backup file only.
- **Opportunistic.** Migration runs transparently the first time a
  CSV is accessed after a field change. No separate command needed
  (though one is provided for peace of mind).
- **Safe.** Every migration first writes a timestamped backup to
  ``data/.backups/`` before touching the live file.
- **Idempotent.** Running twice is a no-op.
- **Observable.** Every migration is logged to ``data/.schema_version.json``
  with before/after column lists and a timestamp.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_applier.config import BACKUP_DIR, SCHEMA_VERSION_FILE


def _model_columns(model_type: type) -> list[str]:
    """Return the current set of CSV columns for a dataclass model."""
    return [f.name for f in dc_fields(model_type)]


def _field_defaults(model_type: type) -> dict[str, Any]:
    """Return a mapping of field name -> string-serialized default.

    Used to backfill new columns when migrating older CSV files.
    Mirrors the serialization that ``repository.save()`` performs.
    """
    from dataclasses import MISSING

    defaults: dict[str, Any] = {}
    for f in dc_fields(model_type):
        if f.default is not MISSING:
            value = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            value = f.default_factory()  # type: ignore[misc]
        else:
            value = ""
        # Match repository.save() string coercions so migrated rows
        # round-trip identically.
        if isinstance(value, bool):
            value = str(value)
        elif isinstance(value, list):
            value = str(value)
        defaults[f.name] = value
    return defaults


def _read_header(path: Path) -> list[str]:
    """Return the header row of a CSV file, or an empty list if empty."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return []


def _load_schema_state() -> dict:
    if not SCHEMA_VERSION_FILE.exists():
        return {"models": {}, "history": []}
    try:
        return json.loads(SCHEMA_VERSION_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"models": {}, "history": []}


def _save_schema_state(state: dict) -> None:
    SCHEMA_VERSION_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


def _backup(path: Path) -> Path:
    """Copy a CSV to the backup directory with a UTC timestamp suffix."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"{path.stem}.{ts}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


def migrate_csv(path: Path, model_type: type) -> dict | None:
    """Align ``path`` with the current schema of ``model_type``.

    Returns a dict describing the migration if one was performed, or
    ``None`` if no action was needed. Safe to call every time before
    reading or writing the file.

    Behavior:
    - Missing file: nothing to migrate (caller creates it with current header).
    - Empty file (or header-only, no drift): nothing to migrate.
    - Header exactly matches current model: nothing to migrate.
    - Header drifts: back up, rewrite with the current model's header,
      preserving overlapping columns, backfilling new columns with
      dataclass defaults, and dropping any columns not present on the
      new model (the dropped data is preserved in the backup).
    """
    if not path.exists():
        return None

    current = _read_header(path)
    if not current:
        return None

    target = _model_columns(model_type)
    if current == target:
        return None

    # At least one of: new columns added, old columns removed, or
    # columns reordered. In any case we rewrite to the canonical order.
    backup_path = _backup(path)

    defaults = _field_defaults(model_type)
    overlap = [c for c in current if c in target]
    added = [c for c in target if c not in current]
    removed = [c for c in current if c not in target]

    # Read existing rows
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Rewrite with canonical header, preserving overlap + backfilling added
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=target)
        writer.writeheader()
        for row in rows:
            new_row: dict[str, Any] = {}
            for col in target:
                if col in overlap:
                    new_row[col] = row.get(col, defaults[col])
                else:
                    new_row[col] = defaults[col]
            writer.writerow(new_row)

    record = {
        "model": model_type.__name__,
        "path": str(path.name),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backup": str(backup_path.name),
        "before_columns": current,
        "after_columns": target,
        "added": added,
        "removed": removed,
        "rows_migrated": len(rows),
    }
    state = _load_schema_state()
    state["models"][model_type.__name__] = target
    state["history"].append(record)
    _save_schema_state(state)

    return record


def record_current_schema(model_types: list[type]) -> None:
    """Record the current schema for a list of models without rewriting data.

    Called on first-run / fresh-install to pin the initial schema so
    future drift is detectable. Does not touch CSV files.
    """
    state = _load_schema_state()
    changed = False
    for mt in model_types:
        name = mt.__name__
        cols = _model_columns(mt)
        if state["models"].get(name) != cols:
            state["models"][name] = cols
            changed = True
    if changed:
        _save_schema_state(state)


def list_migration_history() -> list[dict]:
    """Return the recorded migration history (newest last)."""
    return _load_schema_state().get("history", [])
