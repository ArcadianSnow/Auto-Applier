"""Contract tests for ``av3 filter`` (the CLI entry, spec section 7 #3).

These tests stub :class:`FilterWorker` (behavior already covered by
``test_filter_worker.py``) so the CLI tests stay focused on the CLI's own contract:
argument parsing, pre-flight checks, summary line shape, exit codes, and that the
right constructor knobs reach the worker.

What is intentionally NOT tested here:
  * Worker state transitions, fail-open posture, telemetry skip rows — owned by
    ``test_filter_worker.py``.
  * Live Ollama HTTP — owned by the dedicated embed smoke tests (Phase 3 (9/M)).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from av3.cli.main import cli
from av3.pipeline.filter_worker import FilterRunSummary


# --------------------------------------------------------------- fixtures / helpers

def _seed_bank(data_dir: Path) -> None:
    """Write the one artifact the filter command pre-flights for."""
    (data_dir / "profile").mkdir(parents=True, exist_ok=True)
    (data_dir / "profile" / "master.json").write_text(
        json.dumps({
            "contact": {"name": "Ada Lovelace", "email": "ada@example.com"},
            "work_history": [],
            "education": [],
            "skills": ["python", "sql"],
            "certifications": [],
            "allowed_metrics": [],
            "work_authorization": "US citizen",
            "requires_sponsorship": False,
            "eeo": {},
        }),
        encoding="utf-8",
    )


class _RecordingWorker:
    """Captures constructor kwargs for assertion and returns a deterministic summary."""

    last_init: dict | None = None
    next_summary: FilterRunSummary | None = None

    def __init__(self, **kwargs):
        type(self).last_init = dict(kwargs)
        self._limit_seen: int | None = None

    async def run_once(self, limit=None):
        self._limit_seen = limit
        summary = type(self).next_summary
        if summary is None:
            summary = FilterRunSummary(run_id="test-run")
        if limit is not None:
            summary.notes = list(summary.notes) + [f"limit_seen={limit}"]
        return summary


def _invoke(tmp_path: Path, monkeypatch, args: list[str], *,
            worker_summary: FilterRunSummary | None = None,
            seed_bank: bool = True):
    """Run ``av3 filter`` with stubs swapped in; return the Click Result."""
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    if seed_bank:
        _seed_bank(tmp_path)

    _RecordingWorker.last_init = None
    _RecordingWorker.next_summary = worker_summary

    # Patch FilterWorker in the cli module's import resolution path. The command
    # imports `from av3.pipeline import FilterWorker`; pipeline re-exports the same
    # symbol it imports from filter_worker, so monkeypatching both keeps the test
    # robust to the CLI changing its import style.
    monkeypatch.setattr("av3.pipeline.FilterWorker", _RecordingWorker, raising=True)
    monkeypatch.setattr(
        "av3.pipeline.filter_worker.FilterWorker", _RecordingWorker, raising=True
    )

    return CliRunner().invoke(cli, ["filter", *args])


# --------------------------------------------------------------- pre-flight

def test_preflight_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, ["--limit", "1"], seed_bank=False)
    assert result.exit_code == 2
    assert "fact bank" in result.output
    assert "fix ->" in result.output
    # Worker must NOT have been constructed when pre-flight fails.
    assert _RecordingWorker.last_init is None


# --------------------------------------------------------------- happy path

def test_summary_line_shape_on_clean_run(tmp_path, monkeypatch):
    summary = FilterRunSummary(
        run_id="rid-1", attempted=4, passed=2, filtered=2, failed_open=0,
        errors=0, elapsed_s=0.7,
    )
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)

    assert result.exit_code == 0
    out = result.output
    assert "run_id=rid-1" in out
    assert "attempted=4" in out
    assert "passed=2" in out
    assert "filtered=2" in out
    assert "failed_open=0" in out
    assert "errors=0" in out
    assert "elapsed=0.7s" in out


def test_notes_block_rendered_when_present(tmp_path, monkeypatch):
    summary = FilterRunSummary(run_id="rid-2", notes=["no embed client; routed all"])
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 0
    assert "Notes:" in result.output
    assert "no embed client" in result.output


# --------------------------------------------------------------- exit codes

def test_errors_field_drives_exit_1(tmp_path, monkeypatch):
    """A per-job embed error fail-opens that job but still bumps `errors`, which the
    CLI surfaces as exit 1 so CI/monitoring can alert on a misconfigured Ollama."""
    summary = FilterRunSummary(run_id="rid-3", attempted=3, passed=2, failed_open=1,
                               errors=1)
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 1
    assert "errors=1" in result.output


def test_clean_run_with_failed_open_only_exits_0(tmp_path, monkeypatch):
    """failed_open alone is not an error condition (it's a documented fail-open
    posture, e.g. --no-llm). Exit code stays 0."""
    summary = FilterRunSummary(run_id="rid-4", attempted=3, passed=0, failed_open=3,
                               errors=0)
    result = _invoke(tmp_path, monkeypatch, ["--no-llm"], worker_summary=summary)
    assert result.exit_code == 0


# --------------------------------------------------------------- option plumbing

def test_no_llm_passes_none_embed_client(tmp_path, monkeypatch):
    _invoke(tmp_path, monkeypatch, ["--no-llm"])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["embed_client"] is None


def test_default_embed_client_is_constructed(tmp_path, monkeypatch):
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["embed_client"] is not None  # OllamaEmbeddings instance
    # The HTTP client must be lazy: constructor doesn't dial out.
    assert hasattr(kwargs["embed_client"], "embed")


def test_threshold_flag_threads_through(tmp_path, monkeypatch):
    _invoke(tmp_path, monkeypatch, ["--threshold", "0.42"])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["threshold"] == 0.42


def test_default_threshold_is_06(tmp_path, monkeypatch):
    """The conservative default (favor recall) until the eval harness (Phase 3 (7/M))
    provides a calibration set."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["threshold"] == 0.6


def test_limit_threads_through_to_run_once(tmp_path, monkeypatch):
    summary = FilterRunSummary(run_id="rid-5")
    result = _invoke(tmp_path, monkeypatch, ["--limit", "7"], worker_summary=summary)
    assert result.exit_code == 0
    # The recording worker stamps the limit into notes so we can observe it.
    assert "limit_seen=7" in result.output


def test_fact_bank_is_loaded_and_passed(tmp_path, monkeypatch):
    """The bank we seed must be the bank the worker receives — not a default-empty
    FactBank built somewhere upstream of the CLI."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    bank = kwargs["fact_bank"]
    assert bank.skills == ["python", "sql"]
    assert bank.contact.email == "ada@example.com"
