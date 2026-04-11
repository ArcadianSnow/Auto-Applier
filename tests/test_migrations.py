"""Tests for the forward-only CSV schema migration framework."""
import csv
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_applier.storage import migrations


@dataclass
class _Legacy:
    """Pretend old model with 3 columns."""
    id: str
    name: str = ""
    score: int = 0


@dataclass
class _Current:
    """Same model after adding + removing columns."""
    id: str
    name: str = ""
    # score removed
    tags: list = field(default_factory=list)
    enabled: bool = False
    note: str = "n/a"


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Redirect BACKUP_DIR and SCHEMA_VERSION_FILE into a temp area."""
    backup = tmp_path / ".backups"
    backup.mkdir()
    schema = tmp_path / ".schema_version.json"
    monkeypatch.setattr(migrations, "BACKUP_DIR", backup)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION_FILE", schema)
    return tmp_path


def _write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return (reader.fieldnames or []), list(reader)


class TestMigrateCsv:
    def test_noop_when_headers_match(self, tmp_data):
        path = tmp_data / "current.csv"
        header = ["id", "name", "tags", "enabled", "note"]
        _write_csv(path, header, [{"id": "1", "name": "a", "tags": "[]", "enabled": "False", "note": "x"}])

        result = migrations.migrate_csv(path, _Current)
        assert result is None
        header_after, _ = _read_csv(path)
        assert header_after == header

    def test_missing_file_returns_none(self, tmp_data):
        assert migrations.migrate_csv(tmp_data / "nope.csv", _Current) is None

    def test_empty_file_returns_none(self, tmp_data):
        path = tmp_data / "empty.csv"
        path.touch()
        assert migrations.migrate_csv(path, _Current) is None

    def test_adds_new_columns_with_defaults(self, tmp_data):
        path = tmp_data / "data.csv"
        _write_csv(path, ["id", "name", "score"], [
            {"id": "1", "name": "alice", "score": "5"},
            {"id": "2", "name": "bob", "score": "3"},
        ])

        result = migrations.migrate_csv(path, _Current)

        assert result is not None
        assert result["added"] == ["tags", "enabled", "note"]
        assert result["removed"] == ["score"]
        assert result["rows_migrated"] == 2

        header, rows = _read_csv(path)
        assert header == ["id", "name", "tags", "enabled", "note"]
        assert len(rows) == 2
        assert rows[0]["id"] == "1"
        assert rows[0]["name"] == "alice"
        assert rows[0]["tags"] == "[]"       # list default
        assert rows[0]["enabled"] == "False" # bool default
        assert rows[0]["note"] == "n/a"      # str default
        assert "score" not in rows[0]

    def test_backup_created_before_rewrite(self, tmp_data):
        path = tmp_data / "data.csv"
        _write_csv(path, ["id", "name", "score"], [{"id": "1", "name": "alice", "score": "5"}])

        result = migrations.migrate_csv(path, _Current)
        assert result is not None
        backups = list(migrations.BACKUP_DIR.glob("data.*.csv"))
        assert len(backups) == 1
        # Backup retains the old schema
        old_header, old_rows = _read_csv(backups[0])
        assert old_header == ["id", "name", "score"]
        assert old_rows[0]["score"] == "5"

    def test_migration_is_idempotent(self, tmp_data):
        path = tmp_data / "data.csv"
        _write_csv(path, ["id", "name", "score"], [{"id": "1", "name": "alice", "score": "5"}])

        first = migrations.migrate_csv(path, _Current)
        second = migrations.migrate_csv(path, _Current)

        assert first is not None
        assert second is None  # no drift after first run

    def test_history_recorded(self, tmp_data):
        path = tmp_data / "data.csv"
        _write_csv(path, ["id", "name", "score"], [{"id": "1", "name": "alice", "score": "5"}])

        migrations.migrate_csv(path, _Current)
        history = migrations.list_migration_history()
        assert len(history) == 1
        assert history[0]["model"] == "_Current"
        assert history[0]["added"] == ["tags", "enabled", "note"]
        assert history[0]["removed"] == ["score"]

    def test_reorder_only_still_rewrites(self, tmp_data):
        path = tmp_data / "data.csv"
        # Same columns, different order
        _write_csv(path, ["name", "id", "score"], [{"name": "a", "id": "1", "score": "5"}])
        result = migrations.migrate_csv(path, _Legacy)
        assert result is not None
        header, _ = _read_csv(path)
        assert header == ["id", "name", "score"]


class TestRecordCurrentSchema:
    def test_records_baseline(self, tmp_data):
        migrations.record_current_schema([_Current])
        state = migrations._load_schema_state()
        assert state["models"]["_Current"] == ["id", "name", "tags", "enabled", "note"]

    def test_is_idempotent(self, tmp_data):
        migrations.record_current_schema([_Current])
        migrations.record_current_schema([_Current])
        state = migrations._load_schema_state()
        # No history entries — record_current_schema never logs
        assert state.get("history", []) == []
