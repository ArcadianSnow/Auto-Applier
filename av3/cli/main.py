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


# A small curated Greenhouse seed list (research/ats-discovery-seeding.md) for the
# Phase 1 survey. Real boards verified live (anthropic/stripe/databricks etc.).
_DEFAULT_SEED = [
    "anthropic", "stripe", "databricks", "cloudflare", "coinbase",
    "doordash", "cribl", "ecobee", "cargurus", "tripadvisor",
]


@cli.command()
@click.option("--tokens", default=",".join(_DEFAULT_SEED), help="Comma-separated Greenhouse board tokens.")
@click.option("--max", "max_jobs", default=1, help="Jobs to inspect per board token.")
@click.option("--resume", "resume_path", default="", help="Résumé file to attach (optional in dry-run).")
def survey(tokens: str, max_jobs: int, resume_path: str) -> None:
    """Phase 1 CAPTCHA-presence survey: load real Greenhouse forms (dry-run, NEVER submit),
    classify the anti-bot challenge, and report the distribution. Opens a headed browser.

    This measures CAPTCHA *prevalence* (the problem ceiling), NOT the auto-pass rate —
    the pass rate needs real submits (a separate, gated run)."""
    import asyncio
    import json as _json

    from av3.resume.factbank import Contact, FactBank
    from av3.sources.browser.greenhouse_apply import Applicant
    from av3.sources.browser.survey import run_survey, summarize_survey
    from av3.telemetry import EventSink, configure_sink

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_sink(EventSink(settings.events_db_path))

    # Applicant from the fact bank if present, else a benign placeholder (dry-run only).
    bank_path = settings.data_dir / "profile" / "master.json"
    if bank_path.exists():
        bank = FactBank.load(bank_path)
        applicant = Applicant.from_contact(bank.contact)
    else:
        applicant = Applicant.from_contact(
            Contact(name="Survey Tester", email="survey@example.com")
        )

    token_list = [t.strip() for t in tokens.split(",") if t.strip()]
    click.echo(f"Surveying {len(token_list)} boards (dry-run, no submits)...")
    rows = asyncio.run(
        run_survey(token_list, applicant, resume_path, settings.browser_profile_dir, max_jobs)
    )

    # Persist BEFORE printing so a display issue can never lose the measurement.
    from dataclasses import asdict
    from datetime import datetime, timezone

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
        click.echo(f"  {r.token:14} {r.captcha_type:22} [{flag:10}] q={r.custom_questions:<3} {r.title[:40]}")
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
