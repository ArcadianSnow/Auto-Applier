"""CLI entry point for Auto Applier v2."""
import asyncio
import json
import sys
from collections import Counter

import click

from auto_applier.config import DATA_DIR, USER_CONFIG_FILE, MAX_APPLICATIONS_PER_DAY


def load_user_config() -> dict:
    """Load the user configuration from disk."""
    if USER_CONFIG_FILE.exists():
        with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_user_config(config: dict) -> None:
    """Persist the user configuration to disk."""
    with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ---------------------------------------------------------------------------
# CLI event handlers (proper functions instead of complex lambdas)
# ---------------------------------------------------------------------------


def _on_run_started(**kw):
    mode = "dry run" if kw.get("dry_run") else "live run"
    click.echo(f"Starting {mode}...")


def _on_platform_started(**kw):
    name = kw.get("platform", "").title()
    click.echo(f"\n{'=' * 40}\nPlatform: {name}\n{'=' * 40}")


def _on_platform_login_needed(**kw):
    name = kw.get("platform", "").title()
    click.echo(f"Please log in to {name} in the browser window...")


def _on_platform_login_failed(**kw):
    name = kw.get("platform", "").title()
    click.echo(f"Login failed for {name} -- skipping.", err=True)


def _on_jobs_found(**kw):
    count = kw.get("count", 0)
    keyword = kw.get("keyword", "")
    click.echo(f"Found {count} new jobs for '{keyword}'")


def _on_job_scored(**kw):
    score = kw.get("score")
    job = kw.get("job")
    if score and job:
        click.echo(
            f"  [{score.score}] {job.title} at {job.company} "
            f"(resume: {score.resume_label}, decision: {score.decision.value})"
        )


def _on_application_started(**kw):
    job = kw.get("job")
    resume = kw.get("resume", "")
    if job:
        click.echo(f"  Applying to {job.title} with resume '{resume}'...")


def _on_application_complete(**kw):
    app = kw.get("application")
    if app:
        click.echo(f"  -> {app.status}")


def _on_platform_error(**kw):
    click.echo(f"  ERROR: {kw.get('error', '')}", err=True)


def _on_captcha_detected(**kw):
    name = kw.get("platform", "")
    click.echo(f"  CAPTCHA detected on {name} -- stopping this platform")


def _on_evolution_triggers(**kw):
    triggers = kw.get("triggers", [])
    if triggers:
        click.echo(f"\n{len(triggers)} skills ready for resume evolution:")
        for t in triggers:
            click.echo(f"  -> {t.skill_name} (seen {t.times_seen}x)")


def _on_run_finished(**kw):
    applied = kw.get("applied", 0)
    skipped = kw.get("skipped", 0)
    failed = kw.get("failed", 0)
    reason = kw.get("reason", "")
    click.echo(
        f"\nDone ({reason}). Applied: {applied}, Skipped: {skipped}, Failed: {failed}"
    )


def _attach_cli_handlers(events):
    """Wire all CLI event handlers to an EventEmitter."""
    from auto_applier.orchestrator.events import (
        RUN_STARTED,
        PLATFORM_STARTED,
        PLATFORM_LOGIN_NEEDED,
        PLATFORM_LOGIN_FAILED,
        JOBS_FOUND,
        JOB_SCORED,
        APPLICATION_STARTED,
        APPLICATION_COMPLETE,
        PLATFORM_ERROR,
        CAPTCHA_DETECTED,
        EVOLUTION_TRIGGERS,
        RUN_FINISHED,
    )

    events.on(RUN_STARTED, _on_run_started)
    events.on(PLATFORM_STARTED, _on_platform_started)
    events.on(PLATFORM_LOGIN_NEEDED, _on_platform_login_needed)
    events.on(PLATFORM_LOGIN_FAILED, _on_platform_login_failed)
    events.on(JOBS_FOUND, _on_jobs_found)
    events.on(JOB_SCORED, _on_job_scored)
    events.on(APPLICATION_STARTED, _on_application_started)
    events.on(APPLICATION_COMPLETE, _on_application_complete)
    events.on(PLATFORM_ERROR, _on_platform_error)
    events.on(CAPTCHA_DETECTED, _on_captcha_detected)
    events.on(EVOLUTION_TRIGGERS, _on_evolution_triggers)
    events.on(RUN_FINISHED, _on_run_finished)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Auto Applier v2 -- AI-powered job application automation."""
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Don't submit applications")
@click.option("--platform", default=None, help="Run only on a specific platform")
@click.option("--limit", default=0, type=int, help="Max applications this session")
def run(dry_run, platform, limit):
    """Run the job application pipeline."""
    config = load_user_config()
    config["dry_run"] = dry_run

    if platform:
        config["enabled_platforms"] = [platform]

    if limit > 0:
        config["max_applications_per_day"] = limit

    from auto_applier.orchestrator.events import EventEmitter
    from auto_applier.orchestrator.engine import ApplicationEngine

    events = EventEmitter()
    _attach_cli_handlers(events)

    engine = ApplicationEngine(config, events, cli_mode=True)
    asyncio.run(engine.run())


@cli.command()
def status():
    """Show application statistics."""
    from auto_applier.storage.models import Application, Job
    from auto_applier.storage.repository import load_all

    apps = load_all(Application)
    jobs = load_all(Job)

    click.echo(f"\nJobs found: {len(jobs)}")
    click.echo(f"Applications: {len(apps)}")

    # Status breakdown
    status_counts = Counter(a.status for a in apps)
    for s, count in status_counts.most_common():
        click.echo(f"  {s}: {count}")

    # Source breakdown
    source_counts = Counter(a.source for a in apps if a.source)
    if source_counts:
        click.echo("\nBy platform:")
        for source, count in source_counts.most_common():
            click.echo(f"  {source}: {count}")

    # Resume usage
    resume_counts = Counter(a.resume_used for a in apps if a.resume_used)
    if resume_counts:
        click.echo("\nResume usage:")
        for resume, count in resume_counts.most_common():
            click.echo(f"  {resume}: {count}")


@cli.command()
def gaps():
    """Show skill gap analysis."""
    from auto_applier.resume.evolution import EvolutionEngine

    engine = EvolutionEngine()
    summary = engine.get_gap_summary()

    if not summary:
        click.echo("No skill gaps recorded yet.")
        return

    click.echo(f"\nTop skill gaps (out of {len(summary)} total):\n")
    for label, count, category in summary[:30]:
        click.echo(f"  {count:3d}x  [{category:13s}]  {label}")

    triggers = engine.check_triggers()
    if triggers:
        click.echo(f"\n{len(triggers)} skills ready for resume evolution:")
        for t in triggers:
            click.echo(f"  -> {t.skill_name} (seen {t.times_seen}x)")


@cli.command()
def resumes():
    """List loaded resumes."""
    from auto_applier.config import PROFILES_DIR

    profiles = sorted(PROFILES_DIR.glob("*.json"))
    if not profiles:
        click.echo("No resumes loaded. Use the GUI wizard to add resumes.")
        return

    click.echo(f"\nLoaded resumes ({len(profiles)}):\n")
    for p in profiles:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            skills = len(data.get("skills", [])) + len(data.get("tools", []))
            confirmed = len(data.get("confirmed_skills", []))
            source = data.get("source_file", "")
            label = data.get("label", p.stem)
            click.echo(
                f"  {label:20s}  {skills} skills, {confirmed} confirmed  ({source})"
            )
        except Exception:
            click.echo(f"  {p.stem:20s}  (error reading profile)")
