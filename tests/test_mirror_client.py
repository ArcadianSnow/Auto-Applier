"""Tests for the telemetry relay client + drainer + doctor relay check
(Phase 5 4/M, spec §9).

The relay itself (relay/worker.js) is owner-hosted infra and not exercised here;
the client side is tested with a mocked POST transport so no network is touched.
``doctor.check_relay_reachable`` is tested with httpx.get monkeypatched.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.config import load_settings
from auto_applier.doctor import Status, check_relay_reachable
from auto_applier.telemetry import EventSink, reset_sink
from auto_applier.telemetry.client import DrainResult, MirrorClient, _ingest_url


@pytest.fixture(autouse=True)
def _isolate_sink():
    reset_sink()
    yield
    reset_sink()


def _queue_with(events_db: Path, n_error: int = 0, n_inferred: int = 0):
    """Return an open EventSink whose mirror_queue holds n scrubbed rows."""
    sink = EventSink(events_db)
    for i in range(n_error):
        sink.mirror_queue.enqueue(
            "error",
            {"user_id": "abc123", "app_version": "3.0.0a0", "stage": "apply",
             "error_type": "TimeoutError", "error_msg": f"boom {i}", "ts": "2026-05-29T00:00:00+00:00"},
        )
    for i in range(n_inferred):
        sink.mirror_queue.enqueue(
            "inferred_answer",
            {"user_id": "abc123", "question_text": f"q{i}", "category": "work_authorization",
             "confidence": 0.9, "outcome": "answered", "ts": "2026-05-29T00:00:00+00:00"},
        )
    return sink


# ----------------------------------------------------------------- _ingest_url

def test_ingest_url_appends_path():
    assert _ingest_url("https://relay.example.com") == "https://relay.example.com/ingest"
    assert _ingest_url("https://relay.example.com/") == "https://relay.example.com/ingest"


# ----------------------------------------------------------------- drain

def test_drain_delivers_all_on_2xx(settings):
    sink = _queue_with(settings.events_db_path, n_error=3)
    try:
        calls = []
        def post(url, body):
            calls.append((url, body))
            return 202
        client = MirrorClient(sink.mirror_queue, "https://r.example", post=post)
        result = client.drain()
        assert result == DrainResult(attempted=3, delivered=3, failed=0)
        assert result.all_delivered
        assert sink.mirror_queue.pending_count() == 0
        # All POSTs hit the /ingest endpoint with the {category,payload,schema} shape.
        assert all(u.endswith("/ingest") for u, _ in calls)
        assert calls[0][1]["category"] == "error"
        assert calls[0][1]["schema"] == 1
    finally:
        sink.close()


def test_drain_marks_failed_on_http_error(settings):
    sink = _queue_with(settings.events_db_path, n_error=2)
    try:
        client = MirrorClient(sink.mirror_queue, "https://r.example", post=lambda u, b: 500)
        result = client.drain()
        assert result == DrainResult(attempted=2, delivered=0, failed=2)
        assert not result.all_delivered
        # Rows stay pending (retry later) and their attempts/last_error were bumped.
        assert sink.mirror_queue.pending_count() == 2
        s = sink.mirror_queue.summary()
        assert "HTTP 500" in s["last_error"]
        assert s["last_error_attempts"] == 1
    finally:
        sink.close()


def test_drain_marks_failed_on_transport_exception(settings):
    sink = _queue_with(settings.events_db_path, n_error=1)
    try:
        def boom(u, b):
            raise httpx.ConnectError("no route to host")
        client = MirrorClient(sink.mirror_queue, "https://r.example", post=boom)
        result = client.drain()
        assert result.failed == 1 and result.delivered == 0
        assert "ConnectError" in sink.mirror_queue.summary()["last_error"]
    finally:
        sink.close()


def test_drain_respects_limit(settings):
    sink = _queue_with(settings.events_db_path, n_error=5)
    try:
        client = MirrorClient(sink.mirror_queue, "https://r.example", post=lambda u, b: 202)
        result = client.drain(limit=2)
        assert result.attempted == 2
        assert sink.mirror_queue.pending_count() == 3
    finally:
        sink.close()


def test_drain_per_row_isolation(settings):
    """One row's failure must not abort delivery of the others in the pass."""
    sink = _queue_with(settings.events_db_path, n_error=3)
    try:
        seen = {"n": 0}
        def flaky(u, b):
            seen["n"] += 1
            if seen["n"] == 2:
                raise httpx.ReadTimeout("slow")
            return 202
        client = MirrorClient(sink.mirror_queue, "https://r.example", post=flaky)
        result = client.drain()
        assert result.delivered == 2 and result.failed == 1
    finally:
        sink.close()


# ----------------------------------------------------------------- doctor relay check

def test_relay_check_pass_when_telemetry_off(settings):
    # Default settings → telemetry disabled.
    r = check_relay_reachable(settings)
    assert r.status is Status.PASS
    assert "off" in r.detail


def test_relay_check_warn_when_on_without_url(settings):
    settings.telemetry.enabled = True
    settings.telemetry.relay_url = None
    r = check_relay_reachable(settings)
    assert r.status is Status.WARN
    assert "no relay_url" in r.detail


def test_relay_check_pass_when_healthy(settings, monkeypatch):
    settings.telemetry.enabled = True
    settings.telemetry.relay_url = "https://relay.example.com"

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp())
    r = check_relay_reachable(settings)
    assert r.status is Status.PASS
    assert "/health" in r.detail


def test_relay_check_warn_when_unreachable(settings, monkeypatch):
    settings.telemetry.enabled = True
    settings.telemetry.relay_url = "https://relay.example.com"

    def boom(url, timeout=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    r = check_relay_reachable(settings)
    assert r.status is Status.WARN
    assert "unreachable" in r.detail


# ----------------------------------------------------------------- cli mirror drain

def _run(monkeypatch, data_dir: Path, *args: str):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return CliRunner().invoke(cli, list(args))


def test_mirror_drain_noop_when_disabled(monkeypatch, data_dir):
    res = _run(monkeypatch, data_dir, "mirror", "drain")
    assert res.exit_code == 0
    assert "telemetry disabled" in res.output


def test_mirror_drain_fails_without_relay_url(monkeypatch, data_dir):
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Lee")  # enabled, no relay
    res = _run(monkeypatch, data_dir, "mirror", "drain")
    assert res.exit_code == 2
    assert "no relay_url" in res.output


def test_mirror_drain_happy_path(monkeypatch, data_dir):
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Lee",
         "--relay-url", "https://relay.example.com")
    # Enqueue one scrubbed row as a worker would.
    settings = load_settings(data_dir)
    sink = EventSink(settings.events_db_path)
    from auto_applier.telemetry import attach_mirror_from_settings
    attach_mirror_from_settings(sink, settings)
    sink.emit(stage="apply", status="error", error_type="ValueError", error_msg="x")
    sink.close()

    class _Resp:
        status_code = 202

    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: _Resp())
    res = _run(monkeypatch, data_dir, "mirror", "drain")
    assert res.exit_code == 0, res.output
    assert "delivered=1" in res.output
    assert "still_pending=0" in res.output
