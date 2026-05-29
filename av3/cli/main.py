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
from av3.telemetry import EventSink, configure_sink

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
@click.option("--source", type=click.Choice(["lever", "greenhouse", "ashby"]), default=None,
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
        f"skipped={summary.skipped} paused={summary.paused} "
        f"errors={summary.errors} recovered={summary.recovered} "
        f"dry_run={summary.dry_run_count} elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: any per-job exception is a non-zero exit.
    sys.exit(1 if summary.errors else 0)


@cli.command("filter")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DISCOVERED jobs to process this run.")
@click.option("--threshold", type=float, default=0.6, show_default=True,
              help="Cosine similarity threshold; >= passes to DESCRIBED, < routes to FILTERED.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama. Every DISCOVERED job fail-opens to DESCRIBED (no filtering).")
def filter_cmd(once: bool, limit: int | None, threshold: float, no_llm: bool) -> None:
    """Drain DISCOVERED jobs through the embedding pre-filter (spec section 7 #3).

    Cosine-rank (title + company + snippet) against the master fact-bank summary;
    above threshold passes to DESCRIBED for the next stage to scrape the full JD,
    below threshold routes to FILTERED (terminal). Fail-open if Ollama is unreachable
    or the bank is empty - the alternative would be silently dropping every job into a
    terminal state, which the dashboard couldn't reverse.
    """
    import asyncio

    from av3.llm.embed import OllamaEmbeddings
    from av3.pipeline import FilterWorker
    from av3.resume.factbank import FactBank
    from av3.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist. No resume.pdf check here - the filter only
    # reads the bank (skills/titles/bullets) to form its anchor; the per-job resume
    # is built downstream in the optimize stage.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    # Embed client is HTTP-lazy: constructor doesn't touch the network, so a down
    # Ollama just surfaces at run time and the worker fail-opens per-job.
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )

    async def _run():
        worker = FilterWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            embed_client=embed,
            threshold=threshold,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"passed={summary.passed} filtered={summary.filtered} "
        f"failed_open={summary.failed_open} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # they're fail-opened (so a misconfigured Ollama still trips a monitoring alert).
    sys.exit(1 if summary.errors else 0)


@cli.command("score")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DESCRIBED jobs to process this run.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama/Gemini. Every DESCRIBED job will SKIP (fail-closed) "
                   "with total=0.0 - the opposite of filter --no-llm because scoring "
                   "without an LLM cannot honestly produce a merit-based pass.")
def score_cmd(once: bool, limit: int | None, no_llm: bool) -> None:
    """Drain DESCRIBED jobs through the score worker (spec section 7 #5).

    LLM dimension-scores the full JD against the master fact-bank profile across the
    seven weighted axes from settings.scoring, computes the weighted total, and
    walks the state machine: above review_min stays at DECIDED for the optimize
    worker to pick up; below review_min walks DECIDED -> SKIPPED here so the
    optimize worker can trust every DECIDED job it sees is worth optimizing.

    Fail-CLOSED: missing LLM / empty JD / per-job LLM exception all walk that job
    through SCORED -> DECIDED -> SKIPPED with total=0.0 (the opposite of filter's
    fail-open posture, because fail-open here would auto-apply unscored jobs).
    """
    import asyncio

    from av3.llm.complete import build_default
    from av3.pipeline import ScoreWorker
    from av3.resume.factbank import FactBank
    from av3.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist - the bank summary becomes the profile side
    # of every score prompt. No resume.pdf check (scoring doesn't touch the file;
    # the optimize worker builds the per-job resume from the bank).
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    # LLM client is HTTP-lazy: constructor doesn't touch the network, so a down
    # Ollama just surfaces at run time and the worker fail-closes per-job.
    llm = None if no_llm else build_default(settings)

    if no_llm:
        click.echo(
            "! --no-llm: every DESCRIBED job will SKIP with total=0.0 "
            "(scoring requires an LLM)."
        )

    async def _run():
        worker = ScoreWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            llm_client=llm,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"decided={summary.decided} below_bar={summary.below_bar} "
        f"failed_closed={summary.failed_closed} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # fail-closed (so a misconfigured Ollama still trips a monitoring alert).
    sys.exit(1 if summary.errors else 0)


@cli.command("optimize")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DECIDED jobs to process this run.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama/Gemini. Every DECIDED job will route to REVIEW "
                   "(fail-closed) - the Strict gate cannot generate a tailored "
                   "resume without an LLM.")
def optimize_cmd(once: bool, limit: int | None, no_llm: bool) -> None:
    """Drain DECIDED jobs through the optimize+Strict gate (spec section 7 #6).

    For each above-bar DECIDED job: generate a per-job tailored resume from the
    fact bank, generate a cover letter, run the fabrication guard. ALL THREE must
    pass or the job routes to REVIEW. Pass -> QUEUED_APPLY (apply worker reads it
    blindly, so the Strict gate IS the safety mechanism that justifies
    BROWSER_AUTO - never auto-submit un-optimized).

    Fail-CLOSED: missing LLM / per-job LLM exception / guard rejection / PDF
    render failure all route that one job to REVIEW. Other jobs in the run
    continue (per-job isolation). The CLI exits 1 on errors > 0 so a misconfigured
    Ollama trips monitoring even when the per-job behavior is graceful.
    """
    import asyncio

    from av3.llm.complete import build_default
    from av3.pipeline import OptimizeWorker
    from av3.resume.factbank import FactBank
    from av3.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist - both generation prompts read from it,
    # and the guard uses it as the truth side of the allow-list. No resume.pdf
    # check here: this command writes the per-job PDFs.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    # Ensure the artifacts dir exists - the worker's renderer mkdirs the
    # generated/ subdir, but the parent must exist for that to chain.
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    llm = None if no_llm else build_default(settings)

    if no_llm:
        click.echo(
            "! --no-llm: every DECIDED job will route to REVIEW "
            "(the Strict gate requires an LLM to generate the per-job resume)."
        )

    async def _run():
        worker = OptimizeWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            llm_client=llm,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter/score output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"queued={summary.queued} routed_to_review={summary.routed_to_review} "
        f"guard_rejected={summary.guard_rejected} render_failed={summary.render_failed} "
        f"failed_closed={summary.failed_closed} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # fail-closed (so a misconfigured Ollama still trips a monitoring alert).
    # Guard rejections and render failures do NOT trip the exit code - those are
    # intended-pathway outcomes (the gate is supposed to reject bad output).
    sys.exit(1 if summary.errors else 0)


@cli.command("run")
@click.option("--max-cycles", type=int, default=None,
              help="Stop after N cycles. Default: run forever (Ctrl-C to stop).")
@click.option("--quiet-hours", type=str, default=None,
              help="Local-time window HH:MM-HH:MM during which the apply worker pauses "
                   "(gather stages keep running). Overrides settings.scheduler.quiet_hours.")
@click.option("--cycle-interval-s", type=float, default=None,
              help="Seconds between cycles. Overrides settings.scheduler.cycle_interval_s.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run lets the apply worker SEND REAL APPLICATIONS.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="Apply mode: auto = bot submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama/Gemini wiring. Filter fail-opens, score+optimize fail-CLOSED, "
                   "apply resolver uses bank + sensitive policy only.")
def run_cmd(max_cycles: int | None, quiet_hours: str | None,
            cycle_interval_s: float | None, dry_run: bool, mode: str,
            no_llm: bool) -> None:
    """Always-on staged-worker loop (spec section 7a) — THE production entry.

    Drives filter -> score -> optimize -> apply each cycle in pipeline order
    (so a freshly DISCOVERED job can flow all the way through in one cycle when
    queues are mostly idle). Apply stage is the only one gated by quiet hours;
    gather stages (filter/score/optimize) run 24/7.

    The --once mode on per-worker commands (av3 filter / score / optimize /
    apply --once) stays available for testing and doctor checks; this is what
    you run when the bot should just keep working.
    """
    import asyncio

    from av3.domain.state import ApplyMode
    from av3.llm.complete import build_default
    from av3.llm.embed import OllamaEmbeddings
    from av3.pipeline import (
        ApplyWorker,
        FilterWorker,
        OptimizeWorker,
        Scheduler,
        ScoreWorker,
        default_drivers,
        parse_quiet_hours,
    )
    from av3.resume.factbank import FactBank
    from av3.sources.browser.session import BrowserSession
    from av3.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank + resume file. The apply worker still reads a
    # single global resume.pdf as a fallback (per-job derived paths from the
    # optimize worker land alongside the apply-worker rewire in a future
    # sub-phase).
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
            "        fix -> drop a resume.pdf into the artifacts dir until the "
            "apply worker reads per-job optimize-generated paths.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Build LLM clients ONCE and share across workers — Ollama HTTP is lazy so
    # construction is free even when --no-llm is set (we just pass None instead).
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )
    llm = None if no_llm else build_default(settings)

    apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

    # CLI overrides win over settings defaults; both fall back to the spec defaults.
    effective_cycle_interval = (
        cycle_interval_s if cycle_interval_s is not None
        else settings.scheduler.cycle_interval_s
    )
    effective_quiet_hours_raw = (
        quiet_hours if quiet_hours is not None
        else settings.scheduler.quiet_hours
    )
    quiet_hours_window = parse_quiet_hours(effective_quiet_hours_raw)

    # Loud confirmation before the irreversible path.
    if not dry_run:
        click.echo(
            f"! --no-dry-run: scheduler will SEND REAL APPLICATIONS in mode={mode} "
            f"(quiet_hours={effective_quiet_hours_raw or 'none'}, "
            f"max_cycles={max_cycles if max_cycles is not None else 'unbounded'})"
        )

    # Maintenance hook: prune ephemeral + events + back up both DBs every
    # ``retention.maintenance_interval_s`` seconds. Defined as an async
    # closure so the scheduler can call it without knowing about retention
    # internals. Constructed once and threaded into the scheduler.
    from av3.pipeline.retention import (
        prune_ephemeral as _prune_ephemeral,
        prune_events as _prune_events,
        run_backup_cycle as _run_backup_cycle,
    )

    async def _maintenance():
        _prune_ephemeral(conn, settings.retention.ephemeral_days)
        _prune_events(settings.events_db_path, settings.retention.events_days)
        _run_backup_cycle(settings)

    async def _run():
        session = BrowserSession(settings.browser_profile_dir)
        await session.start()
        try:
            filter_worker = FilterWorker(
                settings=settings, conn=conn, fact_bank=bank,
                embed_client=embed,
            )
            score_worker = ScoreWorker(
                settings=settings, conn=conn, fact_bank=bank,
                llm_client=llm,
            )
            optimize_worker = OptimizeWorker(
                settings=settings, conn=conn, fact_bank=bank,
                llm_client=llm,
            )
            apply_worker = ApplyWorker(
                settings=settings, conn=conn, fact_bank=bank,
                resume_path=str(resume_path),
                new_page=session.new_page,
                embed_client=embed,
                llm_client=llm,
                mode=apply_mode,
                dry_run=dry_run,
                drivers=default_drivers(),
            )
            scheduler = Scheduler(
                filter_worker=filter_worker,
                score_worker=score_worker,
                optimize_worker=optimize_worker,
                apply_worker=apply_worker,
                cycle_interval_s=effective_cycle_interval,
                quiet_hours=quiet_hours_window,
                maintenance=_maintenance,
                maintenance_interval_s=settings.retention.maintenance_interval_s,
            )
            return await scheduler.run(max_cycles=max_cycles)
        finally:
            await session.stop()

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter/score output style.
    click.echo(
        f"cycles={len(summary.cycles)} total_errors={summary.total_errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    # Per-cycle short lines (last 5 only — full detail is in events.db).
    if summary.cycles:
        click.echo("Cycles (last 5):")
        for cs in summary.cycles[-5:]:
            qh = " [quiet-hours]" if cs.apply_skipped_quiet_hours else ""
            paused = " [paused]" if cs.paused else ""
            errs = f" errors={list(cs.stage_errors.keys())}" if cs.stage_errors else ""
            click.echo(f"  - cycle={cs.cycle} elapsed={cs.elapsed_s:.1f}s{qh}{paused}{errs}")

    # CI / cron friendliness: any cycle-level error is a non-zero exit (a stage
    # crash is bad even when we kept the loop alive).
    sys.exit(1 if summary.total_errors else 0)


@cli.command("prune")
@click.option("--ephemeral-days", type=int, default=None,
              help="Override settings.retention.ephemeral_days for this run.")
@click.option("--events-days", type=int, default=None,
              help="Override settings.retention.events_days for this run.")
def prune_cmd(ephemeral_days: int | None, events_days: int | None) -> None:
    """Delete ephemeral data older than the retention windows (spec section 4).

    Two scopes:
      * jobs in EPHEMERAL_STATES (SKIPPED/FILTERED) older than
        --ephemeral-days. APPLIED is kept indefinitely (dedup source of truth).
      * events.db rows older than --events-days (higher write volume, shorter window).

    Both run inside their own transactions; a crash mid-delete rolls back.
    """
    from av3.pipeline.retention import prune_ephemeral, prune_events

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    eff_eph = ephemeral_days if ephemeral_days is not None else settings.retention.ephemeral_days
    eff_evt = events_days if events_days is not None else settings.retention.events_days

    try:
        job_result = prune_ephemeral(conn, eff_eph)
    finally:
        conn.close()
    evt_result = prune_events(settings.events_db_path, eff_evt)

    click.echo(
        f"pruned jobs={job_result.deleted} cutoff={job_result.cutoff_iso} "
        f"(ephemeral_days={eff_eph})"
    )
    click.echo(
        f"pruned events={evt_result.deleted} cutoff={evt_result.cutoff_iso} "
        f"(events_days={eff_evt})"
    )


@cli.command("serve")
@click.option("--host", type=str, default=None,
              help="Bind address. Overrides settings.web.host (default 127.0.0.1). "
                   "Set to 0.0.0.0 to expose to the LAN — the local-first default "
                   "stays on localhost so a runner box doesn't silently leak state.")
@click.option("--port", type=int, default=None,
              help="Listen port. Overrides settings.web.port (default 8765).")
@click.option("--no-scheduler", is_flag=True, default=False,
              help="Start the web UI WITHOUT the background scheduler (read-only "
                   "diagnostics mode). Read-only API endpoints still answer; no "
                   "pipeline work happens. Useful when the fact bank / resume "
                   "aren't ready yet (pre-onboarding) so the dashboard can still "
                   "load to walk the user through setup.")
@click.option("--quiet-hours", type=str, default=None,
              help="Local-time HH:MM-HH:MM window during which the apply worker "
                   "pauses. Overrides settings.scheduler.quiet_hours.")
@click.option("--cycle-interval-s", type=float, default=None,
              help="Seconds between scheduler cycles. Overrides "
                   "settings.scheduler.cycle_interval_s.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run lets the apply worker SEND REAL "
                   "APPLICATIONS in mode=auto.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="Apply mode: auto = bot submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama/Gemini wiring. Filter fail-opens, score+optimize "
                   "fail-CLOSED, apply resolver uses bank + sensitive policy only.")
def serve_cmd(host: str | None, port: int | None, no_scheduler: bool,
              quiet_hours: str | None, cycle_interval_s: float | None,
              dry_run: bool, mode: str, no_llm: bool) -> None:
    """Run the local web UI + background worker service (spec section 11b Phase 4).

    The Phase 4 (1/M) entry: starts the FastAPI app on http://host:port and
    (unless --no-scheduler) boots the staged-worker scheduler as a background
    asyncio task. The dashboard is a Phase 4 (2/M) deliverable; right now the
    splash page lists the read-only JSON API endpoints.

    Lifecycle: SIGINT / Ctrl-C triggers a clean shutdown — the scheduler task
    is cancelled and awaited before uvicorn exits.
    """
    import asyncio
    import uvicorn

    from av3.domain.state import ApplyMode
    from av3.llm.complete import build_default
    from av3.llm.embed import OllamaEmbeddings
    from av3.pipeline import (
        ApplyWorker,
        FilterWorker,
        OptimizeWorker,
        Scheduler,
        ScoreWorker,
        default_drivers,
        parse_quiet_hours,
    )
    from av3.resume.factbank import FactBank
    from av3.sources.browser.session import BrowserSession
    from av3.telemetry import configure_sink
    from av3.web import SchedulerService, WebState, create_app

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    effective_host = host if host is not None else settings.web.host
    effective_port = port if port is not None else settings.web.port

    conn = init_app_db(settings.app_db_path)
    configure_sink(EventSink(settings.events_db_path))

    web_state = WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )

    # Decide whether to spin up the scheduler. Pre-onboarding the fact bank
    # and resume don't exist yet; rather than failing the whole command, drop
    # into read-only diagnostics mode so the user can still reach the (future
    # 5/M) onboarding wizard from the dashboard.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    resume_path = settings.artifacts_dir / "resume.pdf"
    scheduler_ready = fact_bank_path.exists() and resume_path.exists()

    service: SchedulerService | None = None

    if no_scheduler:
        click.echo("! --no-scheduler: read-only diagnostics mode "
                   "(read-only API answers; no pipeline work).")
    elif not scheduler_ready:
        # ASCII-only echo to match doctor/status output style.
        click.echo(
            "! scheduler not started: missing prerequisites — run onboarding "
            "first.",
            err=True,
        )
        if not fact_bank_path.exists():
            click.echo(f"        fact bank: missing at {fact_bank_path}", err=True)
        if not resume_path.exists():
            click.echo(f"        resume:    missing at {resume_path}", err=True)
        click.echo(
            "        web UI still available at "
            f"http://{effective_host}:{effective_port} (read-only).",
            err=True,
        )
    else:
        # Build the factory the SchedulerService will call with our pause
        # predicate. Same construction as ``av3 run`` (see run_cmd) — keep in
        # sync if knobs change.
        bank = FactBank.load(fact_bank_path)

        embed = None if no_llm else OllamaEmbeddings(
            host=settings.llm.ollama_host, model=settings.llm.embed_model
        )
        llm = None if no_llm else build_default(settings)
        apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

        effective_cycle_interval = (
            cycle_interval_s if cycle_interval_s is not None
            else settings.scheduler.cycle_interval_s
        )
        effective_quiet_hours_raw = (
            quiet_hours if quiet_hours is not None
            else settings.scheduler.quiet_hours
        )
        quiet_hours_window = parse_quiet_hours(effective_quiet_hours_raw)

        if not dry_run:
            click.echo(
                f"! --no-dry-run: scheduler will SEND REAL APPLICATIONS in mode={mode} "
                f"(quiet_hours={effective_quiet_hours_raw or 'none'})"
            )

        from av3.pipeline.retention import (
            prune_ephemeral as _prune_ephemeral,
            prune_events as _prune_events,
            run_backup_cycle as _run_backup_cycle,
        )

        async def _maintenance():
            _prune_ephemeral(conn, settings.retention.ephemeral_days)
            _prune_events(settings.events_db_path, settings.retention.events_days)
            _run_backup_cycle(settings)

        # The BrowserSession is started lazily inside the factory so the
        # uvicorn event loop owns its lifecycle. The factory runs inside
        # SchedulerService.start() (already async); the teardown closure
        # mirror-runs inside SchedulerService.stop().
        _session_holder: dict[str, BrowserSession | None] = {"session": None}

        async def _factory(pause_predicate):
            session = BrowserSession(settings.browser_profile_dir)
            await session.start()
            _session_holder["session"] = session
            return Scheduler(
                filter_worker=FilterWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    embed_client=embed,
                ),
                score_worker=ScoreWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    llm_client=llm,
                ),
                optimize_worker=OptimizeWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    llm_client=llm,
                ),
                apply_worker=ApplyWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    resume_path=str(resume_path), new_page=session.new_page,
                    embed_client=embed, llm_client=llm, mode=apply_mode,
                    dry_run=dry_run, drivers=default_drivers(),
                ),
                cycle_interval_s=effective_cycle_interval,
                quiet_hours=quiet_hours_window,
                pause_predicate=pause_predicate,
                maintenance=_maintenance,
                maintenance_interval_s=settings.retention.maintenance_interval_s,
            )

        async def _teardown():
            sess = _session_holder.get("session")
            if sess is not None:
                await sess.stop()
                _session_holder["session"] = None

        service = SchedulerService(_factory, teardown=_teardown)

    app = create_app(state=web_state, service=service)

    click.echo(
        f"Starting av3 web UI on http://{effective_host}:{effective_port} "
        f"(scheduler={'on' if service is not None else 'off'})"
    )

    try:
        uvicorn.run(
            app,
            host=effective_host,
            port=effective_port,
            log_level="info",
            access_log=False,  # quiet — events.db is the audit trail
        )
    finally:
        conn.close()


@cli.command("backup")
def backup_cmd() -> None:
    """Snapshot app.db + events.db; rotate older snapshots (spec section 4).

    Uses SQLite's online backup API so it's safe while the DB is in use. Both
    backups are attempted even if one fails; failure is reported in the
    summary line with a non-zero exit code so cron / monitoring catches it.
    """
    from av3.pipeline.retention import run_backup_cycle

    settings = load_settings()
    configure_sink(EventSink(settings.events_db_path))
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    summary = run_backup_cycle(settings)

    for b in summary.backups:
        click.echo(
            f"{b.db_label}: snapshot={b.snapshot_path.name} rotated={b.rotated}"
        )
    for db_label, err in summary.errors.items():
        click.echo(f"  x FAIL {db_label}: {err}", err=True)

    sys.exit(0 if summary.ok else 1)


if __name__ == "__main__":
    cli()
