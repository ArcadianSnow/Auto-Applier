"""CLI entry point for Auto Applier v2."""
import asyncio
import json
from collections import Counter

import click

from auto_applier.config import USER_CONFIG_FILE


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
    click.echo(
        f"\n  ┌─ ACTION NEEDED ───────────────────────────────────\n"
        f"  │  Please log in to {name} in the open browser.\n"
        f"  │  If you see a CAPTCHA / verification challenge,\n"
        f"  │  solve it manually — the run resumes automatically\n"
        f"  │  once the login is detected.\n"
        f"  └───────────────────────────────────────────────────"
    )


def _on_platform_login_failed(**kw):
    name = kw.get("platform", "").title()
    click.echo(
        f"\n  Login failed for {name}. The platform has been put in "
        f"a 4-hour cooldown to avoid digging the bot-detection hole "
        f"deeper. Clear it sooner with: --cli unpause "
        f"{kw.get('platform', '')}",
        err=True,
    )


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
    click.echo(
        f"\n  ┌─ CAPTCHA DETECTED ───────────────────────────────\n"
        f"  │  {name.title()} flagged this session as automation.\n"
        f"  │  The platform has been put in a 4-hour cooldown\n"
        f"  │  so the bot-detection fingerprint can cool off.\n"
        f"  │\n"
        f"  │  What you can do:\n"
        f"  │   1. Solve the verification in the open browser\n"
        f"  │      window (this re-warms the cookie).\n"
        f"  │   2. Run dry-run dice + ziprecruiter in the\n"
        f"  │      meantime — they have separate fingerprints.\n"
        f"  │   3. Clear the cooldown sooner with:\n"
        f"  │        python -m auto_applier --cli unpause {name}\n"
        f"  └───────────────────────────────────────────────────",
        err=True,
    )


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
    dry_run = kw.get("dry_run", False)

    click.echo(
        f"\nDone ({reason}). Applied: {applied}, Skipped: {skipped}, Failed: {failed}"
    )


def _on_cycle_started(**kw):
    n = kw.get("cycle_number", 0)
    max_n = kw.get("max_cycles", 0)
    suffix = f" of {max_n}" if max_n else ""
    click.echo(f"\n=== Starting cycle {n}{suffix} ===")


def _on_cycle_idle(**kw):
    secs = kw.get("seconds_until_next", 0)
    mins = max(1, secs // 60)
    refinement_only = kw.get("refinement_only", False)
    candidates = kw.get("refinement_candidates", 0)
    cycle_n = kw.get("cycle_number", 0)

    if refinement_only:
        click.echo(
            f"\n[Cycle {cycle_n}] Outside active hours. Next cycle in ~{mins} "
            "min."
        )
    else:
        click.echo(
            f"\n[Cycle {cycle_n}] Done. Next cycle in ~{mins} min."
        )

    if candidates > 0:
        click.echo(
            f"  TIP: You have {candidates} skill gap(s) ready for review. "
            "Run in another terminal:"
        )
        click.echo("       python -m auto_applier --cli refine")


def _on_continuous_finished(**kw):
    reason = kw.get("reason", "")
    total = kw.get("total_cycles", 0)
    click.echo(
        f"\nContinuous mode finished after {total} cycle(s). {reason}"
    )

    # Plain-English explainer. The raw numbers above can be confusing
    # for a first-time user — they might see "Skipped: 7" and think
    # the tool is broken when actually most skips are perfectly normal
    # (external apply links, dead listings, low-match jobs).
    _print_run_explainer(applied, skipped, failed, dry_run)


def _print_run_explainer(applied: int, skipped: int, failed: int, dry_run: bool) -> None:
    """Novice-friendly summary of what just happened.

    Tells the user: what the counts mean, and what to do next.
    Pulls recent Application records for a "why were these skipped"
    breakdown so the user isn't guessing.

    Wrapped in a broad try/except at the end because EventEmitter
    silently swallows handler exceptions. A bug here would make the
    whole novice summary disappear without any error to investigate.
    """
    try:
        _run_explainer_body(applied, skipped, failed, dry_run)
    except Exception as exc:
        # Surface the error so we can debug, but don't crash the run.
        click.echo(f"\n(explainer error: {exc})", err=True)


def _run_explainer_body(applied: int, skipped: int, failed: int, dry_run: bool) -> None:
    from auto_applier.storage.models import Application
    from auto_applier.storage.repository import load_all
    from collections import Counter
    import datetime

    click.echo("")  # spacer

    # Small helper for plural suffixes
    def _s(n: int) -> str:
        return "s" if n != 1 else ""

    # What the applied number means
    if applied == 0:
        if skipped == 0 and failed == 0:
            click.echo(
                "No jobs were scored this run - try a broader search "
                "(different keywords or location)."
            )
            return
        click.echo("No applications went through this run.")
    else:
        if dry_run:
            click.echo(
                f"[OK] The tool successfully walked through {applied} "
                f"application{_s(applied)} (but did not submit - "
                "this was a test run)."
            )
        else:
            click.echo(
                f"[OK] Submitted {applied} application{_s(applied)}."
            )

    # Breakdown of WHY skips happened — pulled from failure_reason
    # on the most recent skipped/failed Applications.
    if skipped or failed:
        apps = load_all(Application)
        # Just today's records — count per failure reason
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        recent = [
            a for a in apps
            if a.status in ("skipped", "failed")
            and a.applied_at.startswith(today)
        ]
        reason_counts = Counter(
            a.failure_reason or "other"
            for a in recent
        )
        if reason_counts:
            click.echo("")
            click.echo(f"Why jobs were skipped or failed:")
            for reason, count in reason_counts.most_common(5):
                label = reason[:60] if reason else "(no reason recorded)"
                click.echo(f"  {count:3d}  {label}")

    # Hint the user toward `cli almost` if we saw any high-score externals
    try:
        from auto_applier.storage.models import Application as App
        from auto_applier.storage.repository import load_all as _load_all
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        externals = [
            a for a in _load_all(App)
            if a.status == "skipped"
            and a.applied_at.startswith(today)
            and a.score >= 8
            and "company" in (a.failure_reason or "").lower()
        ]
        if externals:
            click.echo("")
            plural = "s" if len(externals) != 1 else ""
            needs = "" if len(externals) != 1 else "s"
            click.echo(
                f"TIP: {len(externals)} high-score job{plural} "
                f"need{needs} manual application on the "
                "company's own website."
            )
            click.echo(
                "     Run: python -m auto_applier --cli almost"
            )
    except Exception:
        pass


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
        CYCLE_STARTED,
        CYCLE_IDLE,
        CONTINUOUS_FINISHED,
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
    events.on(CYCLE_STARTED, _on_cycle_started)
    events.on(CYCLE_IDLE, _on_cycle_idle)
    events.on(CONTINUOUS_FINISHED, _on_continuous_finished)


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
@click.option("--continuous", is_flag=True,
              help="Loop the pipeline on a cadence instead of running once. "
                   "Uses continuous_cycle_delay_min/max from user_config.json.")
@click.option("--max-cycles", default=0, type=int,
              help="Safety cap for --continuous (0 = unlimited).")
@click.option("--active-hours", default=None,
              help="Active-hours window for --continuous (e.g. '09:00-22:00'). "
                   "Outside this window the loop idles — browser stays warm, "
                   "refinement prompts still fire.")
def run(dry_run, platform, limit, continuous, max_cycles, active_hours):
    """Run the job application pipeline."""
    from auto_applier.log_setup import start_run_logging
    log_path = start_run_logging()
    click.echo(f"Debug log: {log_path}")

    config = load_user_config()
    config["dry_run"] = dry_run

    if platform:
        config["enabled_platforms"] = [platform]

    if limit > 0:
        config["max_applications_per_day"] = limit

    # Continuous-mode flags override user_config.json for this run only.
    if continuous:
        config["continuous_mode"] = True
    if max_cycles:
        config["continuous_max_cycles"] = max_cycles
    if active_hours:
        config["continuous_active_hours"] = active_hours

    from auto_applier.orchestrator.events import EventEmitter
    from auto_applier.orchestrator.engine import ApplicationEngine

    events = EventEmitter()
    _attach_cli_handlers(events)

    engine = ApplicationEngine(config, events, cli_mode=True)
    if config.get("continuous_mode"):
        click.echo(
            "Continuous mode: tool will loop between cycles. "
            "Press Ctrl+C to stop, or run `auto-applier refine` in a "
            "separate terminal during idle periods."
        )
        asyncio.run(engine.run_continuous())
    else:
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

    # Outcome breakdown — how are your submitted applications doing?
    from auto_applier.analysis.outcome import outcome_summary
    summary = outcome_summary()
    if summary:
        click.echo("\nWhat happened after you applied:")
        # Display in a meaningful order
        order = ["pending", "acknowledged", "interview", "offer",
                 "rejected", "ghosted", "withdrawn"]
        total = sum(summary.values())
        for key in order:
            count = summary.get(key, 0)
            if count == 0:
                continue
            pct = (count / total * 100) if total else 0
            click.echo(f"  {key:14s}  {count:3d}  ({pct:4.1f}%)")
        click.echo(
            "\nTIP: Update outcomes with: "
            "python -m auto_applier --cli respond <job_id> <outcome>"
        )


@cli.command()
@click.option("--min-score", default=9, type=int,
              help="Minimum score to surface (default 9)")
@click.option("--cover", is_flag=True,
              help="Generate cover letters for all shown jobs")
def almost(min_score, cover):
    """Show high-score jobs you should apply to manually.

    Lists jobs that scored >= min-score but couldn't be auto-applied.
    This includes:
      - LinkedIn jobs (discovery-only — Auto Applier never auto-applies
        on LinkedIn because its anti-automation blocks direct job-page
        navigation)
      - Externally-hosted jobs (company wants you to apply on their own
        site)
      - Jobs the platform blocked mid-flow

    These are jobs worth your time to apply manually.
    """
    from auto_applier.storage.models import Application, Job
    from auto_applier.storage.repository import load_all as _load_all

    apps = _load_all(Application)
    jobs = {j.job_id: j for j in _load_all(Job)}

    # Find skipped applications with preserved scores
    candidates = [
        a for a in apps
        if a.status == "skipped"
        and a.score >= min_score
        and a.resume_used  # must have a resume recorded
    ]

    if not candidates:
        click.echo(
            f"\nNo jobs scoring {min_score}+ were skipped yet.\n"
            "Run some dry-runs or real runs first: "
            "python -m auto_applier --cli run --dry-run"
        )
        return

    # Sort by score descending, then by company for stable display
    candidates.sort(key=lambda a: (-a.score, jobs.get(a.job_id, Job("", "", "", "")).company))

    click.echo(f"\n{'=' * 60}")
    click.echo(f"  Good jobs you should apply to manually")
    click.echo(f"  (scored {min_score}+, but need external application)")
    click.echo(f"{'=' * 60}\n")

    # Group by recommended resume
    from collections import defaultdict
    by_resume: dict[str, list] = defaultdict(list)
    for app in candidates:
        by_resume[app.resume_used].append(app)

    for resume_label, app_list in sorted(by_resume.items()):
        click.echo(f"Use resume: {resume_label}")
        for app in app_list:
            job = jobs.get(app.job_id)
            if not job:
                continue
            click.echo(
                f"  [{app.score:2d}] {job.title[:55]}"
                f"  @ {job.company[:30] if job.company else '(unknown company)'}"
            )
            if job.url:
                click.echo(f"       URL: {job.url}")
            if app.failure_reason:
                click.echo(f"       {app.failure_reason}")
            click.echo("")

    if not cover:
        click.echo(
            "TIP: To generate cover letters for these, run:\n"
            "     python -m auto_applier --cli almost --cover\n"
        )
        return

    # Interactive cover letter generation
    if not click.confirm(
        f"\nGenerate cover letters for all {len(candidates)} job(s)?",
        default=True,
    ):
        return

    asyncio.run(_generate_almost_cover_letters(candidates, jobs))


async def _generate_almost_cover_letters(candidates, jobs) -> None:
    """Generate cover letters for each high-score external job."""
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.cover_letter_service import generate_cover_letter

    router = LLMRouter()
    await router.initialize()
    resume_manager = ResumeManager(router)

    success = 0
    failed = 0
    for app in candidates:
        job = jobs.get(app.job_id)
        if not job:
            continue
        click.echo(f"  Generating cover letter for {job.title} @ {job.company}...")
        try:
            result = await generate_cover_letter(
                job_id=app.job_id,
                router=router,
                resume_manager=resume_manager,
                preferred_resume=app.resume_used,
            )
        except Exception as exc:
            click.echo(f"    [FAIL] {exc}")
            failed += 1
            continue

        if result is None or not result.letter:
            click.echo(f"    [FAIL] Could not generate letter")
            failed += 1
            continue

        click.echo(f"    [OK] Saved to {result.file_path}")
        success += 1

    click.echo(f"\n{success} cover letter(s) saved.")
    if failed:
        click.echo(f"{failed} failed — check logs for details.")
    from auto_applier.config import COVER_LETTERS_DIR
    click.echo(f"Cover letters folder: {COVER_LETTERS_DIR}")


@cli.command()
@click.argument("job_id")
@click.option("--resume", default="",
              help="Override which resume to use (default: auto-pick)")
@click.option("--print-text", is_flag=True,
              help="Also print the letter to terminal for copy-paste")
def cover(job_id, resume, print_text):
    """Generate a cover letter for any job in your history.

    Writes the letter to data/cover_letters/ as a Markdown file.
    Uses the resume that was recorded with the application, or the
    one you pass with --resume.
    """
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.cover_letter_service import generate_cover_letter

    async def run_it():
        router = LLMRouter()
        await router.initialize()
        resume_manager = ResumeManager(router)
        return await generate_cover_letter(
            job_id=job_id,
            router=router,
            resume_manager=resume_manager,
            preferred_resume=resume,
        )

    result = asyncio.run(run_it())

    if result is None:
        click.echo(
            f"No job found with id '{job_id}'.\n"
            "Run `python -m auto_applier --cli show <job_id>` to check.",
            err=True,
        )
        return

    if not result.letter:
        click.echo(
            f"Could not generate a cover letter for {result.job_title} "
            f"@ {result.company}.\n"
            "Check that Ollama is running and a resume is loaded.",
            err=True,
        )
        return

    click.echo(f"\n[OK] Cover letter generated for:")
    click.echo(f"  {result.job_title} @ {result.company}")
    click.echo(f"  Resume used: {result.resume_label}")
    if result.file_path:
        click.echo(f"  Saved to: {result.file_path}")

    if print_text:
        click.echo(f"\n{'-' * 60}")
        click.echo(result.letter)
        click.echo(f"{'-' * 60}\n")


@cli.command()
@click.argument("job_id")
@click.argument("outcome", type=click.Choice([
    "acknowledged", "interview", "rejected", "offer",
    "ghosted", "withdrawn", "pending",
]))
@click.option("--source", default="",
              help="Narrow match to a specific platform (e.g., indeed)")
@click.option("--note", default="",
              help="Optional free-text note")
def respond(job_id, outcome, source, note):
    """Record what happened after you applied to a job.

    Examples:
        python -m auto_applier --cli respond ind-abc123 interview
        python -m auto_applier --cli respond zr-xyz789 rejected --note "generic email"

    Outcomes:
        acknowledged — "thanks for applying" email
        interview    — got an interview invitation
        rejected     — employer said no
        offer        — received a job offer
        ghosted      — no response after many weeks
        withdrawn    — you withdrew your application
        pending      — reset to default (haven't heard back yet)
    """
    from auto_applier.analysis.outcome import set_outcome
    try:
        result = set_outcome(
            job_id=job_id,
            outcome=outcome,
            source=source,
            note=note,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        return

    if result is None:
        click.echo(
            f"No application found for job_id '{job_id}'"
            + (f" on source '{source}'." if source else ".")
        )
        click.echo(
            "Run `python -m auto_applier --cli status` to see your applications."
        )
        return

    # Friendly confirmation messages per outcome
    messages = {
        "interview": "Congrats on the interview! Good luck.",
        "offer": "Congratulations on the offer!",
        "rejected": "Recorded. Don't give up - each 'no' gets you closer.",
        "acknowledged": "Recorded the acknowledgement.",
        "ghosted": "Recorded as ghosted.",
        "withdrawn": "Marked as withdrawn.",
        "pending": "Reset to pending.",
    }
    click.echo(f"\n[OK] Outcome recorded: {outcome}")
    click.echo(f"  Job ID: {job_id}")
    if messages.get(outcome):
        click.echo(f"\n{messages[outcome]}")


@cli.command()
@click.argument("title")
@click.option("--add", is_flag=True,
              help="Add approved titles to user_config.json search_keywords")
@click.option("--no-llm", is_flag=True,
              help="Skip the LLM and use only the static fallback dictionary")
def expand(title, add, no_llm):
    """Suggest adjacent job titles you can also search for.

    A novice user who types "Data Analyst" might not know to also
    search "Business Intelligence Analyst" or "Analytics Engineer".
    This command generates those adjacents — tailored to your resume
    when available — so you can broaden your search without missing
    jobs you're qualified for.

    Examples:
        python -m auto_applier --cli expand "Data Analyst"
        python -m auto_applier --cli expand "Data Analyst" --add
        python -m auto_applier --cli expand "Project Manager" --no-llm

    Use --add to append the approved titles directly to your config.
    """
    from auto_applier.analysis.title_expansion import expand_title
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.manager import ResumeManager

    async def do_expand():
        router = LLMRouter() if not no_llm else None
        if router is not None:
            await router.initialize()

        # Load resume text for context, if any resume is loaded
        resume_text = ""
        if router is not None:
            try:
                mgr = ResumeManager(router)
                resumes = mgr.list_resumes()
                if resumes:
                    # Use the first resume's text for context — fine
                    # because this is advisory, not an applying decision
                    resume_text = mgr.get_resume_text(resumes[0].label)
            except Exception:
                pass  # resume context is optional

        return await expand_title(
            seed=title,
            router=router,
            resume_text=resume_text,
            prefer_llm=not no_llm,
        )

    result = asyncio.run(do_expand())

    if not result.has_suggestions:
        click.echo(
            f"\nNo adjacent titles found for '{title}'.\n"
            "Try a more specific or common title "
            "(e.g., 'Data Analyst' instead of 'DA Guy')."
        )
        return

    click.echo(f"\nAdjacent titles for '{title}':")
    if result.source == "llm":
        click.echo(f"(suggested by AI based on your resume context)\n")
    else:
        click.echo(f"(from the built-in title dictionary)\n")

    for i, adjacent in enumerate(result.adjacents, 1):
        click.echo(f"  {i}. {adjacent}")

    if result.reasoning:
        click.echo(f"\nReasoning: {result.reasoning}")

    if not add:
        click.echo(
            "\nTIP: Use --add to save these to your search_keywords in "
            "user_config.json"
        )
        return

    # Ask which ones to add
    click.echo("")
    approved = []
    for adjacent in result.adjacents:
        if click.confirm(f"  Add '{adjacent}'?", default=True):
            approved.append(adjacent)

    if not approved:
        click.echo("\nNothing added.")
        return

    # Update user_config.json
    config = load_user_config()
    keywords = config.get("search_keywords", [])
    existing_lower = {k.lower().strip() for k in keywords}
    added = []
    for t in approved:
        if t.lower().strip() not in existing_lower:
            keywords.append(t)
            added.append(t)
    config["search_keywords"] = keywords
    save_user_config(config)

    if added:
        click.echo(f"\n[OK] Added {len(added)} title(s) to search_keywords:")
        for t in added:
            click.echo(f"  + {t}")
    else:
        click.echo("\nAll approved titles were already in your search list.")


@cli.command()
@click.option("--max-skills", default=5, type=int,
              help="Max skills to review this session (default 5)")
def refine(max_skills):
    """Walk through missing skills and improve your resume.

    For each recurring skill gap, you can:
      (a) Describe your experience -> LLM drafts resume bullets
      (b) Mark as currently learning (goes into learning goals)
      (c) Dismiss (never suggest again)
      (d) Skip for now

    Approved bullets are added to your resume profile's confirmed
    skills — the scoring pipeline will treat them as "on the resume"
    from the next run onward.

    Important: the LLM is told to use ONLY facts you provide.
    It will NOT invent team sizes, employers, or numbers. If your
    description is vague, you'll get no bullets (and a prompt to
    try again with more detail).
    """
    from auto_applier.analysis import learning_goals
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.evolution import EvolutionEngine
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.refine import (
        check_resume_suggestion,
        collect_refine_candidates,
        generate_bullets,
        save_confirmed_skill,
    )

    asyncio.run(_run_refine_session(max_skills))


async def _run_refine_session(max_skills: int) -> None:
    from auto_applier.analysis import learning_goals
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.evolution import EvolutionEngine
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.refine import (
        check_resume_suggestion,
        collect_refine_candidates,
        generate_bullets,
        save_confirmed_skill,
    )

    router = LLMRouter()
    await router.initialize()
    resume_manager = ResumeManager(router)

    # Resume suggestion check first — if the user has a structural
    # mismatch we should surface that BEFORE walking individual gaps,
    # since building a new resume changes what gaps matter.
    suggestions = check_resume_suggestion()
    for sug in suggestions:
        click.echo(f"\n{'=' * 60}")
        click.echo("Resume suggestion:")
        click.echo(f"{'=' * 60}")
        click.echo(
            f"You've applied to {sug.evidence_count} {sug.target_archetype}-type "
            f"jobs using your '{sug.existing_resume}' resume.\n"
            f"Average match score was {sug.avg_score:.1f}/10 "
            "(below what a tailored resume could get)."
        )
        if sug.example_titles:
            click.echo("\nExample jobs:")
            for t in sug.example_titles:
                click.echo(f"  - {t}")
        click.echo(
            f"\nA resume focused on {sug.target_archetype} roles would "
            "likely score higher and unlock more matches."
        )
        click.echo(
            "\n(Title-focused resume generation is a future feature. "
            "For now, create a new resume file and add it via the "
            "GUI wizard.)"
        )

    # Collect gap candidates
    candidates = collect_refine_candidates()
    if not candidates:
        click.echo(
            "\nNo skills are ready to review yet.\n"
            "Apply to more jobs or wait for more gap data to accumulate."
        )
        return

    # Limit the session size so it doesn't feel like a chore
    candidates = candidates[:max_skills]

    click.echo(f"\n{'=' * 60}")
    click.echo(f"Resume refinement — {len(candidates)} skill(s) to review")
    click.echo(f"{'=' * 60}")
    click.echo(
        "For each skill, tell us if you have experience with it. If you do, "
        "the AI will draft resume bullets using ONLY what you describe — "
        "no made-up details.\n"
    )

    added_count = 0
    learning_count = 0
    dismissed_count = 0
    skipped_count = 0

    evolution = EvolutionEngine()

    for i, cand in enumerate(candidates, 1):
        click.echo(f"\n[{i}/{len(candidates)}] Skill: {cand.skill}")
        click.echo(
            f"  Appeared in {cand.count} jobs you applied to "
            f"(using your '{cand.resume_label}' resume)"
        )
        if cand.sample_companies:
            click.echo(f"  Examples: {', '.join(cand.sample_companies[:3])}")

        click.echo("\nWhat would you like to do?")
        click.echo("  (a) I have experience — let me describe it")
        click.echo("  (b) I'm currently learning this")
        click.echo("  (c) Not interested — don't suggest again")
        click.echo("  (d) Skip for now")
        click.echo("  (q) Quit the session")

        choice = click.prompt("Choice", type=click.Choice(
            ["a", "b", "c", "d", "q"],
        ), default="d").strip().lower()

        if choice == "q":
            click.echo("\nEnding session early.")
            break

        if choice == "d":
            skipped_count += 1
            continue

        if choice == "c":
            learning_goals.set_state(cand.skill, "not_interested")
            evolution.mark_prompted(cand.skill)
            click.echo(f"  [OK] Dismissed '{cand.skill}'.")
            dismissed_count += 1
            continue

        if choice == "b":
            learning_goals.set_state(cand.skill, "learning")
            click.echo(f"  [OK] Added '{cand.skill}' to your learning list.")
            learning_count += 1
            continue

        # choice == "a": have experience
        level = click.prompt(
            "Your level with this skill",
            type=click.Choice(["beginner", "intermediate", "advanced", "expert"]),
            default="intermediate",
        )
        description = click.prompt(
            "Briefly describe a project or role where you used this "
            "(1-2 sentences, just the facts)",
            type=str,
        )

        if not description.strip():
            click.echo("  Empty description — skipping.")
            skipped_count += 1
            continue

        resume_text = resume_manager.get_resume_text(cand.resume_label)
        bullets = await generate_bullets(
            skill=cand.skill,
            user_description=description,
            resume_label=cand.resume_label,
            resume_text=resume_text,
            router=router,
            level=level,
        )

        if not bullets:
            click.echo(
                "  AI couldn't generate solid bullets from that description.\n"
                "  Try again with more specifics (what, where, a concrete "
                "outcome if you can recall one)."
            )
            skipped_count += 1
            continue

        click.echo("\n  Proposed bullets:")
        for b in bullets:
            click.echo(f"    * {b}")

        if not click.confirm("\nAdd these to your resume?", default=True):
            click.echo("  Not added.")
            skipped_count += 1
            continue

        ok = save_confirmed_skill(
            resume_label=cand.resume_label,
            skill=cand.skill,
            level=level,
            bullets=bullets,
            resume_manager=resume_manager,
        )
        if ok:
            evolution.mark_prompted(cand.skill)
            click.echo(
                f"  [OK] Added '{cand.skill}' to '{cand.resume_label}' resume."
            )
            added_count += 1
        else:
            click.echo("  [FAIL] Could not save — resume profile missing.")
            skipped_count += 1

    # Summary
    click.echo(f"\n{'=' * 60}")
    click.echo("Refinement session complete")
    click.echo(f"{'=' * 60}")
    click.echo(f"  Added to resume:        {added_count}")
    click.echo(f"  Added to learning list: {learning_count}")
    click.echo(f"  Dismissed:              {dismissed_count}")
    click.echo(f"  Skipped:                {skipped_count}")
    if added_count:
        click.echo(
            "\nYour next `cli run` will score against the updated resumes."
        )


@cli.group()
def learn():
    """Track skills you're learning or already know.

    Three states:
      learning        — studying this skill now
      certified       — you've completed it; candidate to add to resume
      not_interested  — never suggest this one again
    """
    pass


@learn.command("add")
@click.argument("skill")
def learn_add(skill):
    """Mark a skill as something you're currently learning."""
    from auto_applier.analysis.learning_goals import set_state
    set_state(skill, "learning")
    click.echo(f"[OK] Now tracking '{skill}' as: learning")


@learn.command("done")
@click.argument("skill")
def learn_done(skill):
    """Mark a skill as completed/certified.

    The refine chat will use this to propose adding the skill
    as a confirmed item on your resume profile.
    """
    from auto_applier.analysis.learning_goals import set_state
    set_state(skill, "certified")
    click.echo(
        f"[OK] Marked '{skill}' as certified.\n"
        "     Run `cli refine` to add this to your resume."
    )


@learn.command("dismiss")
@click.argument("skill")
def learn_dismiss(skill):
    """Stop suggesting this skill in gaps/trends reports."""
    from auto_applier.analysis.learning_goals import set_state
    set_state(skill, "not_interested")
    click.echo(f"[OK] Dismissed '{skill}' — it will no longer appear in reports.")


@learn.command("list")
@click.option("--state", default=None,
              type=click.Choice(["learning", "certified", "not_interested"]),
              help="Filter by state")
def learn_list(state):
    """List skills you're tracking."""
    from auto_applier.analysis.learning_goals import list_goals
    goals = list_goals(state=state)
    if not goals:
        click.echo("No skills tracked yet.")
        click.echo(
            "TIP: Add one with `python -m auto_applier --cli learn add <skill>`"
        )
        return

    click.echo("")
    buckets: dict[str, list[str]] = {}
    for skill, st in goals:
        buckets.setdefault(st, []).append(skill)

    # Ordered display
    for st in ("learning", "certified", "not_interested"):
        skills = buckets.get(st, [])
        if not skills:
            continue
        click.echo(f"{st.upper()}:")
        for s in skills:
            click.echo(f"  - {s}")
        click.echo("")


@learn.command("remove")
@click.argument("skill")
def learn_remove(skill):
    """Stop tracking a skill entirely (forget its state)."""
    from auto_applier.analysis.learning_goals import remove
    ok = remove(skill)
    if ok:
        click.echo(f"[OK] Removed '{skill}' from learning goals.")
    else:
        click.echo(f"'{skill}' was not tracked.")


@cli.command("auto-ghost")
@click.option("--days", default=30, type=int,
              help="Days without response to mark as ghosted (default 30)")
def auto_ghost(days):
    """Auto-mark old pending applications as ghosted.

    Walks through all your applications with outcome=pending and
    status=applied. If the applied date is older than --days days,
    marks the outcome as ghosted.
    """
    from auto_applier.analysis.outcome import auto_mark_ghosted
    count = auto_mark_ghosted(days=days)
    if count == 0:
        click.echo(
            f"No applications needed ghosting — all pending applications "
            f"are newer than {days} days."
        )
    else:
        click.echo(
            f"[OK] Marked {count} old application{'s' if count != 1 else ''} as ghosted."
        )


@cli.command()
@click.option("--by-resume", is_flag=True,
              help="Group gaps by the resume that was used")
@click.option("--by-title", is_flag=True,
              help="Group gaps by job title archetype")
@click.option("--limit", default=30, type=int,
              help="Max skills to show (default 30)")
def gaps(by_resume, by_title, limit):
    """Show skills missing from your resume across job applications.

    Default view: flat ranked list of skills most frequently asked for
    but not on your resume.

    --by-resume: breaks down gaps per resume label, so you can see
    which of your resumes is missing what.

    --by-title: groups by archetype (analyst, engineer, scientist, etc.)
    so you can see which skills belong to which career track.

    You can combine --by-resume --by-title to get the full breakdown.
    """
    from auto_applier.analysis.gap_tracker import gaps_with_context
    from auto_applier.analysis.learning_goals import skills_by_state
    from collections import Counter, defaultdict

    contexts = gaps_with_context()

    if not contexts:
        click.echo(
            "No skill gaps recorded yet.\n"
            "Run some applications first with: "
            "python -m auto_applier --cli run --dry-run"
        )
        return

    # Respect learning goals: hide "not_interested" skills entirely,
    # tag "learning" and "certified" with labels so the user knows.
    goal_states = skills_by_state()
    dismissed = goal_states["not_interested"]
    learning_set = goal_states["learning"]
    certified_set = goal_states["certified"]

    def _annotate(skill: str) -> str:
        if skill in learning_set:
            return f"{skill}  [learning]"
        if skill in certified_set:
            return f"{skill}  [certified]"
        return skill

    # Filter out dismissed skills
    contexts = [c for c in contexts
                if c.gap.field_label.lower().strip() not in dismissed]

    if not contexts:
        click.echo(
            "All tracked gaps have been dismissed.\n"
            "Run more applications to discover new skills."
        )
        return

    # Auto-detect single-resume vs multi-resume. If only one resume
    # is in use, the --by-resume grouping is just noise.
    resume_labels = {c.gap.resume_label for c in contexts if c.gap.resume_label}
    single_resume = len(resume_labels) <= 1

    # FLAT view (default)
    if not by_resume and not by_title:
        counter: Counter = Counter()
        for c in contexts:
            key = c.gap.field_label.lower().strip()
            counter[key] += 1

        total = len(contexts)
        unique = len(counter)
        click.echo(
            f"\nSkills you're missing across {total} application"
            f"{'s' if total != 1 else ''} "
            f"({unique} unique skill{'s' if unique != 1 else ''}):\n"
        )
        for skill, count in counter.most_common(limit):
            pct = count / total * 100 if total else 0
            click.echo(f"  {count:3d}  ({pct:4.1f}%)  {_annotate(skill)}")

        if len(counter) > limit:
            click.echo(f"\n... and {len(counter) - limit} more.")

        click.echo(
            "\nTIP: Run `python -m auto_applier --cli trends` to see "
            "which skills to prioritize learning."
        )
        return

    # GROUPED view — by resume and/or by title
    # Decide grouping keys
    def _group_key(ctx):
        parts = []
        if by_resume and not single_resume:
            parts.append(("resume", ctx.gap.resume_label or "(unknown)"))
        if by_title:
            parts.append(("title", ctx.archetype))
        return tuple(parts) if parts else (("all", "all"),)

    buckets: dict[tuple, Counter] = defaultdict(Counter)
    for c in contexts:
        buckets[_group_key(c)][c.gap.field_label.lower().strip()] += 1

    # Print each bucket
    click.echo("")
    for group, counts in sorted(buckets.items(), key=lambda x: -sum(x[1].values())):
        header_parts = []
        for kind, val in group:
            if kind == "resume":
                header_parts.append(f"Resume: {val}")
            elif kind == "title":
                header_parts.append(f"Title archetype: {val}")
        header = "  |  ".join(header_parts) if header_parts else "All"
        click.echo(f"=== {header} ({sum(counts.values())} gaps) ===")
        for skill, count in counts.most_common(limit):
            click.echo(f"  {count:3d}  {skill}")
        click.echo("")

    if single_resume and by_resume:
        click.echo(
            "(You only have one resume loaded, so --by-resume grouping "
            "was skipped.)"
        )


@cli.command()
@click.option("--limit", default=10, type=int,
              help="Max skills to show per section (default 10)")
def trends(limit):
    """Show which skills to prioritize learning.

    Groups your recurring gaps into:
    - Universal skills: show up across MULTIPLE career tracks. These
      are foundational and unlock jobs in more than one direction.
    - Track-specific skills: only appear in one archetype.

    Universal skills are usually the highest-leverage things to learn
    first. If you had to pick one thing, start with #1 here.
    """
    from auto_applier.analysis.gap_tracker import gaps_with_context
    from auto_applier.analysis.learning_goals import skills_by_state
    from collections import Counter, defaultdict

    contexts = gaps_with_context()

    if not contexts:
        click.echo(
            "No skill gaps recorded yet. Apply to some jobs first."
        )
        return

    # Filter dismissed / already-certified skills so trends only shows
    # things the user hasn't dealt with yet.
    goal_states = skills_by_state()
    excluded = goal_states["not_interested"] | goal_states["certified"]
    learning_set = goal_states["learning"]

    # Build: skill -> set of archetypes it appeared in, and total count
    skill_archetypes: dict[str, set[str]] = defaultdict(set)
    skill_counts: Counter = Counter()
    for c in contexts:
        key = c.gap.field_label.lower().strip()
        if key in excluded:
            continue
        skill_archetypes[key].add(c.archetype)
        skill_counts[key] += 1

    # Universal = missing in >= 2 different archetypes AND >= 2 apps
    universal: list[tuple[str, int, set[str]]] = []
    track_specific: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for skill, count in skill_counts.most_common():
        archetypes = skill_archetypes[skill]
        # Don't count 'other' archetype as a real track
        real_archetypes = archetypes - {"other"}
        if len(real_archetypes) >= 2 and count >= 2:
            universal.append((skill, count, real_archetypes))
        elif len(real_archetypes) == 1:
            (track,) = real_archetypes
            track_specific[track].append((skill, count))

    # Present
    total_apps = len(contexts)
    click.echo(
        f"\nBased on {total_apps} application{'s' if total_apps != 1 else ''}, "
        "here's what to prioritize learning:\n"
    )

    def _annotate(skill: str) -> str:
        if skill in learning_set:
            return f"{skill} [learning]"
        return skill

    if universal:
        click.echo("UNIVERSAL skills (highest priority - open more doors):")
        for skill, count, archetypes in universal[:limit]:
            archs = ", ".join(sorted(archetypes))
            click.echo(f"  {count:3d}  {_annotate(skill):36s}  (across: {archs})")
        click.echo("")

    if track_specific:
        click.echo("TRACK-SPECIFIC skills (useful only for one career path):")
        for track in sorted(track_specific.keys()):
            skills = track_specific[track]
            if not skills:
                continue
            click.echo(f"\n  {track.upper()} track:")
            for skill, count in skills[:limit]:
                click.echo(f"    {count:3d}  {_annotate(skill)}")
        click.echo("")

    if not universal and not track_specific:
        click.echo(
            "Not enough data yet — apply to more jobs across different "
            "role types to see trends."
        )
        return

    click.echo(
        "TIP: Mark a skill you're learning:\n"
        "     python -m auto_applier --cli learn add <skill>\n"
        "  Or dismiss one you don't care about:\n"
        "     python -m auto_applier --cli learn dismiss <skill>"
    )


@cli.command()
def doctor():
    """Run preflight checks — verify everything is ready to run."""
    from auto_applier import doctor as doctor_module
    import sys as _sys
    _sys.exit(doctor_module.run())


@cli.command()
def pauses():
    """List active platform cooldowns (set after CAPTCHA / login failure)."""
    from auto_applier.orchestrator import platform_pauses

    active = platform_pauses.list_active()
    if not active:
        click.echo("No platforms are currently paused.")
        return
    click.echo(f"Active platform cooldowns ({len(active)}):")
    for rec in active:
        remaining = platform_pauses.format_remaining(rec)
        click.echo(
            f"  {rec.platform:14s}  {remaining:>8s} remaining  "
            f"— {rec.reason[:80]}"
        )
    click.echo(
        "\nClear with: --cli unpause <platform>  (or 'all' to clear every pause)"
    )


@cli.command()
@click.argument("platform")
def unpause(platform: str):
    """Manually clear a platform cooldown. Pass 'all' to clear every pause."""
    from auto_applier.orchestrator import platform_pauses

    if platform.lower() == "all":
        count = platform_pauses.unpause_all()
        click.echo(f"Cleared {count} pause(s).")
        return
    if platform_pauses.unpause(platform):
        click.echo(f"Unpaused {platform}.")
    else:
        click.echo(f"No active pause for '{platform}'.")


@cli.command()
@click.argument("job_id")
def show(job_id: str):
    """Show everything known about a single job (jobs, apps, gaps, followups)."""
    from auto_applier.analysis.observability import get_job_detail

    detail = get_job_detail(job_id)
    if not detail:
        click.echo(f"No job with id '{job_id}'.")
        return

    click.echo(f"\n=== Job {job_id} ===")
    for j in detail["jobs"]:
        click.echo(f"\n[Job row]")
        for k, v in j.items():
            click.echo(f"  {k:16s} {v}")

    if detail["applications"]:
        click.echo("\n[Applications]")
        for a in detail["applications"]:
            click.echo(
                f"  {a.get('applied_at', '?')}  "
                f"{a.get('status', '?'):8s}  "
                f"{a.get('source', '?'):12s}  "
                f"score={a.get('score', 0)}  "
                f"resume={a.get('resume_used', '')}"
            )
            if a.get("failure_reason"):
                click.echo(f"      failure: {a['failure_reason']}")

    if detail["skill_gaps"]:
        click.echo(f"\n[Skill gaps: {len(detail['skill_gaps'])}]")
        for g in detail["skill_gaps"][:15]:
            click.echo(f"  [{g.get('category', '?')}] {g.get('field_label', '?')}")

    if detail["followups"]:
        click.echo("\n[Follow-ups]")
        for f in detail["followups"]:
            click.echo(
                f"  {f.get('due_date', '?')}  "
                f"[{f.get('status', '?'):9s}]  "
                f"{f.get('channel', '?')}"
            )


@cli.command()
@click.option(
    "--output", "-o", default="auto_applier_export.json",
    help="Output file path (default: auto_applier_export.json)",
)
def export(output: str):
    """Export all CSV data plus schema hints to a single JSON file."""
    from pathlib import Path as _Path
    from auto_applier.analysis.observability import write_export

    path = write_export(_Path(output))
    size_kb = path.stat().st_size / 1024
    click.echo(f"Exported to {path} ({size_kb:.1f} KB)")


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


@cli.command("reset-history")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def reset_history(yes: bool):
    """Wipe run history so dry runs have a clean slate.

    Clears jobs.csv, applications.csv, skill_gaps.csv, and
    followups.csv while preserving resume profiles, user_config,
    archetypes, story bank, and research briefings. Useful when
    iterating on the pipeline — every prior run's dedup state
    goes away and subsequent runs see every job as fresh.

    Backups go to data/.backups/ before anything is deleted.
    """
    from shutil import copy2
    from auto_applier.config import (
        APPLICATIONS_CSV, BACKUP_DIR, FOLLOWUPS_CSV, JOBS_CSV, SKILL_GAPS_CSV,
    )
    from datetime import datetime, timezone

    targets = [
        ("jobs.csv", JOBS_CSV),
        ("applications.csv", APPLICATIONS_CSV),
        ("skill_gaps.csv", SKILL_GAPS_CSV),
        ("followups.csv", FOLLOWUPS_CSV),
    ]
    existing = [(n, p) for n, p in targets if p.exists()]
    if not existing:
        click.echo("Nothing to reset — no history CSVs exist.")
        return

    click.echo("The following CSV files will be cleared:")
    for name, path in existing:
        click.echo(f"  {name}")
    click.echo(
        "\nResume profiles, user_config.json, archetypes, and the story "
        "bank are NOT touched."
    )
    if not yes:
        click.confirm("Proceed?", abort=True)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for name, path in existing:
        backup_path = BACKUP_DIR / f"{path.stem}.{timestamp}.reset{path.suffix}"
        try:
            copy2(path, backup_path)
        except Exception as e:
            click.echo(f"  warn: could not back up {name}: {e}")
        try:
            path.unlink()
        except Exception as e:
            click.echo(f"  error: could not delete {name}: {e}")
            continue
        click.echo(f"  cleared {name} (backup: {backup_path.name})")
    click.echo("\nDone. Dry runs will now see every job as fresh.")


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


@cli.command()
@click.argument("company")
@click.option(
    "--from-file", "-f", type=click.Path(exists=True),
    help="Path to a text file containing source material (career page, news, notes)",
)
@click.option(
    "--show/--no-show", default=True, help="Print the briefing to stdout",
)
def research(company: str, from_file: str, show: bool):
    """Generate a company research briefing from pasted or file-provided source material.

    Paste the career page / article / notes into a text file and pass
    --from-file, or pipe stdin when --from-file is omitted.
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path
    from auto_applier.analysis.research import (
        CompanyResearcher, save_briefing,
    )
    from auto_applier.llm.router import LLMRouter

    if from_file:
        source_material = _Path(from_file).read_text(encoding="utf-8")
    else:
        click.echo(
            "Paste source material, then Ctrl-Z + Enter (Windows) or "
            "Ctrl-D (Unix) when done:"
        )
        import sys as _sys
        source_material = _sys.stdin.read()

    if not source_material.strip():
        click.echo("No source material provided — aborting.")
        return

    async def _run():
        router = LLMRouter()
        await router.initialize()
        researcher = CompanyResearcher(router)
        return await researcher.research(company, source_material)

    briefing = _asyncio.run(_run())
    if briefing is None:
        click.echo(
            "Research failed — LLM produced no usable summary. "
            "Try providing more source material."
        )
        return

    path = save_briefing(briefing)
    click.echo(f"Saved briefing to {path}")
    if show:
        click.echo("")
        click.echo(briefing.to_markdown())


@cli.command()
@click.argument("job_id")
@click.option("--resume", default=None, help="Resume label to use (defaults to best match)")
def tailor(job_id: str, resume: str):
    """Generate a tailored PDF resume for a specific stored job."""
    import asyncio as _asyncio
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.tailor import (
        ResumeTailor, render_html, render_pdf,
        save_tailored_json, tailored_pdf_path,
    )
    from auto_applier.storage.models import Job
    from auto_applier.storage.repository import load_all

    jobs = [j for j in load_all(Job) if j.job_id == job_id]
    if not jobs:
        click.echo(f"No job found with id '{job_id}'.")
        return
    job = jobs[0]

    async def _run():
        router = LLMRouter()
        # initialize() probes each backend and populates the
        # availability dict — without this, complete_json/complete
        # see every backend as unavailable and immediately fall
        # through to "All LLM backends failed for JSON prompt".
        await router.initialize()
        mgr = ResumeManager(router)
        label = resume
        if not label:
            resumes = mgr.list_resumes()
            if not resumes:
                return "No resumes loaded."
            label = resumes[0].label
        resume_text = mgr.get_resume_text(label)
        if not resume_text:
            return f"Resume '{label}' has no parsed text."

        click.echo(f"Tailoring resume '{label}' for {job.title} @ {job.company}...")
        tailor_obj = ResumeTailor(router)
        tailored = await tailor_obj.tailor(
            resume_text=resume_text,
            job_description=job.description,
            company_name=job.company,
            job_title=job.title,
            job_id=job.job_id,
            resume_label=label,
        )
        if tailored is None:
            return "LLM failed to produce a tailored resume. See logs."

        # Pull name/contact from user_config.json if available
        cfg = load_user_config()
        personal = cfg.get("personal_info", cfg)
        name = personal.get("name", "")
        contact_bits = []
        for k in ("email", "phone", "city"):
            v = personal.get(k, "")
            if v:
                contact_bits.append(str(v))
        contact = " | ".join(contact_bits)

        html_content = render_html(tailored, name=name, contact=contact)
        json_path = save_tailored_json(tailored)
        pdf_path = tailored_pdf_path(job.job_id)

        ok = await render_pdf(html_content, pdf_path)
        if not ok:
            return f"HTML written to {json_path.with_suffix('.html')}, but PDF render failed."
        # Keep a copy of the HTML alongside the PDF for debugging
        html_path = pdf_path.with_suffix(".html")
        html_path.write_text(html_content, encoding="utf-8")
        return f"Wrote {pdf_path}"

    result = _asyncio.run(_run())
    click.echo(result)


@cli.command()
@click.argument("job_id")
@click.option("--resume", default=None, help="Resume label to use (defaults to first loaded)")
def outreach(job_id: str, resume: str):
    """Generate a LinkedIn connection-request message for a specific job."""
    import asyncio as _asyncio
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.resume.outreach import OutreachWriter
    from auto_applier.storage.models import Job
    from auto_applier.storage.repository import load_all

    jobs = [j for j in load_all(Job) if j.job_id == job_id]
    if not jobs:
        click.echo(f"No job found with id '{job_id}'.")
        return
    job = jobs[0]

    async def _gen():
        router = LLMRouter()
        await router.initialize()
        mgr = ResumeManager(router)
        label = resume
        if not label:
            resumes = mgr.list_resumes()
            if not resumes:
                return None, "No resumes loaded — add one via the GUI wizard first."
            label = resumes[0].label
        resume_text = mgr.get_resume_text(label)
        if not resume_text:
            return None, f"Resume '{label}' has no parsed text."
        writer = OutreachWriter(router)
        msg = await writer.generate(
            resume_text=resume_text,
            job_description=job.description,
            company_name=job.company,
            job_title=job.title,
        )
        return msg, None

    message, err = _asyncio.run(_gen())
    if err:
        click.echo(err)
        return
    if not message:
        click.echo("LLM could not produce a message. Check `cli doctor`.")
        return
    click.echo(f"\n--- {len(message)} chars ---")
    click.echo(message)
    click.echo("-------------------")


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


@followup.command("draft")
@click.argument("job_id")
@click.option(
    "--attempt", "-a", default=1, type=int,
    help="1=warm check-in, 2=direct with new signal, 3=closing the loop",
)
@click.option(
    "--resume", default=None,
    help="Resume label to use (defaults to best match for the job)",
)
def followup_draft(job_id: str, attempt: int, resume: str):
    """Generate a tailored follow-up email body for a stored job."""
    import asyncio as _asyncio
    from datetime import datetime, timezone
    from auto_applier.llm.router import LLMRouter
    from auto_applier.resume.followup_writer import FollowupEmailWriter
    from auto_applier.resume.manager import ResumeManager
    from auto_applier.storage.models import Application, Job
    from auto_applier.storage.repository import load_all

    jobs = [j for j in load_all(Job) if j.job_id == job_id]
    if not jobs:
        click.echo(f"No job found with id '{job_id}'.")
        return
    job = jobs[0]

    # Figure out days since the application was sent
    apps = [
        a for a in load_all(Application)
        if a.job_id == job_id and a.status in ("applied", "dry_run")
    ]
    if apps:
        try:
            applied_at = datetime.fromisoformat(apps[0].applied_at)
            days_since = max(0, (datetime.now(timezone.utc) - applied_at).days)
        except ValueError:
            days_since = 7
    else:
        days_since = 7

    async def _gen():
        router = LLMRouter()
        await router.initialize()
        mgr = ResumeManager(router)
        label = resume or (apps[0].resume_used if apps else "")
        if not label:
            resumes = mgr.list_resumes()
            if not resumes:
                return None, "No resumes loaded."
            label = resumes[0].label
        resume_text = mgr.get_resume_text(label)
        if not resume_text:
            return None, f"Resume '{label}' has no parsed text."
        writer = FollowupEmailWriter(router)
        body = await writer.generate(
            resume_text=resume_text,
            job_description=job.description,
            company_name=job.company,
            job_title=job.title,
            attempt=attempt,
            days_since=days_since,
        )
        return body, None

    body, err = _asyncio.run(_gen())
    if err:
        click.echo(err)
        return
    if not body:
        click.echo("LLM failed to produce a follow-up. Check `cli doctor`.")
        return
    click.echo(
        f"\n--- attempt {attempt}, {days_since} days since application ---"
    )
    click.echo(body)
    click.echo("-------------------")


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
