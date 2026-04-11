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
def doctor():
    """Run preflight checks — verify everything is ready to run."""
    from auto_applier import doctor as doctor_module
    import sys as _sys
    _sys.exit(doctor_module.run())


@cli.group()
def archetype():
    """Manage job archetypes for resume routing."""
    pass


@archetype.command("list")
def archetype_list():
    """Show defined archetypes."""
    from auto_applier.resume.archetypes import load_archetypes

    archs = load_archetypes()
    if not archs:
        click.echo(
            "No archetypes defined. Routing disabled — every job will "
            "score against all resumes.\n\n"
            "Create data/archetypes.json with entries like:\n"
            '  {"archetypes": [{"name": "data_analyst", '
            '"description": "SQL, dashboards, reporting"}]}'
        )
        return

    click.echo(f"\n{len(archs)} archetype(s):\n")
    for a in archs:
        click.echo(f"  {a.name}")
        if a.description:
            click.echo(f"      {a.description}")
        if a.keywords:
            click.echo(f"      keywords: {', '.join(a.keywords)}")


@archetype.command("add")
@click.argument("name")
@click.option("--description", "-d", default="", help="Short description of the role family")
@click.option("--keyword", "-k", multiple=True, help="Keyword hint (repeatable)")
def archetype_add(name: str, description: str, keyword: tuple):
    """Add a new archetype definition."""
    from auto_applier.resume.archetypes import (
        Archetype, load_archetypes, save_archetypes,
    )

    archs = load_archetypes()
    if any(a.name == name for a in archs):
        click.echo(f"Archetype '{name}' already exists. Remove it first to update.")
        return
    archs.append(Archetype(
        name=name, description=description, keywords=list(keyword),
    ))
    save_archetypes(archs)
    click.echo(f"Added archetype '{name}'.")


@archetype.command("remove")
@click.argument("name")
def archetype_remove(name: str):
    """Remove an archetype definition."""
    from auto_applier.resume.archetypes import load_archetypes, save_archetypes

    archs = load_archetypes()
    remaining = [a for a in archs if a.name != name]
    if len(remaining) == len(archs):
        click.echo(f"Archetype '{name}' not found.")
        return
    save_archetypes(remaining)
    click.echo(f"Removed archetype '{name}'.")


@cli.group()
def followup():
    """Manage follow-up reminders for submitted applications."""
    pass


@followup.command("list")
@click.option("--all", "show_all", is_flag=True, help="Show completed and dismissed too")
@click.option("--due", is_flag=True, help="Only show items due today or overdue")
def followup_list(show_all: bool, due: bool):
    """List follow-up reminders."""
    from auto_applier.storage.repository import list_followups, get_due_followups
    from datetime import date

    if due:
        items = get_due_followups()
    elif show_all:
        items = list_followups()
    else:
        items = list_followups(status="pending")

    if not items:
        click.echo("No follow-ups to show.")
        return

    today = date.today().isoformat()
    click.echo(f"\n{len(items)} follow-up(s):\n")
    for f in sorted(items, key=lambda x: x.due_date):
        marker = " (OVERDUE)" if f.status == "pending" and f.due_date < today else ""
        marker = " (DUE TODAY)" if f.status == "pending" and f.due_date == today else marker
        click.echo(
            f"  {f.due_date}  [{f.status:9s}]  {f.source:12s}  "
            f"{f.job_id}{marker}"
        )
        if f.notes:
            click.echo(f"              notes: {f.notes}")


@followup.command("done")
@click.argument("job_id")
@click.option("--source", default=None, help="Narrow to a specific platform")
def followup_done(job_id: str, source: str):
    """Mark follow-ups for a job as done."""
    from auto_applier.storage.repository import update_followups_for_job

    n = update_followups_for_job(job_id, "done", source=source)
    click.echo(f"Marked {n} follow-up(s) as done for job {job_id}.")


@followup.command("dismiss")
@click.argument("job_id")
@click.option("--source", default=None, help="Narrow to a specific platform")
def followup_dismiss(job_id: str, source: str):
    """Dismiss follow-ups for a job (stop reminding)."""
    from auto_applier.storage.repository import update_followups_for_job

    n = update_followups_for_job(job_id, "dismissed", source=source)
    click.echo(f"Dismissed {n} follow-up(s) for job {job_id}.")


@cli.command()
def migrations():
    """Show CSV schema migration history."""
    from auto_applier.storage.migrations import list_migration_history
    from auto_applier.storage.repository import _CSV_MAP, _ensure_csv

    # Touch each CSV to trigger any pending migration before listing.
    for model_type, path in _CSV_MAP.items():
        try:
            _ensure_csv(path, model_type)
        except Exception as e:
            click.echo(f"  warn: {path.name}: {e}")

    history = list_migration_history()
    if not history:
        click.echo("No schema migrations recorded. Current schema is the baseline.")
        return

    click.echo(f"\nSchema migration history ({len(history)} entries):\n")
    for rec in history[-20:]:
        ts = rec.get("timestamp", "?")
        model = rec.get("model", "?")
        added = rec.get("added", [])
        removed = rec.get("removed", [])
        rows = rec.get("rows_migrated", 0)
        click.echo(f"  {ts}  {model}  (+{len(added)} -{len(removed)}, {rows} rows)")
        if added:
            click.echo(f"      added:   {', '.join(added)}")
        if removed:
            click.echo(f"      removed: {', '.join(removed)}")
        click.echo(f"      backup:  {rec.get('backup', '?')}")


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
