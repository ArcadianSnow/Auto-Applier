"""Tests for the in-app first-run setup endpoints + background jobs (spec §11a).

Mirrors ``test_web_onboarding.py::TestSeedBoardsBackground``: the heavy helpers
(``setup_ops.pull_models`` / ``install_browser``) are faked, and we drive the
``/api/setup`` start+status routes through a TestClient plus the worker jobs directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from auto_applier import setup_ops
from auto_applier.config import Settings
from auto_applier.web import WebState, create_app


@pytest.fixture
def web_state(settings: Settings, conn: sqlite3.Connection) -> WebState:
    return WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )


def _make_client(web_state: WebState) -> TestClient:
    return TestClient(create_app(state=web_state, service=None))


def _reset(action: str) -> None:
    from auto_applier.web import routes as R
    R._SETUP[action] = {"status": "idle"}


# --------------------------------------------------------------- readiness endpoint

def test_readiness_endpoint(web_state, monkeypatch):
    from auto_applier.doctor import CheckResult, Status
    monkeypatch.setattr(setup_ops, "readiness", lambda s: [
        CheckResult("llm", Status.WARN, "models not pulled", fix="run setup-llm"),
        CheckResult("browser", Status.PASS, "real Chrome found"),
    ])
    client = _make_client(web_state)
    r = client.get("/api/setup/readiness")
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert [c["name"] for c in checks] == ["llm", "browser"]
    assert checks[0]["status"] == "WARN" and checks[0]["fix"] == "run setup-llm"


# --------------------------------------------------------------- worker jobs

def test_run_pull_models_job_marks_done(settings: Settings, monkeypatch):
    from auto_applier.web import routes as R
    from auto_applier.setup_ops import PullResult

    def fake_pull(s, progress_cb=None):
        if progress_cb:
            progress_cb({"status": "running", "percent": 100, "phase": "success"})
        return PullResult(ok=True, models=["a", "b"])

    monkeypatch.setattr(setup_ops, "pull_models", fake_pull)
    _reset("pull-models")
    asyncio.run(R._run_pull_models_job(settings))
    assert R._SETUP["pull-models"]["status"] == "done"


def test_run_pull_models_job_marks_error(settings: Settings, monkeypatch):
    from auto_applier.web import routes as R
    from auto_applier.setup_ops import PullResult

    monkeypatch.setattr(setup_ops, "pull_models",
                        lambda s, progress_cb=None: PullResult(ok=False, error="ollama_not_running"))
    _reset("pull-models")
    asyncio.run(R._run_pull_models_job(settings))
    assert R._SETUP["pull-models"]["status"] == "error"
    assert R._SETUP["pull-models"]["error"] == "ollama_not_running"


# --------------------------------------------------------------- start/status routes

def test_pull_start_then_status_done(web_state, monkeypatch):
    from auto_applier.setup_ops import PullResult
    monkeypatch.setattr(setup_ops, "pull_models",
                        lambda s, progress_cb=None: PullResult(ok=True, models=["a", "b"]))
    _reset("pull-models")
    client = _make_client(web_state)

    r = client.post("/api/setup/pull-models/start")
    assert r.status_code == 200
    assert r.json()["status"] in ("running", "done")

    final = None
    for _ in range(60):
        s = client.get("/api/setup/pull-models/status").json()
        if s["status"] != "running":
            final = s
            break
        time.sleep(0.05)
    assert final is not None and final["status"] == "done"


def test_install_browser_start_then_status_done(web_state, monkeypatch):
    from auto_applier.setup_ops import InstallResult
    monkeypatch.setattr(setup_ops, "install_browser",
                        lambda progress_cb=None, backend="auto": InstallResult(ok=True, backend_used="patchright"))
    _reset("install-browser")
    client = _make_client(web_state)

    r = client.post("/api/setup/install-browser/start")
    assert r.status_code == 200

    final = None
    for _ in range(60):
        s = client.get("/api/setup/install-browser/status").json()
        if s["status"] != "running":
            final = s
            break
        time.sleep(0.05)
    assert final is not None and final["status"] == "done"


def test_start_idempotent_while_running(web_state, monkeypatch):
    from auto_applier.web import routes as R
    # Simulate an already-running job: a second start must return the in-flight snapshot,
    # not kick off another (and must NOT call pull_models).
    called = {"n": 0}
    monkeypatch.setattr(setup_ops, "pull_models",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    R._SETUP["pull-models"] = {"status": "running", "percent": 42}
    client = _make_client(web_state)
    r = client.post("/api/setup/pull-models/start")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert called["n"] == 0
    _reset("pull-models")


def test_unknown_action_404(web_state):
    client = _make_client(web_state)
    assert client.post("/api/setup/bogus/start").status_code == 404
    assert client.get("/api/setup/bogus/status").status_code == 404
