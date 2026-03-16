"""CLI entry point for Auto Applier."""

import asyncio
import json
import shutil
from pathlib import Path

import click

from auto_applier.config import (
    DATA_DIR,
    MAX_APPLICATIONS_PER_DAY,
    PROJECT_ROOT,
    RESUMES_DIR,
    USER_CONFIG_FILE,
)


def load_user_config() -> dict:
    """Load user config from JSON file."""
    if USER_CONFIG_FILE.exists():
        with open(USER_CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_user_config(config: dict) -> None:
    """Save user config to JSON file."""
    with open(USER_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


@click.group()
def cli():
    """Auto Applier - Automated LinkedIn job application tool."""
    pass


@cli.command()
def configure():
    """Set up LinkedIn credentials, resume, and personal info."""
    config = load_user_config()

    click.echo("=== Auto Applier Setup ===\n")

    # LinkedIn credentials → .env file
    click.echo("LinkedIn Credentials:")
    click.echo("(These are saved to .env and never committed to git)\n")

    email = click.prompt("LinkedIn email", default=config.get("email", ""))
    password = click.prompt("LinkedIn password", hide_input=True)

    env_path = PROJECT_ROOT / ".env"
    with open(env_path, "w") as f:
        f.write(f"LINKEDIN_EMAIL={email}\n")
        f.write(f"LINKEDIN_PASSWORD={password}\n")
    click.echo(f"Credentials saved to {env_path}\n")

    # Resume upload
    click.echo("Resume:")
    resume_path = click.prompt(
        "Path to your resume (PDF or DOCX)",
        default=config.get("resume_path", ""),
    )
    resume_path = Path(resume_path).expanduser().resolve()

    if resume_path.exists():
        dest = RESUMES_DIR / resume_path.name
        shutil.copy2(resume_path, dest)
        config["resume_path"] = str(dest)
        click.echo(f"Resume copied to {dest}\n")
    else:
        click.echo(f"Warning: File not found at {resume_path}. Skipping.\n")

    # Personal info for form filling
    click.echo("Personal Info (used to fill Easy Apply forms):")
    config["email"] = email
    config["first_name"] = click.prompt("First name", default=config.get("first_name", ""))
    config["last_name"] = click.prompt("Last name", default=config.get("last_name", ""))
    config["phone"] = click.prompt("Phone number", default=config.get("phone", ""))
    config["city"] = click.prompt("City", default=config.get("city", ""))
    config["linkedin"] = click.prompt(
        "LinkedIn profile URL", default=config.get("linkedin", "")
    )
    config["website"] = click.prompt(
        "Website/Portfolio URL (optional)",
        default=config.get("website", ""),
    )

    # Job search preferences
    click.echo("\nJob Search Preferences:")
    keywords_input = click.prompt(
        "Job titles/keywords (comma-separated)",
        default=",".join(config.get("search_keywords", [])),
    )
    config["search_keywords"] = [k.strip() for k in keywords_input.split(",") if k.strip()]

    config["location"] = click.prompt(
        "Preferred location (or 'remote')",
        default=config.get("location", ""),
    )

    save_user_config(config)
    click.echo(f"\nConfiguration saved to {USER_CONFIG_FILE}")
    click.echo("Run 'auto-applier run' to start applying!")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Walk through the flow without submitting applications.")
@click.option("--limit", default=0, help="Max applications this session (0 = use daily limit).")
def run(dry_run: bool, limit: int):
    """Search for jobs and auto-apply on LinkedIn."""
    config = load_user_config()

    if not config.get("search_keywords"):
        click.echo("No search keywords configured. Run 'auto-applier configure' first.")
        return

    resume_path = config.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        click.echo("No resume found. Run 'auto-applier configure' first.")
        return

    if dry_run:
        click.echo("[DRY RUN MODE] Will not submit any applications.\n")

    asyncio.run(_run_applications(config, dry_run, limit))


async def _run_applications(config: dict, dry_run: bool, limit: int):
    """Core application loop."""
    from auto_applier.analysis.gap_tracker import record_gaps, record_skills_gaps_from_description
    from auto_applier.browser.anti_detect import random_delay
    from auto_applier.browser.easy_apply import click_easy_apply, fill_application_modal
    from auto_applier.browser.job_search import get_job_description, search_jobs
    from auto_applier.browser.linkedin_auth import ensure_logged_in
    from auto_applier.browser.session import BrowserSession
    from auto_applier.config import MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
    from auto_applier.resume.parser import extract_text
    from auto_applier.resume.skills import extract_skills
    from auto_applier.storage import repository
    from auto_applier.storage.models import Application, Job

    # Parse resume
    resume_text = extract_text(Path(config["resume_path"]))
    resume_skills = extract_skills(resume_text)
    click.echo(f"Parsed resume: found {len(resume_skills)} known skills.")

    # Check daily limit
    todays_count = repository.get_todays_application_count()
    max_today = limit if limit > 0 else MAX_APPLICATIONS_PER_DAY
    remaining = max_today - todays_count

    if remaining <= 0:
        click.echo(f"Already applied to {todays_count} jobs today (limit: {max_today}). Try again tomorrow.")
        return

    click.echo(f"Daily budget: {remaining} applications remaining ({todays_count} done today).\n")

    # Start browser
    session = BrowserSession()
    try:
        context = await session.start()
        page = await ensure_logged_in(context)

        applied_count = 0

        for keyword in config.get("search_keywords", []):
            if applied_count >= remaining:
                click.echo("Reached daily application limit. Stopping.")
                break

            jobs = await search_jobs(page, keyword, config.get("location", ""))

            for job in jobs:
                if applied_count >= remaining:
                    break

                if repository.job_already_applied(job.job_id):
                    continue

                click.echo(f"\n--- {job.title} at {job.company} ---")

                # Get full job description for gap analysis
                description = await get_job_description(page, job.url)
                job.description = description
                repository.save(job)

                # Record skills gaps from description
                record_skills_gaps_from_description(job.job_id, description, resume_skills)

                # Try Easy Apply
                if not await click_easy_apply(page):
                    repository.save(Application(job_id=job.job_id, status="skipped", failure_reason="No Easy Apply button"))
                    continue

                success, form_gaps = await fill_application_modal(page, config, dry_run=dry_run)

                if form_gaps:
                    record_gaps(job.job_id, form_gaps)

                status = "dry_run" if dry_run else ("applied" if success else "failed")
                failure_reason = "" if success else "Modal completion failed"
                repository.save(Application(job_id=job.job_id, status=status, failure_reason=failure_reason))

                applied_count += 1
                click.echo(f"  Status: {status} ({applied_count}/{remaining})")

                # Wait between applications
                await random_delay(MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS)

        click.echo(f"\nSession complete. Applied to {applied_count} jobs.")
        click.echo("Run 'auto-applier gaps' to see skills gap report.")

    except Exception as e:
        click.echo(f"\nError: {e}")
        raise
    finally:
        await session.close()


@cli.command()
def status():
    """Show applied jobs and their statuses."""
    from auto_applier.analysis.report import print_status_report
    print_status_report()


@cli.command()
def gaps():
    """Show skills gap report — what jobs are asking for that you might be missing."""
    from auto_applier.analysis.report import print_gaps_report
    print_gaps_report()


if __name__ == "__main__":
    cli()
