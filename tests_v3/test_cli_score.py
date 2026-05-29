"""Contract tests for ``av3 score`` (the CLI entry, spec section 7 #5).

Stubs :class:`ScoreWorker` (behavior already covered by ``test_score_worker.py``)
so these tests focus on the CLI's own contract: argument parsing, pre-flight,
summary line shape, exit codes, --no-llm warning + None LLM threading, fact-bank
threading.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from av3.cli.main import cli
from av3.pipeline.score_worker import ScoreRunSummary


# --------------------------------------------------------------- fixtures

def _seed_bank(data_dir: Path) -> None:
    """Write the one artifact the score command pre-flights for."""
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
    next_summary: ScoreRunSummary | None = None

    def __init__(self, **kwargs):
        type(self).last_init = dict(kwargs)
        self._limit_seen: int | None = None

    async def run_once(self, limit=None):
        self._limit_seen = limit
        summary = type(self).next_summary
        if summary is None:
            summary = ScoreRunSummary(run_id="test-run")
        if limit is not None:
            summary.notes = list(summary.notes) + [f"limit_seen={limit}"]
        return summary


def _invoke(tmp_path: Path, monkeypatch, args: list[str], *,
            worker_summary: ScoreRunSummary | None = None,
            seed_bank: bool = True):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    if seed_bank:
        _seed_bank(tmp_path)

    _RecordingWorker.last_init = None
    _RecordingWorker.next_summary = worker_summary

    monkeypatch.setattr("av3.pipeline.ScoreWorker", _RecordingWorker, raising=True)
    monkeypatch.setattr(
        "av3.pipeline.score_worker.ScoreWorker", _RecordingWorker, raising=True
    )

    return CliRunner().invoke(cli, ["score", *args])


# --------------------------------------------------------------- pre-flight

def test_preflight_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, [], seed_bank=False)
    assert result.exit_code == 2
    assert "fact bank" in result.output
    assert "fix ->" in result.output
    assert _RecordingWorker.last_init is None


# --------------------------------------------------------------- summary line

def test_summary_line_shape_on_clean_run(tmp_path, monkeypatch):
    summary = ScoreRunSummary(
        run_id="rid-1", attempted=5, decided=3, below_bar=2,
        failed_closed=0, errors=0, elapsed_s=1.2,
    )
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)

    assert result.exit_code == 0
    out = result.output
    assert "run_id=rid-1" in out
    assert "attempted=5" in out
    assert "decided=3" in out
    assert "below_bar=2" in out
    assert "failed_closed=0" in out
    assert "errors=0" in out
    assert "elapsed=1.2s" in out


def test_notes_block_rendered_when_present(tmp_path, monkeypatch):
    summary = ScoreRunSummary(run_id="rid-2",
                              notes=["no LLM client; every job will SKIP"])
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 0
    assert "Notes:" in result.output


# --------------------------------------------------------------- exit codes

def test_errors_field_drives_exit_1(tmp_path, monkeypatch):
    summary = ScoreRunSummary(run_id="rid-3", attempted=3, decided=2,
                              failed_closed=1, errors=1)
    result = _invoke(tmp_path, monkeypatch, [], worker_summary=summary)
    assert result.exit_code == 1
    assert "errors=1" in result.output


def test_failed_closed_alone_does_not_error(tmp_path, monkeypatch):
    """--no-llm intentionally fail-closes every job (not an error condition);
    exit code stays 0 so cron schedules don't alarm on the documented mode."""
    summary = ScoreRunSummary(run_id="rid-4", attempted=3,
                              failed_closed=3, errors=0)
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
    # CLI surfaces a visible warning so the user doesn't think the run scored normally.
    assert "--no-llm" in result.output
    assert "SKIP" in result.output  # part of "every DESCRIBED job will SKIP"


def test_default_llm_client_is_constructed(tmp_path, monkeypatch):
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    assert kwargs["llm_client"] is not None
    # The HTTP client must be lazy: constructor doesn't dial out.
    assert hasattr(kwargs["llm_client"], "complete_json")


def test_limit_threads_through_to_run_once(tmp_path, monkeypatch):
    summary = ScoreRunSummary(run_id="rid-5")
    result = _invoke(tmp_path, monkeypatch, ["--limit", "9"], worker_summary=summary)
    assert result.exit_code == 0
    assert "limit_seen=9" in result.output


def test_fact_bank_is_loaded_and_passed(tmp_path, monkeypatch):
    """The bank we seed must be the bank the worker receives — guards against a
    silent wiring regression where the CLI builds a default-empty FactBank."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    bank = kwargs["fact_bank"]
    assert bank.skills == ["python", "sql"]
    assert bank.contact.email == "ada@example.com"


def test_settings_passed_to_worker(tmp_path, monkeypatch):
    """The worker reads settings.scoring.weights + review_min; the CLI must thread
    the loaded Settings, not a default-constructed instance, so user_config.json
    overrides (when present) actually take effect."""
    _invoke(tmp_path, monkeypatch, [])
    kwargs = _RecordingWorker.last_init
    assert kwargs is not None
    settings = kwargs["settings"]
    assert hasattr(settings, "scoring")
    assert settings.scoring.review_min == 4.0  # default
    assert settings.scoring.weights.skills == 0.35  # default
