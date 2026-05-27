"""v3 CLI (Click). Phase 0: ``init-db``, ``doctor``, ``status``.

Discovery/score/apply commands arrive with the Phase 1 vertical slice.
"""

from __future__ import annotations

import sys

import click

from av3 import __version__
from av3.config import load_settings
from av3.db import init_app_db
from av3.doctor import Status, fail_count, run_doctor
from av3.db.repositories import JobRepo
from av3.telemetry import EventSink

# ASCII-only markers — the Windows console (cp1252) can't encode unicode glyphs and
# raises UnicodeEncodeError. This is dev tooling; reliability beats prettiness.
_GLYPH = {Status.PASS: "+", Status.WARN: "!", Status.FAIL: "x"}


@click.group()
@click.version_option(__version__, prog_name="av3")
def cli() -> None:
    """Auto Applier v3 — staged, observable job-application pipeline."""


@cli.command("init-db")
def init_db() -> None:
    """Create the data dir and initialize app.db + events.db (idempotent)."""
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    conn = init_app_db(settings.app_db_path)
    conn.close()
    EventSink(settings.events_db_path).close()
    click.echo(f"Initialized v3 data dir at {settings.data_dir}")
    click.echo(f"  app.db    -> {settings.app_db_path}")
    click.echo(f"  events.db -> {settings.events_db_path}")


@cli.command()
def doctor() -> None:
    """Run preflight checks. Exits non-zero on any FAIL (CI-gateable)."""
    results = run_doctor()
    for r in results:
        line = f"  {_GLYPH[r.status]} {r.status.value:4} {r.name}: {r.detail}"
        click.echo(line)
        if r.status is not Status.PASS and r.fix:
            click.echo(f"        fix -> {r.fix}")
    fails = fail_count(results)
    warns = sum(1 for r in results if r.status is Status.WARN)
    click.echo(f"\n{len(results)} checks - {fails} fail, {warns} warn")
    sys.exit(1 if fails else 0)


@cli.command()
def status() -> None:
    """Show job counts by state from app.db."""
    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        counts = JobRepo(conn).count_by_state()
    finally:
        conn.close()
    if not counts:
        click.echo("No jobs yet. Run discovery (Phase 1+).")
        return
    click.echo("Jobs by state:")
    for state, n in sorted(counts.items()):
        click.echo(f"  {state:14} {n}")


if __name__ == "__main__":
    cli()
