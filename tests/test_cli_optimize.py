"""Contract tests for ``av3 optimize`` (the CLI entry, spec section 7 #6).

Stubs :class:`OptimizeWorker` (behavior already covered by ``test_optimize_worker.py``)
so these tests focus on the CLI's own contract: argument parsing, pre-flight,
summary line shape, exit codes, --no-llm warning + None LLM threading, fact-bank
threading.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.pipeline.optimize_worker import OptimizeRunSummary


# --------------------------------------------------------------- fixtures

def _seed_bank(data_dir: Path) -> None:
    """Write the one artifact the optimize command pre-flights for."""
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
    next_summary: OptimizeRunSummary | None = None

    def __init__(self, **kwargs):
        type(self).last_init = dict(kwargs)
        self._limit_seen: int | None = None

    async def run_once(self, limit=None):
        self._limit_seen = limit
        summary = type(self).next_summary
        if summary is None:
            summary = OptimizeRunSummary(run_id="test-run")
        if limit is not None:
            summary.notes = list(summary.notes) + [f"limit_seen={limit}"]
        return summary


def _invoke(tmp_path: Path, monkeypatch, args: list[str], *,
            worker_summary: OptimizeRunSummary | None = None,
            seed_bank: bool = True):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    if seed_bank:
        _seed_bank(tmp_path)

    _RecordingWorker.last_init = None
    _RecordingWorker.next_summary = worker_summary

    monkeypatch.setattr("auto_applier.pipeline.OptimizeWorker", _RecordingWorker, raising=True)
    monkeypatch.setattr(
        "auto_applier.pipeline.optimize_worker.OptimizeWorker", _RecordingWorker, raising=True
    )

    return CliRunner().invoke(cli, ["optimize", *args])


# --------------------------------------------------------------- pre-flight

def test_preflight_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, [], seed_bank=False)
    assert result.exit_code == 2
    assert "fact bank" in result.output
    assert "fix ->" in result.output
    assert _RecordingWorker.last_init is None


def test_preflight_creates_artifacts_dir(tmp_path, monkeypatch):
    """The renderer mkdirs the generated/ subdir, but the artifacts_dir parent
    must exist for that to chain cleanly. CLI ensures it on every invocation."""
    _invoke(tmp_path, monkeypatch, [])
    assert (tmp_path / "artifacts").exists()


# --------------------------------------------------------------- summary line

def test_summary_line_shape_on_clean_run(tmp_path, monkeypatch):
    summary = OptimizeRunSummary(
        run_id="rid-1", attempted=5, queued=3, routed_to_review=2,
        guard_rejected=1, render_failed=0, failed_closed=1, errors=0,
        elapsed_s=2.3,
    )
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)

    assert result.exit_code == 0
    out = result.output
    assert "run_id=rid-1" in out
    assert "attempted=5" in out
    assert "queued=3" in out
    assert "routed_to_review=2" in out
    assert "guard_rejected=1" in out
    assert "render_failed=0" in out
    assert "failed_closed=1" in out
    assert "errors=0" in out
    assert "elapsed=2.3s" in out


def test_notes_block_rendered_when_present(tmp_path, monkeypatch):
    summary = OptimizeRunSummary(
        run_id="rid-2",
        notes=["no LLM client; every DECIDED job will route to REVIEW"],
    )
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 0
    assert "Notes:" in result.output


# --------------------------------------------------------------- exit codes

def test_errors_field_drives_exit_1(tmp_path, monkeypatch):
    summary = OptimizeRunSummary(run_id="rid-3", attempted=3, queued=2,
                                 failed_closed=1, errors=1)
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 1
    assert "errors=1" in result.output


def test_guard_rejections_alone_do_not_error(tmp_path, monkeypatch):
    """Guard rejections are intended-pathway outcomes - the gate is supposed
    to reject bad output. Don't trip the exit code on them."""
    summary = OptimizeRunSummary(run_id="rid-4", attempted=3,
                                 queued=2, routed_to_review=1, guard_rejected=1,
                                 errors=0)
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 0


def test_render_failures_alone_do_not_error(tmp_path, monkeypatch):
    """Render failures (Playwright missing, etc.) are also a graceful fail-CLOSED;
    they route the job to REVIEW but don't trip the CLI exit code. The
    misconfiguration shows up in the routed_to_review + render_failed counters
    which an operator can monitor separately."""
    summary = OptimizeRunSummary(run_id="rid-5", attempted=3,
                                 queued=2, routed_to_review=1, render_failed=1,
                                 errors=0)
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 0


def test_failed_closed_alone_does_not_error(tmp_path, monkeypatch):
    """--no-llm intentionally fail-closes every job (not an error condition);
    exit code stays 0 so cron schedules don't alarm on the documented mode."""
    summary = OptimizeRunSummary(run_id="rid-6", attempted=3,
                                 routed_to_review=3, failed_closed=3, errors=0)
    result = _invoke(tmp_path, monkeypatch, ["--no-llm"], worker_summary=summary)
    assert result.exit_code == 0


# --------------------------------------------------------------- option plumbing

def test_no_llm_passes_none_client_and_warns(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, ["--no-llm"])
    assert result.exit_code == 0
    # Worker got None for llm_client.
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["llm_client"] is None
    # CLI surfaces a visible warning so the user doesn't think the run optimized normally.
    assert "--no-llm" in result.output
    assert "REVIEW" in result.output


def test_default_llm_client_is_constructed(tmp_path, monkeypatch):
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["llm_client"] is not None
    # The HTTP client must be lazy: constructor doesn't dial out.
    assert hasattr(kwargs["llm_client"], "complete_json")


def test_limit_threads_through_to_run_once(tmp_path, monkeypatch):
    summary = OptimizeRunSummary(run_id="rid-7")
    result = _invoke(tmp_path, monkeypatch, ["--limit", "9"], worker_summary=summary)
    assert result.exit_code == 0
    assert "limit_seen=9" in result.output


def test_fact_bank_is_loaded_and_passed(tmp_path, monkeypatch):
    """The bank we seed must be the bank the worker receives - guards against a
    silent wiring regression where the CLI builds a default-empty FactBank."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    bank = kwargs["fact_bank"]
    assert bank.skills == ["python", "sql"]
    assert bank.contact.email == "ada@example.com"


def test_settings_passed_to_worker(tmp_path, monkeypatch):
    """The worker reads settings.artifacts_dir + llm.ollama_model for tagging; the
    CLI must thread the loaded Settings, not a default-constructed instance."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    settings = kwargs["settings"]
    assert hasattr(settings, "artifacts_dir")
    assert settings.artifacts_dir == tmp_path / "artifacts"
