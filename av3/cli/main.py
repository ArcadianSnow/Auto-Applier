"""v3 CLI (Click). Phase 0: ``init-db``, ``doctor``, ``status``.

Discovery/score/apply commands arrive with the Phase 1 vertical slice.
"""

from __future__ import annotations

import sys

import click

# The Windows console defaults to cp1252 and raises UnicodeEncodeError on non-ASCII
# (job titles, company names, JD text). Emit UTF-8 with replacement so output never
# crashes the command, regardless of the terminal's codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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
    """Auto Applier v3 - staged, observable job-application pipeline."""


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


# Curated seed tokens per ATS (research/ats-discovery-seeding.md), confirmed live where
# noted. Dead tokens are skipped at discovery, so generous lists are safe.
# Confirmed live 2026-05-26 (confirm-probe sweep). Dead tokens are skipped at discovery.
_GH_SEED = ["anthropic", "cloudflare", "tripadvisor", "figma", "discord", "reddit", "gitlab", "robinhood"]
_LEVER_SEED = ["matchgroup", "highspot"]
_ASHBY_SEED = ["Ashby", "Linear", "Ramp", "Vanta", "Notion", "OpenAI"]


@cli.command()
@click.option("--gh", "gh_tokens", default=",".join(_GH_SEED), help="Greenhouse board tokens.")
@click.option("--lever", "lever_sites", default=",".join(_LEVER_SEED), help="Lever site names.")
@click.option("--ashby", "ashby_slugs", default=",".join(_ASHBY_SEED), help="Ashby board slugs.")
@click.option("--max", "max_jobs", default=1, help="Jobs to inspect per token.")
def survey(gh_tokens: str, lever_sites: str, ashby_slugs: str, max_jobs: int) -> None:
    """Multi-ATS CAPTCHA-presence survey: load real Greenhouse/Lever/Ashby apply forms
    (dry-run, NEVER submit), classify the anti-bot challenge, report per-source. Opens a
    headed browser.

    Measures CAPTCHA *prevalence* (the ceiling) to compare auto-apply viability across
    ATSes — NOT the auto-pass rate, which needs real submits (a separate, gated run)."""
    import asyncio
    import json as _json
    from dataclasses import asdict
    from datetime import datetime, timezone

    from av3.sources.browser.survey import (
        ashby_targets,
        gh_targets,
        lever_targets,
        run_multi_survey,
        summarize_survey,
    )
    from av3.telemetry import EventSink, configure_sink

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_sink(EventSink(settings.events_db_path))

    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    click.echo("Building targets (confirm-probing tokens via public APIs)...")
    targets = (
        gh_targets(_split(gh_tokens), max_jobs)
        + lever_targets(_split(lever_sites), max_jobs)
        + ashby_targets(_split(ashby_slugs), max_jobs)
    )
    click.echo(f"Surveying {len(targets)} live apply forms (dry-run, no submits)...")
    rows = asyncio.run(run_multi_survey(targets, settings.browser_profile_dir))

    # Persist BEFORE printing so a display issue can never lose the measurement.
    summary = summarize_survey(rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = settings.data_dir / f"survey_{ts}.json"
    out_path.write_text(
        _json.dumps({"rows": [asdict(r) for r in rows], "summary": summary}, indent=2),
        encoding="utf-8",
    )

    for r in rows:
        if not r.form_present:
            flag = "no-form"
        elif r.captcha_type == "none":
            flag = "none"
        elif r.enterprise:
            flag = "ENTERPRISE"
        elif r.is_invisible:
            flag = "invisible"
        else:
            flag = "VISIBLE"
        click.echo(f"  {r.source:11} {r.token:14} {r.captcha_type:22} [{flag:10}] {r.title[:36]}")
    click.echo("\nSummary:")
    click.echo(_json.dumps(summary, indent=2))
    click.echo(f"\nSaved -> {out_path}")


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
