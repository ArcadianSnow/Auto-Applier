"""CLI reporting for application status and skill gaps."""
import json
from collections import Counter

import click

from auto_applier.config import PROFILES_DIR
from auto_applier.storage.models import Application, Job, SkillGap
from auto_applier.storage.repository import load_all
from auto_applier.resume.evolution import EvolutionEngine


def print_status_report() -> None:
    """Print formatted application statistics."""
    apps = load_all(Application)
    jobs = load_all(Job)

    click.echo(f"\n{'='*50}")
    click.echo("  Auto Applier v2 — Status Report")
    click.echo(f"{'='*50}\n")

    click.echo(f"  Jobs discovered:  {len(jobs)}")
    click.echo(f"  Applications:     {len(apps)}\n")

    # Status breakdown
    status_counts = Counter(a.status for a in apps)
    for status in ["applied", "dry_run", "skipped", "failed"]:
        count = status_counts.get(status, 0)
        click.echo(f"    {status:12s}  {count}")

    # Platform breakdown
    platform_counts = Counter(a.source for a in apps if a.source)
    if platform_counts:
        click.echo(f"\n  By platform:")
        for platform, count in platform_counts.most_common():
            click.echo(f"    {platform:15s}  {count}")

    # Resume usage
    resume_counts = Counter(a.resume_used for a in apps if a.resume_used)
    if resume_counts:
        click.echo(f"\n  Resume usage:")
        for resume, count in resume_counts.most_common():
            click.echo(f"    {resume:15s}  {count}")

    # Score distribution
    scored = [a for a in apps if a.score > 0]
    if scored:
        avg = sum(a.score for a in scored) / len(scored)
        click.echo(f"\n  Average score:    {avg:.1f}/10")

    # Cover letters
    cl_count = sum(1 for a in apps if a.cover_letter_generated)
    if cl_count:
        click.echo(f"  Cover letters:    {cl_count}")

    # LLM usage
    llm_count = sum(1 for a in apps if a.used_llm)
    if llm_count:
        click.echo(f"  Used AI:          {llm_count}")

    # Last 10 applications
    if apps:
        click.echo(f"\n  Last 10 applications:")
        for app in apps[-10:]:
            job = None
            for j in jobs:
                if j.job_id == app.job_id:
                    job = j
                    break
            title = job.title[:30] if job else app.job_id[:30]
            company = job.company[:15] if job else ""
            click.echo(f"    [{app.score:2d}] {app.status:8s}  {title:30s}  {company:15s}  ({app.resume_used})")

    click.echo()


def print_gaps_report() -> None:
    """Print skill gap analysis with evolution triggers."""
    engine = EvolutionEngine()
    summary = engine.get_gap_summary()

    if not summary:
        click.echo("\nNo skill gaps recorded yet. Run some applications first.")
        return

    click.echo(f"\n{'='*50}")
    click.echo("  Skill Gap Analysis")
    click.echo(f"{'='*50}\n")

    click.echo(f"  Total unique gaps: {len(summary)}\n")
    click.echo(f"  {'Count':>5s}  {'Category':13s}  Skill")
    click.echo(f"  {'─'*5}  {'─'*13}  {'─'*30}")

    for label, count, category in summary[:30]:
        click.echo(f"  {count:5d}  {category:13s}  {label}")

    if len(summary) > 30:
        click.echo(f"\n  ... and {len(summary) - 30} more (check data/skill_gaps.csv)")

    # Show evolution triggers
    triggers = engine.check_triggers()
    if triggers:
        click.echo(f"\n  Ready for resume evolution ({len(triggers)} skills):")
        for t in triggers:
            click.echo(f"    -> {t.skill_name} (seen {t.times_seen}x, resume: {t.resume_label or 'any'})")
        click.echo(f"\n  Run the GUI wizard to confirm these skills and update your resume.")

    click.echo()
