"""CLI entry point for Auto Applier."""

import asyncio
import json
import os
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


def _build_platform_config(config: dict) -> dict:
    """Merge user config with .env credentials into platform-ready config.

    The run loop passes this merged dict to each platform adapter.
    Platform adapters read credentials from config['platforms'][source_id].
    """
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    merged = dict(config)
    platforms = merged.setdefault("platforms", {})

    for key in merged.get("enabled_platforms", ["linkedin"]):
        prefix = key.upper()
        entry = platforms.setdefault(key, {})
        # .env overrides config for passwords (passwords are never in config JSON)
        entry["email"] = os.getenv(f"{prefix}_EMAIL", entry.get("email", ""))
        entry["password"] = os.getenv(f"{prefix}_PASSWORD", "")

    return merged


@click.group()
def cli():
    """Auto Applier - Automated job application tool."""
    pass


@cli.command()
def configure():
    """Set up credentials, resume, and personal info."""
    config = load_user_config()

    click.echo("=== Auto Applier Setup ===\n")

    # Platform selection
    click.echo("Available platforms: linkedin, indeed, dice, ziprecruiter")
    platforms_input = click.prompt(
        "Enable platforms (comma-separated)",
        default=",".join(config.get("enabled_platforms", ["linkedin"])),
    )
    enabled = [p.strip().lower() for p in platforms_input.split(",") if p.strip()]
    config["enabled_platforms"] = enabled

    platforms_config = config.setdefault("platforms", {})
    env_lines = []

    for key in enabled:
        click.echo(f"\n--- {key.title()} Credentials ---")
        existing = platforms_config.get(key, {})
        email = click.prompt(f"{key.title()} email", default=existing.get("email", ""))
        password = click.prompt(f"{key.title()} password", hide_input=True)
        platforms_config[key] = {"email": email}
        env_lines.append(f"{key.upper()}_EMAIL={email}")
        env_lines.append(f"{key.upper()}_PASSWORD={password}")

    env_path = PROJECT_ROOT / ".env"
    with open(env_path, "w") as f:
        f.write("\n".join(env_lines) + "\n")
    click.echo(f"\nCredentials saved to {env_path}")

    # Resume upload
    click.echo("\nResume:")
    resume_path = click.prompt(
        "Path to your resume (PDF or DOCX)",
        default=config.get("resume_path", ""),
    )
    resume_path = Path(resume_path).expanduser().resolve()
    if resume_path.exists():
        dest = RESUMES_DIR / resume_path.name
        shutil.copy2(resume_path, dest)
        config["resume_path"] = str(dest)
        click.echo(f"Resume copied to {dest}")
    else:
        click.echo(f"Warning: File not found at {resume_path}")

    # Personal info
    click.echo("\nPersonal Info:")
    config["first_name"] = click.prompt("First name", default=config.get("first_name", ""))
    config["last_name"] = click.prompt("Last name", default=config.get("last_name", ""))
    config["phone"] = click.prompt("Phone number", default=config.get("phone", ""))
    config["city"] = click.prompt("City", default=config.get("city", ""))
    config["linkedin"] = click.prompt("LinkedIn URL", default=config.get("linkedin", ""))
    config["website"] = click.prompt("Website (optional)", default=config.get("website", ""))

    # Search preferences
    click.echo("\nJob Search Preferences:")
    keywords_input = click.prompt(
        "Job titles/keywords (comma-separated)",
        default=",".join(config.get("search_keywords", [])),
    )
    config["search_keywords"] = [k.strip() for k in keywords_input.split(",") if k.strip()]
    config["location"] = click.prompt("Preferred location", default=config.get("location", ""))

    save_user_config(config)
    click.echo(f"\nConfiguration saved to {USER_CONFIG_FILE}")
    click.echo("Run 'auto-applier run' to start applying!")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Walk through without submitting.")
@click.option("--limit", default=0, help="Max applications this session (0 = daily limit).")
@click.option("--platform", default="", help="Run only a specific platform (e.g. 'linkedin').")
def run(dry_run: bool, limit: int, platform: str):
    """Search for jobs and auto-apply across all enabled platforms."""
    config = load_user_config()

    if not config.get("search_keywords"):
        click.echo("No search keywords configured. Run 'auto-applier configure' first.")
        return

    resume_path = config.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        click.echo("No resume found. Run 'auto-applier configure' first.")
        return

    if platform:
        config["enabled_platforms"] = [platform]

    if dry_run:
        click.echo("[DRY RUN MODE] Will not submit any applications.\n")

    asyncio.run(_run_applications(config, dry_run, limit))


async def _run_applications(config: dict, dry_run: bool, limit: int):
    """Core application loop — iterates over enabled platforms."""
    from auto_applier.analysis.gap_tracker import record_gaps, record_skills_gaps_from_description
    from auto_applier.browser.anti_detect import random_delay
    from auto_applier.browser.platforms import PLATFORM_REGISTRY
    from auto_applier.browser.session import BrowserSession
    from auto_applier.config import MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
    from auto_applier.resume.parser import extract_text
    from auto_applier.resume.skills import extract_skills
    from auto_applier.storage import repository
    from auto_applier.storage.models import Application

    merged_config = _build_platform_config(config)

    # Parse resume
    resume_text = extract_text(Path(config["resume_path"]))
    resume_skills = extract_skills(resume_text)
    click.echo(f"Parsed resume: found {len(resume_skills)} known skills.")

    # Check daily limit
    todays_count = repository.get_todays_application_count()
    max_today = limit if limit > 0 else MAX_APPLICATIONS_PER_DAY
    remaining = max_today - todays_count

    if remaining <= 0:
        click.echo(f"Already applied to {todays_count} jobs today (limit: {max_today}).")
        return

    click.echo(f"Daily budget: {remaining} applications remaining ({todays_count} done today).")

    enabled = merged_config.get("enabled_platforms", ["linkedin"])
    click.echo(f"Platforms: {', '.join(enabled)}\n")

    session = BrowserSession()
    try:
        context = await session.start()
        applied_count = 0

        for platform_key in enabled:
            if applied_count >= remaining:
                click.echo("Reached daily application limit.")
                break

            PlatformClass = PLATFORM_REGISTRY.get(platform_key)
            if not PlatformClass:
                click.echo(f"Unknown platform '{platform_key}', skipping.")
                continue

            platform = PlatformClass(context, merged_config)
            click.echo(f"\n{'='*50}")
            click.echo(f" {platform.name}")
            click.echo(f"{'='*50}")

            if not await platform.ensure_logged_in():
                click.echo(f"  Could not log in to {platform.name}. Skipping.")
                continue

            for keyword in config.get("search_keywords", []):
                if applied_count >= remaining:
                    break

                jobs = await platform.search_jobs(keyword, config.get("location", ""))

                for job in jobs:
                    if applied_count >= remaining:
                        break
                    if repository.job_already_applied(job.job_id, platform.source_id):
                        continue

                    click.echo(f"\n  --- {job.title} at {job.company} ---")

                    description = await platform.get_job_description(job)
                    job.description = description
                    repository.save(job)

                    record_skills_gaps_from_description(
                        job.job_id, description, resume_skills,
                    )

                    success, form_gaps = await platform.apply_to_job(job, dry_run=dry_run)

                    if form_gaps:
                        record_gaps(job.job_id, form_gaps)

                    status = "dry_run" if dry_run else ("applied" if success else "failed")
                    repository.save(Application(
                        job_id=job.job_id,
                        status=status,
                        source=platform.source_id,
                        failure_reason="" if success else "Application failed",
                    ))

                    applied_count += 1
                    click.echo(f"    Status: {status} ({applied_count}/{remaining})")

                    await random_delay(
                        MIN_DELAY_BETWEEN_APPLICATIONS,
                        MAX_DELAY_BETWEEN_APPLICATIONS,
                    )

        click.echo(f"\nSession complete. Applied to {applied_count} jobs across {len(enabled)} platform(s).")
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
    """Show skills gap report."""
    from auto_applier.analysis.report import print_gaps_report
    print_gaps_report()


if __name__ == "__main__":
    cli()
