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


@cli.command()
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum QUEUED_APPLY jobs to process this run.")
@click.option("--source", type=click.Choice(["lever", "greenhouse"]), default=None,
              help="Process only this source's jobs (subsets the driver registry).")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run SENDS REAL APPLICATIONS.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="auto = bot fills + submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama/Gemini wiring. Resolver uses bank + sensitive policy only.")
def apply(once: bool, limit: int | None, source: str | None,
          dry_run: bool, mode: str, no_llm: bool) -> None:
    """Drain QUEUED_APPLY jobs through the apply worker (spec section 7 #7).

    Constructs the resolver from the fact bank, opens one stealthy Chrome session, and
    walks each queued job through the per-ATS driver. --dry-run keeps the job in
    QUEUED_APPLY (no state ping-pong); --no-dry-run is the gated path that actually
    submits.
    """
    import asyncio

    from av3.domain.state import ApplyMode
    from av3.llm.complete import build_default
    from av3.llm.embed import OllamaEmbeddings
    from av3.pipeline import ApplyWorker, default_drivers
    from av3.resume.factbank import FactBank
    from av3.sources.browser.session import BrowserSession
    from av3.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank + resume file MUST exist. Clearer to fail here with a
    # doctor-style fix hint than to crash mid-run.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    resume_path = settings.artifacts_dir / "resume.pdf"
    if not resume_path.exists():
        click.echo(f"  x FAIL resume: missing at {resume_path}", err=True)
        click.echo(
            "        fix -> drop a resume.pdf into the artifacts dir until "
            "section 6b resume generation lands.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    drivers = default_drivers()
    if source:
        drivers = {source: drivers[source]}

    # Embed + LLM clients are HTTP-lazy: constructor doesn't touch the network, so a
    # down Ollama just surfaces at resolve time and the resolver falls through to REVIEW.
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )
    llm = None if no_llm else build_default(settings)

    apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

    # Loud confirmation before the irreversible path. The handoff calls this out as a
    # gated user decision and the spec stays "safety floor never tunable by config".
    if not dry_run:
        click.echo(
            f"! --no-dry-run: real submits to source={source or 'lever+greenhouse'} "
            f"mode={mode} limit={limit if limit is not None else 'unbounded'}"
        )

    async def _run():
        session = BrowserSession(settings.browser_profile_dir)
        await session.start()
        try:
            worker = ApplyWorker(
                settings=settings,
                conn=conn,
                fact_bank=bank,
                resume_path=str(resume_path),
                new_page=session.new_page,
                embed_client=embed,
                llm_client=llm,
                mode=apply_mode,
                dry_run=dry_run,
                drivers=drivers,
            )
            return await worker.run_once(limit=limit)
        finally:
            await session.stop()

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; the cp1252 Windows console reconfigures at module top but
    # we still avoid unicode glyphs to match doctor/status output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"applied={summary.applied} review={summary.review} "
        f"skipped={summary.skipped} errors={summary.errors} "
        f"dry_run={summary.dry_run_count} elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: any per-job exception is a non-zero exit.
    sys.exit(1 if summary.errors else 0)


if __name__ == "__main__":
    cli()
