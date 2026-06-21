"""Tests for the shared first-run setup helpers (``auto_applier.setup_ops``).

The model pull streams Ollama's HTTP ``/api/pull`` NDJSON (faked here as a scripted line
iterator); the browser install shells the playwright installer (faked subprocess). Both are
exercised without any network or real download.
"""

from __future__ import annotations

import json
import types

import httpx

from auto_applier import setup_ops
from auto_applier.config import Settings
from auto_applier.doctor import CheckResult, Status


class _FakeStream:
    """Stand-in for the context manager returned by ``httpx.stream``."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


# --------------------------------------------------------------- pull_models

def test_pull_models_happy_path(settings: Settings, monkeypatch):
    script = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "pulling abc", "total": 100, "completed": 50}),
        json.dumps({"status": "pulling abc", "total": 100, "completed": 100}),
        json.dumps({"status": "success"}),
    ]
    monkeypatch.setattr(setup_ops.httpx, "stream",
                        lambda *a, **k: _FakeStream(list(script)))

    seen: list[dict] = []
    result = setup_ops.pull_models(settings, seen.append)

    assert result.ok is True
    assert result.failed == []
    # Both configured models were pulled.
    assert result.models == [settings.llm.ollama_model, settings.llm.embed_model]
    percents = [f["percent"] for f in seen if "percent" in f]
    assert 50 in percents and 100 in percents


def test_pull_models_server_down(settings: Settings, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(setup_ops.httpx, "stream", boom)
    result = setup_ops.pull_models(settings)
    assert result.ok is False
    assert result.error == "ollama_not_running"


def test_pull_models_error_line_marks_failed(settings: Settings, monkeypatch):
    script = [json.dumps({"error": "file does not exist"})]
    monkeypatch.setattr(setup_ops.httpx, "stream",
                        lambda *a, **k: _FakeStream(list(script)))
    result = setup_ops.pull_models(settings)
    assert result.ok is False
    # The completion model (first) is recorded as failed.
    assert settings.llm.ollama_model in result.failed


# --------------------------------------------------------------- install_browser

def _fake_proc(returncode: int, stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")


def test_install_browser_success(monkeypatch):
    monkeypatch.setattr(setup_ops.subprocess, "run", lambda *a, **k: _fake_proc(0))
    result = setup_ops.install_browser()
    assert result.ok is True
    assert result.backend_used == "patchright"


def test_install_browser_falls_back_to_playwright(monkeypatch):
    calls: list[str] = []

    def fake_run(argv, **k):
        pkg = argv[2]  # [python, -m, <pkg>, install, chromium]
        calls.append(pkg)
        return _fake_proc(0 if pkg == "playwright" else 1, stderr="nope")

    monkeypatch.setattr(setup_ops.subprocess, "run", fake_run)
    result = setup_ops.install_browser()
    assert result.ok is True
    assert result.backend_used == "playwright"
    assert calls == ["patchright", "playwright"]


def test_install_browser_both_fail(monkeypatch):
    monkeypatch.setattr(setup_ops.subprocess, "run",
                        lambda *a, **k: _fake_proc(1, stderr="boom"))
    result = setup_ops.install_browser()
    assert result.ok is False
    assert "boom" in result.error


# --------------------------------------------------------------- readiness / dirs

def test_readiness_returns_llm_and_browser(settings: Settings, monkeypatch):
    monkeypatch.setattr(setup_ops, "check_llm",
                        lambda s: CheckResult("llm", Status.PASS, "ok"))
    monkeypatch.setattr(setup_ops, "check_browser",
                        lambda s: CheckResult("browser", Status.WARN, "no browser"))
    checks = setup_ops.readiness(settings)
    assert [c.name for c in checks] == ["llm", "browser"]


def test_ensure_data_dirs_creates_all(settings: Settings):
    setup_ops.ensure_data_dirs(settings)
    assert settings.data_dir.exists()
    assert settings.artifacts_dir.exists()
    assert settings.backups_dir.exists()
