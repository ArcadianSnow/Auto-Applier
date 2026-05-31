"""Phase 4 (6/M) — av3 launch CLI tests.

Coverage:
  * launch_cmd spawns the right child args
  * --no-browser skips webbrowser.open
  * Probe waits + opens browser on success
  * Probe times out + still opens browser (best-effort UX)
  * --host 0.0.0.0 rewrites the probe target to 127.0.0.1
  * Exit code propagates from the child

The child process is faked — we never actually start uvicorn from a
test. The signal-forwarding path (KeyboardInterrupt → child.terminate)
is hard to exercise reliably under pytest, so we cover the happy path
+ the no-browser branch + the host-rewrite branch.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from auto_applier.cli.main import _wait_for_port, cli


class TestPortProbe:

    def test_returns_false_when_nothing_listening(self):
        # Pick a port we expect to be closed; the probe times out and
        # returns False instead of raising.
        ok = _wait_for_port("127.0.0.1", 1, timeout_s=0.05)
        assert ok is False


class TestLaunchCmd:

    def _run(self, args, *, settings, child_returncode=0,
             probe_ok=True, monkeypatch):
        """Run the launch_cmd with a faked child process + faked browser."""
        monkeypatch.setenv("AV3_DATA_DIR", str(settings.data_dir))

        fake_child = MagicMock()
        fake_child.wait.return_value = child_returncode
        opened_urls: list[str] = []
        spawned_args: list[list[str]] = []

        def _fake_popen(args, **kwargs):
            spawned_args.append(args)
            return fake_child

        def _fake_open(url):
            opened_urls.append(url)
            return True

        def _fake_wait_for_port(*_a, **_kw):
            return probe_ok

        with patch("subprocess.Popen", _fake_popen), \
             patch("webbrowser.open", _fake_open), \
             patch("auto_applier.cli.main._wait_for_port", _fake_wait_for_port):
            runner = CliRunner()
            result = runner.invoke(cli, ["launch"] + args)
        return result, spawned_args, opened_urls, fake_child

    def test_spawns_serve_with_default_host_port(
        self, settings, monkeypatch
    ):
        result, spawned, opened, child = self._run(
            ["--port", "9001"], settings=settings, monkeypatch=monkeypatch,
        )
        # Exit code reflects the child's.
        assert result.exit_code == 0
        # Exactly one spawn — the serve subprocess.
        assert len(spawned) == 1
        args = spawned[0]
        # python -m auto_applier.cli.main serve --host 127.0.0.1 --port 9001
        assert args[0] == sys.executable
        assert args[1:4] == ["-m", "auto_applier.cli.main", "serve"]
        assert "--host" in args
        assert "--port" in args
        assert "9001" in args
        # Browser was opened to the dashboard URL.
        assert any("9001" in u for u in opened)

    def test_no_browser_skips_open(self, settings, monkeypatch):
        result, _spawned, opened, _child = self._run(
            ["--no-browser", "--port", "9002"],
            settings=settings, monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0
        assert opened == []

    def test_host_0_0_0_0_rewrites_probe_to_localhost(
        self, settings, monkeypatch
    ):
        # When the user binds to 0.0.0.0, the dashboard URL must point at
        # 127.0.0.1 — opening 0.0.0.0:port in a browser would either
        # fail outright or take the user through the LAN gateway.
        result, _spawned, opened, _child = self._run(
            ["--host", "0.0.0.0", "--port", "9003"],
            settings=settings, monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0
        # Opened URL is on 127.0.0.1, NOT 0.0.0.0.
        assert any(u.startswith("http://127.0.0.1:9003") for u in opened)
        assert not any("0.0.0.0" in u for u in opened)

    def test_probe_timeout_still_opens_browser(
        self, settings, monkeypatch
    ):
        # The probe gave up; the launcher still opens the URL so the user
        # sees the connection error in the tab rather than the launcher
        # silently hanging.
        result, _spawned, opened, _child = self._run(
            ["--port", "9004", "--probe-timeout-s", "0.05"],
            settings=settings, monkeypatch=monkeypatch, probe_ok=False,
        )
        assert result.exit_code == 0
        assert len(opened) == 1

    def test_child_nonzero_exit_propagates(
        self, settings, monkeypatch
    ):
        result, _spawned, _opened, _child = self._run(
            ["--port", "9005"],
            settings=settings, monkeypatch=monkeypatch, child_returncode=42,
        )
        assert result.exit_code == 42
