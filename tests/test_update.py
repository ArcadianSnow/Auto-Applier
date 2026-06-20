"""Tests for the auto-update feed check + the install-browser CLI
(Phase 5 5/M, spec §11a).

The version-compare + feed-parse logic is pure and fully tested here; the
GitHub fetch is injected (``http_get``) so no network is touched. The
``av3 install-browser`` command is tested with subprocess monkeypatched — we
never actually download Chromium in CI. The PyInstaller build (``build_v3.py``)
is not unit-tested (it shells out to a multi-minute native build).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import auto_applier.cli.main as cli_main
from auto_applier.cli.main import cli
from auto_applier.update import (
    DEFAULT_REPO,
    UpdateInfo,
    check_for_update,
    compare_versions,
    parse_release_feed,
)


# ----------------------------------------------------------------- compare_versions

@pytest.mark.parametrize("cur,latest,expected", [
    ("3.0.0a0", "3.0.0a1", True),
    ("3.0.0a0", "3.0.0", True),
    ("3.0.0", "3.0.0", False),
    ("3.0.1", "3.0.0", False),
    ("3.0.0a0", "v3.0.0a1", True),   # leading 'v' stripped
    ("3.0.0", "v2.9.9", False),
    ("3.0.0", "garbage", False),     # unparseable → not newer (fail safe)
])
def test_compare_versions(cur, latest, expected):
    assert compare_versions(cur, latest) is expected


# ----------------------------------------------------------------- parse_release_feed

def test_parse_feed_dict_newer():
    payload = {"tag_name": "v3.1.0", "html_url": "https://gh/r/1", "prerelease": False}
    info = parse_release_feed(payload, "3.0.0a0")
    assert info == UpdateInfo(current="3.0.0a0", latest="3.1.0",
                              url="https://gh/r/1", is_newer=True)


def test_parse_feed_list_picks_newest_nondraft():
    payload = [
        {"tag_name": "v3.0.0a1", "html_url": "u1", "prerelease": True},
        {"tag_name": "v3.0.0a3", "html_url": "u3", "prerelease": True},
        {"tag_name": "v3.0.0a2", "html_url": "u2", "prerelease": True},
        {"tag_name": "v9.9.9", "html_url": "ud", "draft": True},  # draft ignored
    ]
    info = parse_release_feed(payload, "3.0.0a0")
    assert info.latest == "3.0.0a3"
    assert info.url == "u3"
    assert info.is_newer


def test_parse_feed_skips_prerelease_when_disallowed():
    payload = [
        {"tag_name": "v3.0.0a5", "html_url": "u5", "prerelease": True},
        {"tag_name": "v2.9.0", "html_url": "u2", "prerelease": False},
    ]
    info = parse_release_feed(payload, "3.0.0a0", allow_prerelease=False)
    # Only the stable 2.9.0 is considered → not newer than 3.0.0a0.
    assert info.latest == "2.9.0"
    assert info.is_newer is False


def test_parse_feed_empty_returns_none():
    assert parse_release_feed([], "3.0.0a0") is None
    assert parse_release_feed([{"draft": True, "tag_name": "v9"}], "3.0.0a0") is None
    assert parse_release_feed({"prerelease": True}, "3.0.0a0", allow_prerelease=False) is None


# ----------------------------------------------------------------- check_for_update

def test_check_for_update_newer():
    def fake_get(url):
        assert DEFAULT_REPO in url
        return 200, [{"tag_name": "v3.5.0", "html_url": "u", "prerelease": False}]
    info = check_for_update("3.0.0a0", http_get=fake_get)
    assert info.is_newer and info.latest == "3.5.0"


def test_check_for_update_http_error_returns_none():
    assert check_for_update("3.0.0a0", http_get=lambda u: (403, None)) is None


def test_check_for_update_transport_exception_returns_none():
    def boom(url):
        raise RuntimeError("offline")
    # Must NEVER raise — a missed check is not an error.
    assert check_for_update("3.0.0a0", http_get=boom) is None


# ----------------------------------------------------------------- av3 update CLI

def _run(*args: str):
    return CliRunner().invoke(cli, list(args))


def test_update_cmd_reports_available(monkeypatch):
    monkeypatch.setattr(
        cli_main, "__version__", "3.0.0a0", raising=False,
    )
    import auto_applier.update as upd
    monkeypatch.setattr(
        upd, "_httpx_get",
        lambda timeout_s: (lambda url: (200, [{"tag_name": "v3.9.0",
                                               "html_url": "https://gh/rel",
                                               "prerelease": False}])),
    )
    res = _run("update")
    assert res.exit_code == 0, res.output
    assert "Update available" in res.output
    assert "3.9.0" in res.output


def test_update_cmd_exit_code_flag(monkeypatch):
    import auto_applier.update as upd
    monkeypatch.setattr(
        upd, "_httpx_get",
        lambda timeout_s: (lambda url: (200, [{"tag_name": "v99.0.0",
                                               "html_url": "u", "prerelease": False}])),
    )
    res = _run("update", "--exit-code")
    assert res.exit_code == 10


def test_update_cmd_offline_is_not_an_error(monkeypatch):
    import auto_applier.update as upd
    monkeypatch.setattr(
        upd, "_httpx_get",
        lambda timeout_s: (lambda url: (_ for _ in ()).throw(RuntimeError("offline"))),
    )
    res = _run("update")
    assert res.exit_code == 0
    assert "Could not reach" in res.output


# ----------------------------------------------------------------- install-browser CLI

class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_install_browser_success_first_backend(monkeypatch):
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        return _Proc(0)
    monkeypatch.setattr("subprocess.run", fake_run)
    res = _run("install-browser")
    assert res.exit_code == 0, res.output
    assert "installed via patchright" in res.output
    assert calls[0][2] == "patchright"  # [python, -m, patchright, install, chromium]


def test_install_browser_falls_back_to_playwright(monkeypatch):
    seq = iter([_Proc(1, "patchright boom"), _Proc(0)])
    monkeypatch.setattr("subprocess.run", lambda args, **kw: next(seq))
    res = _run("install-browser")
    assert res.exit_code == 0, res.output
    assert "installed via playwright" in res.output


def test_install_browser_all_fail_exits_1(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda args, **kw: _Proc(1, "nope"))
    res = _run("install-browser")
    assert res.exit_code == 1
    assert "could not install Chromium" in res.output


# ----------------------------------------------------------------- setup-llm CLI

def test_setup_llm_pulls_both_models(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        return _Proc(0)
    monkeypatch.setattr("subprocess.run", fake_run)
    res = _run("setup-llm")
    assert res.exit_code == 0, res.output
    pulled = [a[2] for a in calls]  # [ollama, pull, <model>]
    assert "gemma4:e4b" in pulled
    assert "nomic-embed-text" in pulled
    assert "All required models are installed" in res.output


def test_setup_llm_missing_ollama_exits_1_with_manual_commands(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    # subprocess.run must NOT be called when ollama is absent.
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: pytest.fail("should not pull without ollama"))
    res = _run("setup-llm")
    assert res.exit_code == 1
    assert "Ollama is not installed" in res.output
    assert "ollama pull gemma4:e4b" in res.output
    assert "ollama pull nomic-embed-text" in res.output


def test_setup_llm_pull_failure_exits_1(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("subprocess.run", lambda args, **kw: _Proc(1, "boom"))
    res = _run("setup-llm")
    assert res.exit_code == 1
    assert "could not pull" in res.output
