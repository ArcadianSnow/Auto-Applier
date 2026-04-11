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


@cli.command()
def patterns():
    """Surface conversion patterns from the application history."""
    from auto_applier.analysis.patterns import analyze

    report = analyze()
    if report.total_applications == 0:
        click.echo("No applications recorded yet — nothing to analyze.")
        return

    click.echo(
        f"\nApplication history ({report.total_applications} total):\n"
        f"  applied:  {report.applied}\n"
        f"  failed:   {report.failed}\n"
        f"  skipped:  {report.skipped}\n"
        f"  dry-run:  {report.dry_run}"
    )

    def _section(title: str, rows: list, format_key=lambda k: str(k)):
        if not rows:
            return
        click.echo(f"\n{title}:")
        click.echo(f"  {'key':24s}  rate    applied/total")
        for key, applied, total, rate in rows[:10]:
            pct = f"{rate*100:5.1f}%"
            click.echo(f"  {format_key(key):24s}  {pct}   {applied}/{total}")

    _section("By resume", report.resume_stats)
    _section("By platform", report.platform_stats)
    _section("By search keyword", report.keyword_stats)
    _section("By score bucket", report.score_buckets)
    _section("By hour of day", report.hour_stats, format_key=lambda h: f"{h:02d}:00")
    _section("By day of week", report.dow_stats)

    if report.dead_listing_sources:
        click.echo("\nDead listings by platform:")
        for source, n in report.dead_listing_sources:
            click.echo(f"  {source:24s}  {n}")

    if report.top_gaps:
        click.echo("\nMost-asked unknown fields (skill gaps):")
        for field, count in report.top_gaps[:10]:
            click.echo(f"  {count:4d}x  {field}")


@cli.command()
def fsck():
    """Check CSV data for integrity issues (read-only)."""
    from auto_applier.storage.integrity import fsck as run_fsck

    report = run_fsck()
    click.echo(
        f"\nData health check:\n"
        f"  {report['jobs']:5d} jobs\n"
        f"  {report['applications']:5d} applications\n"
        f"  {report['skill_gaps']:5d} skill gaps\n"
        f"  {report['followups']:5d} follow-ups"
    )
    if report["healthy"]:
        click.echo("\n  No issues found.")
        return
    click.echo(f"\n  {len(report['issues'])} issue(s):")
    for issue in report["issues"][:50]:
        click.echo(f"    - {issue}")
    if len(report["issues"]) > 50:
        click.echo(f"    ... and {len(report['issues']) - 50} more")
    click.echo("\nRun `auto-applier normalize` to auto-fix alias statuses and duplicates.")


@cli.command()
@click.confirmation_option(prompt="This will rewrite CSV files in place (backups created). Continue?")
def normalize():
    """Repair common CSV inconsistencies in place (creates backups)."""
    from auto_applier.storage.integrity import normalize as run_normalize

    changes = run_normalize()
    click.echo("\nNormalization complete:")
    for k, v in changes.items():
        if k == "total":
            continue
        click.echo(f"  {k:32s} {v}")
    if changes["total"] == 0:
        click.echo("\nNothing to fix — data already clean.")
    else:
        click.echo(f"\nTotal changes: {changes['total']}")
        click.echo("Backups written to data/.backups/")


@cli.group()
def story():
    """Manage the STAR+Reflection interview story bank."""
    pass


@story.command("list")
@click.option("--company", default=None, help="Filter by company name")
def story_list(company):
    """List stories in the bank (titles + question prompts)."""
    from auto_applier.resume.story_bank import load_bank

    bank = load_bank()
    if company:
        bank = [s for s in bank if company.lower() in s.company.lower()]
    if not bank:
        click.echo("Story bank is empty.")
        return
    click.echo(f"\n{len(bank)} story/stories:\n")
    for i, s in enumerate(bank, 1):
        click.echo(f"  {i:3d}. {s.title}")
        if s.question_prompt:
            click.echo(f"       Q: {s.question_prompt}")
        if s.company:
            click.echo(f"       from: {s.job_title} @ {s.company}")


@story.command("show")
@click.argument("index", type=int)
def story_show(index: int):
    """Show the full text of a single story (1-indexed)."""
    from auto_applier.resume.story_bank import load_bank

    bank = load_bank()
    if not 1 <= index <= len(bank):
        click.echo(f"Index out of range (bank has {len(bank)} stories).")
        return
    s = bank[index - 1]
    click.echo(f"\n{s.title}")
    click.echo("=" * len(s.title))
    if s.question_prompt:
        click.echo(f"Answers: {s.question_prompt}\n")
    if s.company or s.job_title:
        click.echo(f"From: {s.job_title} @ {s.company}\n")
    click.echo(f"Situation:  {s.situation}\n")
    click.echo(f"Task:       {s.task}\n")
    click.echo(f"Action:     {s.action}\n")
    click.echo(f"Result:     {s.result}\n")
    click.echo(f"Reflection: {s.reflection}")


@story.command("export")
@click.option("--output", "-o", default="story_bank.md", help="Output file path")
def story_export(output: str):
    """Export the full story bank as a markdown document."""
    from auto_applier.resume.story_bank import export_bank_markdown
    from pathlib import Path

    content = export_bank_markdown()
    Path(output).write_text(content, encoding="utf-8")
    click.echo(f"Exported to {output}")


@story.command("prune")
@click.argument("index", type=int)
def story_prune(index: int):
    """Delete a story from the bank (1-indexed)."""
    from auto_applier.resume.story_bank import load_bank, save_bank

    bank = load_bank()
    if not 1 <= index <= len(bank):
        click.echo(f"Index out of range (bank has {len(bank)} stories).")
        return
    removed = bank.pop(index - 1)
    save_bank(bank)
    click.echo(f"Removed: {removed.title}")


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
