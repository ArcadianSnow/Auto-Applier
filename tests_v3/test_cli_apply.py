"""Contract tests for ``av3 apply`` (the CLI entry, spec section 7 #7).

These tests stub :class:`BrowserSession` (no real Chrome) and :class:`ApplyWorker`
(behavior already covered by ``test_apply_worker.py``) so the CLI tests stay focused on
the CLI's own contract: argument parsing, pre-flight checks, summary line shape, exit
codes, and that the right constructor knobs reach the worker.

What is intentionally NOT tested here:
  * Worker state transitions, rate limiting, telemetry mirroring — owned by
    ``test_apply_worker.py``.
  * Live driver behavior — owned by ``test_lever_apply.py`` / ``test_greenhouse_apply.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from av3.cli.main import cli
from av3.domain.state import ApplyMode
from av3.pipeline.apply_worker import ApplyRunSummary


# --------------------------------------------------------------- fixtures / helpers

def _seed_minimal_data(data_dir: Path) -> None:
    """Write the two artifacts the apply command pre-flights for."""
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada Lovelace", "email": "ada@example.com",
                        "phone": "555-0100"},
            "work_history": [],
            "education": [],
            "skills": [],
            "certifications": [],
            "allowed_metrics": [],
            "work_authorization": "US citizen",
            "requires_sponsorship": False,
            "eeo": {},
        }),
        encoding="utf-8",
    )
    (data_dir / "artifacts" / "resume.pdf").write_bytes(b"%PDF-1.4 stub\n")


class _RecordingWorker:
    """Captures constructor kwargs for assertion and returns a deterministic summary."""

    last_init: dict | None = None
    next_summary: ApplyRunSummary | None = None

    def __init__(self, **kwargs):
        type(self).last_init = dict(kwargs)
        self._limit_seen: int | None = None

    async def run_once(self, limit=None):
        self._limit_seen = limit
        summary = type(self).next_summary
        if summary is None:
            summary = ApplyRunSummary(run_id="test-run")
        # Stamp the limit into notes so we can assert it threaded through.
        if limit is not None:
            summary.notes = list(summary.notes) + [f"limit_seen={limit}"]
        return summary


class _StubSession:
    """Stands in for BrowserSession — never touches a real browser."""

    started = False
    stopped = False

    def __init__(self, *_args, **_kwargs):
        pass

    async def start(self):
        type(self).started = True

    async def stop(self):
        type(self).stopped = True

    async def new_page(self):
        return None


def _patch(monkeypatch, *, summary: ApplyRunSummary | None = None) -> None:
    """Install the recording worker + stub session + reset class-level state."""
    _RecordingWorker.last_init = None
    _RecordingWorker.next_summary = summary
    _StubSession.started = False
    _StubSession.stopped = False
    # Patch the symbols at the import sites the CLI uses (lazy-imported inside apply()).
    monkeypatch.setattr("av3.pipeline.ApplyWorker", _RecordingWorker)
    monkeypatch.setattr("av3.sources.browser.session.BrowserSession", _StubSession)


def _run(monkeypatch, data_dir: Path, *args: str, summary=None) -> "Result":
    """Drive the CLI command with AV3_DATA_DIR pointing at the test data dir."""
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _patch(monkeypatch, summary=summary)
    runner = CliRunner()
    return runner.invoke(cli, ["apply", *args])


# --------------------------------------------------------------- pre-flight checks

def test_apply_fails_when_fact_bank_missing(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    # No master.json. CLI should fail fast with a fix hint.
    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 2
    assert "fact bank" in result.output
    assert "fix ->" in result.output


def test_apply_fails_when_resume_missing(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    (data_dir / "profile").mkdir()
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({"contact": {"email": "x@y.z"}}), encoding="utf-8"
    )
    # No resume.pdf.
    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 2
    assert "resume" in result.output
    assert "fix ->" in result.output


# --------------------------------------------------------------- summary shape + exit code

def test_apply_emits_summary_line_on_clean_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    summary = ApplyRunSummary(
        run_id="abc123",
        attempted=2,
        applied=1,
        review=1,
        skipped=0,
        errors=0,
        recovered=0,
        dry_run_count=0,
        elapsed_s=12.3,
    )
    result = _run(monkeypatch, data_dir, "--no-dry-run", summary=summary)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "run_id=abc123" in out
    assert "attempted=2" in out
    assert "applied=1" in out
    assert "review=1" in out
    assert "errors=0" in out
    assert "recovered=0" in out
    assert "elapsed=12.3s" in out


def test_apply_exits_nonzero_when_summary_reports_errors(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    summary = ApplyRunSummary(run_id="r", errors=1)
    result = _run(monkeypatch, data_dir, summary=summary)
    assert result.exit_code == 1


def test_apply_prints_notes_block_when_summary_has_notes(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    summary = ApplyRunSummary(
        run_id="r",
        skipped=1,
        notes=["rate-limit skip: Acme (2/2)"],
    )
    result = _run(monkeypatch, data_dir, summary=summary)
    assert result.exit_code == 0
    assert "Notes:" in result.output
    assert "rate-limit skip: Acme (2/2)" in result.output


# --------------------------------------------------------------- flag threading

def test_dry_run_is_default_and_reaches_the_worker(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 0
    init = _RecordingWorker.last_init
    assert init is not None
    assert init["dry_run"] is True


def test_no_dry_run_flag_flips_worker_dry_run_off(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--no-dry-run")
    assert result.exit_code == 0
    assert _RecordingWorker.last_init["dry_run"] is False
    # And a loud confirmation line should fire (the safety reminder).
    assert "real submits" in result.output


def test_mode_flag_maps_to_apply_mode_enum(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--mode", "assisted")
    assert result.exit_code == 0
    assert _RecordingWorker.last_init["mode"] is ApplyMode.BROWSER_ASSISTED

    result = _run(monkeypatch, data_dir, "--mode", "auto")
    assert result.exit_code == 0
    assert _RecordingWorker.last_init["mode"] is ApplyMode.BROWSER_AUTO


def test_source_filter_subsets_the_driver_registry(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--source", "lever")
    assert result.exit_code == 0
    drivers = _RecordingWorker.last_init["drivers"]
    assert set(drivers.keys()) == {"lever"}

    result = _run(monkeypatch, data_dir, "--source", "greenhouse")
    assert result.exit_code == 0
    drivers = _RecordingWorker.last_init["drivers"]
    assert set(drivers.keys()) == {"greenhouse"}


def test_no_source_passes_full_default_registry(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 0
    drivers = _RecordingWorker.last_init["drivers"]
    # Production default exposes lever + greenhouse + ashby (Phase 2 (6/N): Ashby added).
    assert {"lever", "greenhouse", "ashby"}.issubset(set(drivers.keys()))


def test_source_filter_can_target_ashby(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--source", "ashby")
    assert result.exit_code == 0
    drivers = _RecordingWorker.last_init["drivers"]
    assert set(drivers.keys()) == {"ashby"}


def test_limit_threads_into_run_once(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--limit", "3")
    assert result.exit_code == 0
    # The recording worker stamps the limit into notes so we can assert it threaded.
    assert "limit_seen=3" in result.output


def test_no_llm_flag_passes_none_for_both_clients(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir, "--no-llm")
    assert result.exit_code == 0
    init = _RecordingWorker.last_init
    assert init["embed_client"] is None
    assert init["llm_client"] is None


def test_default_wires_both_llm_clients(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 0
    init = _RecordingWorker.last_init
    assert init["embed_client"] is not None
    assert init["llm_client"] is not None


# --------------------------------------------------------------- session lifecycle

def test_session_started_and_stopped_around_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 0
    assert _StubSession.started is True
    assert _StubSession.stopped is True


# --------------------------------------------------------------- fact bank threading

def test_applicant_is_built_from_fact_bank_when_worker_constructs(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3data"
    data_dir.mkdir()
    _seed_minimal_data(data_dir)

    result = _run(monkeypatch, data_dir)
    assert result.exit_code == 0
    bank = _RecordingWorker.last_init["fact_bank"]
    assert bank.contact.email == "ada@example.com"
    assert bank.contact.name == "Ada Lovelace"
