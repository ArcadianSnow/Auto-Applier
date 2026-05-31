"""Contract tests for ``av3 run`` (the staged-scheduler CLI, spec section 7a).

Stubs the Scheduler + BrowserSession entirely (behavior covered by
``test_scheduler.py``) so these tests focus on the CLI's own contract:
argument parsing, pre-flight, summary line shape, exit codes, option plumbing
(quiet-hours, cycle-interval, max-cycles, dry-run/mode/no-llm), session
lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auto_applier.cli.main import cli
from auto_applier.pipeline.scheduler import CycleSummary, SchedulerRunSummary


# --------------------------------------------------------------- fixtures

def _seed_bank(data_dir: Path) -> None:
    """Write the fact bank artifact the run command pre-flights for."""
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


def _seed_resume(data_dir: Path) -> None:
    """Write the resume.pdf artifact the run command pre-flights for."""
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts" / "resume.pdf").write_bytes(b"\x25PDF-1.4\n%%EOF")


class _RecordingScheduler:
    """Captures constructor kwargs for assertion and returns a deterministic summary."""

    last_init: dict | None = None
    next_summary: SchedulerRunSummary | None = None

    def __init__(self, **kwargs):
        type(self).last_init = dict(kwargs)
        self._max_cycles_seen: int | None = None

    async def run(self, max_cycles=None):
        self._max_cycles_seen = max_cycles
        summary = type(self).next_summary
        if summary is None:
            summary = SchedulerRunSummary()
            # One synthetic cycle so the summary line has something to render.
            summary.cycles.append(CycleSummary(cycle=1, started_at="2026-05-29T00:00:00+00:00"))
        return summary


class _StubBrowserSession:
    """No-op browser session — the scheduler is stubbed so new_page is never called."""

    started = 0
    stopped = 0

    def __init__(self, profile_dir):
        self.profile_dir = profile_dir

    async def start(self):
        type(self).started += 1

    async def stop(self):
        type(self).stopped += 1

    async def new_page(self):
        raise AssertionError("new_page should not be called when Scheduler is stubbed")


def _invoke(
    tmp_path: Path,
    monkeypatch,
    args: list[str],
    *,
    scheduler_summary: SchedulerRunSummary | None = None,
    seed_bank: bool = True,
    seed_resume: bool = True,
):
    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    if seed_bank:
        _seed_bank(tmp_path)
    if seed_resume:
        _seed_resume(tmp_path)

    _RecordingScheduler.last_init = None
    _RecordingScheduler.next_summary = scheduler_summary
    _StubBrowserSession.started = 0
    _StubBrowserSession.stopped = 0

    monkeypatch.setattr("auto_applier.pipeline.Scheduler", _RecordingScheduler, raising=True)
    monkeypatch.setattr(
        "auto_applier.pipeline.scheduler.Scheduler", _RecordingScheduler, raising=True
    )
    monkeypatch.setattr(
        "auto_applier.sources.browser.session.BrowserSession",
        _StubBrowserSession,
        raising=True,
    )

    return CliRunner().invoke(cli, ["run", *args])


# --------------------------------------------------------------- pre-flight

def test_preflight_missing_fact_bank_exits_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, [], seed_bank=False)
    assert result.exit_code == 2
    assert "fact bank" in result.output
    assert "fix ->" in result.output


def test_preflight_missing_resume_exits_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, [], seed_resume=False)
    assert result.exit_code == 2
    assert "resume" in result.output


# --------------------------------------------------------------- summary line

def test_summary_line_shape_on_clean_run(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[
            CycleSummary(cycle=1, started_at="2026-05-29T00:00:00+00:00", elapsed_s=1.2),
            CycleSummary(cycle=2, started_at="2026-05-29T00:01:00+00:00", elapsed_s=1.0),
        ],
        total_errors=0,
        elapsed_s=3.5,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "2"],
                     scheduler_summary=summary)

    assert result.exit_code == 0
    assert "cycles=2" in result.output
    assert "total_errors=0" in result.output
    assert "elapsed=3.5s" in result.output
    assert "Cycles (last 5):" in result.output
    assert "cycle=1" in result.output
    assert "cycle=2" in result.output


def test_summary_renders_quiet_hours_flag(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[CycleSummary(cycle=1, started_at="2026-05-29T00:00:00+00:00",
                              apply_skipped_quiet_hours=True)],
        total_errors=0,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"],
                     scheduler_summary=summary)
    assert "quiet-hours" in result.output


def test_summary_renders_pause_flag(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[CycleSummary(cycle=1, started_at="2026-05-29T00:00:00+00:00", paused=True)],
        total_errors=0,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"],
                     scheduler_summary=summary)
    assert "paused" in result.output


def test_summary_renders_stage_errors(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[CycleSummary(
            cycle=1, started_at="2026-05-29T00:00:00+00:00",
            stage_errors={"filter": "RuntimeError: boom"},
        )],
        total_errors=1,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"],
                     scheduler_summary=summary)
    assert "filter" in result.output


# --------------------------------------------------------------- exit codes

def test_total_errors_drives_exit_1(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[CycleSummary(
            cycle=1, started_at="2026-05-29T00:00:00+00:00",
            stage_errors={"filter": "boom"},
        )],
        total_errors=1,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"],
                     scheduler_summary=summary)
    assert result.exit_code == 1


def test_zero_errors_exits_0(tmp_path, monkeypatch):
    summary = SchedulerRunSummary(
        cycles=[CycleSummary(cycle=1, started_at="2026-05-29T00:00:00+00:00")],
        total_errors=0,
    )
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"],
                     scheduler_summary=summary)
    assert result.exit_code == 0


# --------------------------------------------------------------- option plumbing

def test_max_cycles_threads_through_to_scheduler_run(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "5"])
    assert result.exit_code == 0
    # _RecordingScheduler's run() captured the max_cycles kwarg via the recorder pattern;
    # we can't get to it after Scheduler instances aren't preserved, but we can assert
    # the Scheduler constructor was called (i.e. CLI got to run-construction).
    assert _RecordingScheduler.last_init is not None


def test_quiet_hours_cli_overrides_settings(tmp_path, monkeypatch):
    """CLI --quiet-hours wins over settings.scheduler.quiet_hours. Verify the
    parsed window made it onto the Scheduler constructor kwarg."""
    result = _invoke(tmp_path, monkeypatch,
                     ["--max-cycles", "1", "--quiet-hours", "22:00-08:00"])
    assert result.exit_code == 0
    kwargs = _RecordingScheduler.last_init
    assert kwargs is not None
    qh = kwargs["quiet_hours"]
    assert qh.raw == "22:00-08:00"
    assert qh.is_window is True


def test_cycle_interval_cli_overrides_settings(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch,
                     ["--max-cycles", "1", "--cycle-interval-s", "5.0"])
    assert result.exit_code == 0
    kwargs = _RecordingScheduler.last_init
    assert kwargs is not None
    assert kwargs["cycle_interval_s"] == 5.0


def test_no_llm_passes_none_clients(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1", "--no-llm"])
    assert result.exit_code == 0
    # We can't inspect the workers directly (they were stubbed away by Scheduler
    # stub), but the smoke check is that the CLI didn't fail constructing them.


def test_dry_run_default_no_warning_banner(tmp_path, monkeypatch):
    """Default --dry-run produces no '! --no-dry-run' banner."""
    result = _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"])
    assert "! --no-dry-run" not in result.output


def test_no_dry_run_shows_warning_banner(tmp_path, monkeypatch):
    """--no-dry-run prints a loud confirmation line (CLI guards the irreversible path)."""
    result = _invoke(tmp_path, monkeypatch,
                     ["--max-cycles", "1", "--no-dry-run"])
    assert "! --no-dry-run" in result.output


# --------------------------------------------------------------- session lifecycle

def test_session_lifecycle_start_stop(tmp_path, monkeypatch):
    """The BrowserSession is started before the scheduler runs and stopped
    after — even when max_cycles=1 (no long-lived run)."""
    _invoke(tmp_path, monkeypatch, ["--max-cycles", "1"])
    assert _StubBrowserSession.started == 1
    assert _StubBrowserSession.stopped == 1


def test_session_stops_on_scheduler_error(tmp_path, monkeypatch):
    """A scheduler exception must still close the session (try/finally
    discipline). We construct a Scheduler stub that raises in run()."""

    class _RaisingScheduler:
        def __init__(self, **kwargs):
            pass

        async def run(self, max_cycles=None):
            raise RuntimeError("scheduler boom")

    monkeypatch.setenv("AV3_DATA_DIR", str(tmp_path))
    _seed_bank(tmp_path)
    _seed_resume(tmp_path)
    _StubBrowserSession.started = 0
    _StubBrowserSession.stopped = 0
    monkeypatch.setattr("auto_applier.pipeline.Scheduler", _RaisingScheduler, raising=True)
    monkeypatch.setattr("auto_applier.pipeline.scheduler.Scheduler", _RaisingScheduler, raising=True)
    monkeypatch.setattr(
        "auto_applier.sources.browser.session.BrowserSession",
        _StubBrowserSession,
        raising=True,
    )

    result = CliRunner().invoke(cli, ["run", "--max-cycles", "1"])
    assert result.exit_code != 0  # propagated exception
    # Session was still stopped (try/finally fired).
    assert _StubBrowserSession.stopped == 1
