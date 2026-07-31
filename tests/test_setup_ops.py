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


def _pkg_of(argv) -> str:
    """Which backend a command belongs to.

    The command is now the node driver (``[<pkg>/driver/node.exe, cli.js, install, chromium]``)
    rather than ``[python, -m, <pkg>, …]`` — see ``setup_ops._install_cmd``: the ``-m`` form is
    wrong in a PyInstaller build, where ``sys.executable`` is the app itself. Identify the
    backend by the package name in the driver path instead of a positional index, so this
    doesn't re-break the next time the invocation changes shape.
    """
    joined = " ".join(str(a) for a in argv)
    return "playwright" if "patchright" not in joined else "patchright"


def test_install_browser_falls_back_to_playwright(monkeypatch):
    calls: list[str] = []

    def fake_run(argv, **k):
        pkg = _pkg_of(argv)
        calls.append(pkg)
        return _fake_proc(0 if pkg == "playwright" else 1, stderr="nope")

    monkeypatch.setattr(setup_ops.subprocess, "run", fake_run)
    result = setup_ops.install_browser()
    assert result.ok is True
    assert result.backend_used == "playwright"
    assert calls == ["patchright", "playwright"]


def test_install_cmd_drives_the_node_driver_not_dash_m():
    """``python -m patchright install`` cannot work in a frozen build (``sys.executable`` is
    the bundled app), so the install must go through the node driver — the same thing
    ``patchright/__main__.py`` does internally."""
    cmd = setup_ops._install_cmd("patchright")
    assert cmd is not None
    assert "-m" not in cmd
    assert cmd[-2:] == ["install", "chromium"]
    assert "patchright" in " ".join(cmd)
    assert cmd[0].lower().endswith("node.exe") or "node" in cmd[0].lower()


def _break_driver_import(monkeypatch):
    def boom(name):
        raise ImportError(f"no {name}")

    monkeypatch.setattr(setup_ops, "import_module", boom)


def test_install_cmd_frozen_refuses_the_unusable_dash_m_fallback(monkeypatch):
    """If the driver API ever disappears, a FROZEN build must report "can't" rather than
    silently running a command that cannot possibly succeed."""
    _break_driver_import(monkeypatch)
    monkeypatch.setattr(setup_ops.sys, "frozen", True, raising=False)
    assert setup_ops._install_cmd("patchright") is None


def test_install_cmd_unfrozen_still_falls_back_to_dash_m(monkeypatch):
    """A pip install can legitimately use the module form, so keep it as the fallback there."""
    _break_driver_import(monkeypatch)
    monkeypatch.delattr(setup_ops.sys, "frozen", raising=False)
    cmd = setup_ops._install_cmd("patchright")
    assert cmd is not None and cmd[1:] == ["-m", "patchright", "install", "chromium"]


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
