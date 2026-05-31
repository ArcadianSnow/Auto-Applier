"""Shared fixtures for v3 tests. Everything points at a tmp data dir — no real I/O escapes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from auto_applier.config import Settings, load_settings
from auto_applier.db import init_app_db
from auto_applier.telemetry import EventSink, configure_sink, reset_sink


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "v3data"
    d.mkdir()
    return d


@pytest.fixture
def settings(data_dir: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return load_settings()


@pytest.fixture
def conn(settings: Settings) -> sqlite3.Connection:
    c = init_app_db(settings.app_db_path)
    yield c
    c.close()


@pytest.fixture
def sink(settings: Settings):
    s = configure_sink(EventSink(settings.events_db_path))
    yield s
    reset_sink()
