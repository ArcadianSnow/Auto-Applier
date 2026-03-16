"""Generate human-readable reports from application data."""

import click

from auto_applier.analysis.gap_tracker import get_gap_summary
from auto_applier.storage import repository
from auto_applier.storage.models import Application, Job


def print_status_report() -> None:
    """Print a summary of all job applications."""
    jobs = {j.job_id: j for j in repository.load_all(Job)}
    applications = repository.load_all(Application)

    if not applications:
        click.echo("No applications yet. Run 'auto-applier run' to start applying.")
        return

    click.echo(f"\n{'='*60}")
    click.echo(f" Application Status Report  ({len(applications)} total)")
    click.echo(f"{'='*60}\n")

    # Group by status
    by_status: dict[str, list] = {}
    for app in applications:
        by_status.setdefault(app.status, []).append(app)

    for status, apps in by_status.items():
        click.echo(f"  {status.upper()}: {len(apps)}")

    click.echo(f"\n{'-'*60}")
    click.echo(f"  {'Title':<30} {'Company':<20} {'Status':<10}")
    click.echo(f"  {'-'*28}   {'-'*18}   {'-'*8}")

    for app in applications[-20:]:  # Show last 20
        job = jobs.get(app.job_id)
        title = (job.title[:28] if job else "Unknown")
        company = (job.company[:18] if job else "Unknown")
        click.echo(f"  {title:<30} {company:<20} {app.status:<10}")

    if len(applications) > 20:
        click.echo(f"\n  ... and {len(applications) - 20} more. Check data/applications.csv for full list.")

    click.echo()


def print_gaps_report() -> None:
    """Print a summary of skills gaps found across all applications."""
    gaps = get_gap_summary()

    if not gaps:
        click.echo("No skills gaps recorded yet. Gaps are tracked as you apply to jobs.")
        return

    click.echo(f"\n{'='*60}")
    click.echo(f" Skills Gap Report")
    click.echo(f" These are things jobs asked for that your resume may be missing.")
    click.echo(f"{'='*60}\n")

    click.echo(f"  {'Skill/Question':<35} {'Times Asked':<12} {'Category'}")
    click.echo(f"  {'-'*33}   {'-'*10}   {'-'*12}")

    for label, count, category in gaps[:30]:  # Top 30
        display_label = label[:33] if len(label) > 33 else label
        click.echo(f"  {display_label:<35} {count:<12} {category}")

    click.echo(f"\n  Total unique gaps: {len(gaps)}")
    click.echo(f"  Full data available in data/skill_gaps.csv")
    click.echo()
