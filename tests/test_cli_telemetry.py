"""Contract tests for ``av3 telemetry on|off|status`` + ``av3 export-diagnostics``
(Phase 5 3/M, spec §9).

Covers the opt-in toggle (config persistence + the §9 disclosure + handle
hashing), the status read-out (mirror-queue summary), and the diagnostics
tarball (scrubbed-by-default vs --raw, always-stripped secrets, the
"answer value never leaves" guarantee carried into the support bundle).

What is intentionally NOT re-tested here (owned elsewhere):
  * scrubber field semantics — ``test_mirror_queue.py``.
  * ``cli errors|stats`` — ``test_cli_observability.py``.
  * the relay client / drainer — Phase 5 (4/M), ``test_mirror_client.py``.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.config import load_settings
from auto_applier.telemetry import (
    EventSink,
    attach_mirror_from_settings,
    reset_sink,
    user_id_from_handle,
)


@pytest.fixture(autouse=True)
def _isolate_sink():
    reset_sink()
    yield
    reset_sink()


def _run(monkeypatch, data_dir: Path, *args: str, input: str | None = None):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    runner = CliRunner()
    return runner.invoke(cli, list(args), input=input)


def _read_config(data_dir: Path) -> dict:
    p = data_dir / "user_config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ----------------------------------------------------------------- telemetry on

def test_telemetry_on_with_handle_flag_persists_enabled(monkeypatch, data_dir):
    res = _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Jordan")
    assert res.exit_code == 0, res.output
    cfg = _read_config(data_dir)
    assert cfg["telemetry"]["enabled"] is True
    assert cfg["telemetry"]["handle"] == "Jordan"
    # The displayed user_id is the sha256[:10] pseudonym, not the raw name.
    assert user_id_from_handle("Jordan") in res.output
    assert "Jordan" not in res.output.split("user_id=")[1] if "user_id=" in res.output else True


def test_telemetry_on_prompts_when_no_handle(monkeypatch, data_dir):
    # No --handle and none stored → the §9 disclosure prints and we're prompted.
    res = _run(monkeypatch, data_dir, "telemetry", "on", input="Sam\n")
    assert res.exit_code == 0, res.output
    assert "what leaves your machine" in res.output
    # The disclosure must spell out the "never the answer value" guarantee.
    assert "NEVER the answer value" in res.output
    cfg = _read_config(data_dir)
    assert cfg["telemetry"]["handle"] == "Sam"
    assert cfg["telemetry"]["enabled"] is True


def test_telemetry_on_reuses_stored_handle_without_prompt(monkeypatch, data_dir):
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Alex")
    # Turning off then on again must NOT need a new handle (it's stored).
    _run(monkeypatch, data_dir, "telemetry", "off")
    res = _run(monkeypatch, data_dir, "telemetry", "on")  # no input provided
    assert res.exit_code == 0, res.output
    assert "what leaves your machine" not in res.output  # no disclosure/prompt
    cfg = _read_config(data_dir)
    assert cfg["telemetry"]["enabled"] is True
    assert cfg["telemetry"]["handle"] == "Alex"


def test_telemetry_on_sets_relay_url(monkeypatch, data_dir):
    res = _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Kai",
               "--relay-url", "https://relay.example.workers.dev")
    assert res.exit_code == 0, res.output
    cfg = _read_config(data_dir)
    assert cfg["telemetry"]["relay_url"] == "https://relay.example.workers.dev"


# ----------------------------------------------------------------- telemetry off

def test_telemetry_off_disables_but_keeps_handle(monkeypatch, data_dir):
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Robin")
    res = _run(monkeypatch, data_dir, "telemetry", "off")
    assert res.exit_code == 0, res.output
    cfg = _read_config(data_dir)
    assert cfg["telemetry"]["enabled"] is False
    assert cfg["telemetry"]["handle"] == "Robin"  # handle survives


def test_telemetry_off_warns_about_pending_rows(monkeypatch, data_dir):
    # Opt in, then simulate a worker enqueuing one scrubbed error row.
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Pat")
    settings = load_settings(data_dir)
    sink = EventSink(settings.events_db_path)
    attach_mirror_from_settings(sink, settings)
    sink.emit(stage="apply", status="error", error_type="TimeoutError",
              error_msg="boom")
    sink.close()

    res = _run(monkeypatch, data_dir, "telemetry", "off")
    assert "1 scrubbed row(s) remain queued" in res.output


# ----------------------------------------------------------------- status

def test_telemetry_status_default_disabled(monkeypatch, data_dir):
    res = _run(monkeypatch, data_dir, "telemetry", "status")
    assert res.exit_code == 0, res.output
    assert "enabled:    False" in res.output
    assert "anonymous" in res.output  # no handle yet
    assert "pending:        0" in res.output


def test_telemetry_status_shows_user_id_and_pending(monkeypatch, data_dir):
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Morgan")
    settings = load_settings(data_dir)
    sink = EventSink(settings.events_db_path)
    attach_mirror_from_settings(sink, settings)
    sink.emit(stage="score", status="error", error_type="ValueError",
              error_msg="bad")
    sink.close()

    res = _run(monkeypatch, data_dir, "telemetry", "status")
    assert user_id_from_handle("Morgan") in res.output
    assert "pending:        1" in res.output


# ----------------------------------------------------------------- diagnostics

def _seed_events(data_dir: Path):
    settings = load_settings(data_dir)
    sink = EventSink(settings.events_db_path)
    # An error row carrying PII in the message + a path.
    sink.emit(
        stage="apply", status="error", platform="greenhouse",
        error_type="TimeoutError",
        error_msg="failed for jane@example.com at C:\\Users\\jane\\resume.pdf",
    )
    # An inferred-answer row whose context carries the ANSWER VALUE (must never
    # leave) + a non-eeo category.
    sink.emit(
        stage="resolver_inferred", status="ok",
        context={
            "question": "Are you authorized to work in the US?",
            "category": "work_authorization",
            "confidence": 0.9,
            "outcome": "answered",
            "answer": "Yes, US citizen",  # the value §9 forbids mirroring
        },
    )
    # An EEO inferred row — must drop entirely from the bundle (§8d).
    sink.emit(
        stage="resolver_inferred", status="ok",
        context={
            "question": "What is your gender?",
            "category": "eeo",
            "confidence": 0.5,
            "outcome": "answered",
            "answer": "redacted",
        },
    )
    sink.close()


def _extract(tar_path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            f = tar.extractfile(m)
            out[m.name] = f.read() if f else b""
    return out


def test_export_diagnostics_scrubbed_default(monkeypatch, data_dir):
    _seed_events(data_dir)
    res = _run(monkeypatch, data_dir, "export-diagnostics")
    assert res.exit_code == 0, res.output
    assert "mode=scrubbed" in res.output

    tarball = next(data_dir.glob("diagnostics-*.tar.gz"))
    members = _extract(tarball)
    assert "manifest.json" in members
    assert "settings.json" in members
    assert "doctor.txt" in members
    assert "events_errors.json" in members
    assert "events_inferred.json" in members
    assert "events_stats.json" in members
    assert "mirror_status.json" in members
    assert "events.db" not in members  # raw-only

    # Error message is scrubbed: no email, no path.
    errors_blob = members["events_errors.json"].decode()
    assert "jane@example.com" not in errors_blob
    assert "C:\\Users\\jane" not in errors_blob
    assert "[email]" in errors_blob and "[path]" in errors_blob

    # The inferred-answer value never appears anywhere in the bundle.
    inferred_blob = members["events_inferred.json"].decode()
    assert "US citizen" not in inferred_blob
    assert "answer" not in json.loads(inferred_blob)[0]
    # EEO row dropped → only the work_authorization row survives.
    rows = json.loads(inferred_blob)
    assert all(r.get("category") != "eeo" for r in rows)
    assert "redacted" not in inferred_blob


def test_export_diagnostics_strips_secrets(monkeypatch, data_dir):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    # Stash a handle so telemetry.handle is present to strip.
    _run(monkeypatch, data_dir, "telemetry", "on", "--handle", "Casey")
    _seed_events(data_dir)
    # GEMINI_API_KEY must be in env for THIS invoke too (set by _run? no — _run
    # deletes it). Re-set after the helper clears it by invoking directly.
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    runner = CliRunner()
    res = runner.invoke(cli, ["export-diagnostics"])
    assert res.exit_code == 0, res.output

    tarball = next(data_dir.glob("diagnostics-*.tar.gz"))
    members = _extract(tarball)
    settings_blob = members["settings.json"].decode()
    assert "super-secret-key" not in settings_blob
    assert "Casey" not in settings_blob  # raw handle stripped from settings dump
    # The manifest carries the hashed user_id, which is safe.
    manifest = json.loads(members["manifest.json"].decode())
    assert manifest["user_id"] == user_id_from_handle("Casey")


def test_export_diagnostics_raw_includes_db_and_unscrubbed(monkeypatch, data_dir):
    _seed_events(data_dir)
    res = _run(monkeypatch, data_dir, "export-diagnostics", "--raw")
    assert res.exit_code == 0, res.output
    assert "mode=raw" in res.output
    assert "PII-bearing" in res.output or "un-scrubbed" in res.output

    tarball = next(data_dir.glob("diagnostics-*.tar.gz"))
    members = _extract(tarball)
    assert "events.db" in members  # verbatim copy included
    # Raw error rows keep the original message (PII present — that's the point).
    errors_blob = members["events_errors.json"].decode()
    assert "jane@example.com" in errors_blob

    # EVEN IN RAW MODE the inferred-answer value never leaves (§9 hard line).
    inferred_blob = members["events_inferred.json"].decode()
    assert "US citizen" not in inferred_blob
