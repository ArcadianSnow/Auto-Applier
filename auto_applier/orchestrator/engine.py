"""Application engine -- orchestrates the full discover -> score -> apply pipeline."""
import asyncio
import json
import os
from pathlib import Path

from auto_applier.browser.session import BrowserSession
from auto_applier.browser.platforms import PLATFORM_REGISTRY
from auto_applier.config import (
    MAX_APPLICATIONS_PER_DAY,
    USER_CONFIG_FILE,
    PROJECT_ROOT,
    DEFAULT_AUTO_APPLY_MIN,
    DEFAULT_CLI_AUTO_APPLY_MIN,
    DEFAULT_REVIEW_MIN,
)
from auto_applier.llm.router import LLMRouter
from auto_applier.resume.manager import ResumeManager
from auto_applier.resume.evolution import EvolutionEngine
from auto_applier.scoring.scorer import JobScorer
from auto_applier.scoring.models import ScoreDecision
from auto_applier.storage.models import Application
from auto_applier.storage.repository import get_todays_application_count, save
from auto_applier.orchestrator.events import (
    EventEmitter,
    RUN_STARTED,
    RESUME_PARSED,
    PLATFORM_STARTED,
    PLATFORM_LOGIN_NEEDED,
    PLATFORM_LOGIN_FAILED,
    SEARCH_STARTED,
    JOBS_FOUND,
    JOB_SCORED,
    USER_REVIEW_NEEDED,
    APPLICATION_STARTED,
    APPLICATION_COMPLETE,
    PLATFORM_ERROR,
    PLATFORM_FINISHED,
    EVOLUTION_TRIGGERS,
    RUN_FINISHED,
    CAPTCHA_DETECTED,
)
from auto_applier.orchestrator.pipeline import discover_jobs, fetch_description, apply_to_job


class ApplicationEngine:
    """Orchestrates job discovery, scoring, and application across platforms."""

    def __init__(
        self,
        config: dict,
        events: EventEmitter | None = None,
        cli_mode: bool = False,
    ):
        self.config = config
        self.events = events or EventEmitter()
        self.cli_mode = cli_mode
        self.dry_run = config.get("dry_run", False)

        # Components initialized in start()
        self.router: LLMRouter | None = None
        self.resume_manager: ResumeManager | None = None
        self.scorer: JobScorer | None = None
        self.evolution: EvolutionEngine | None = None
        self.browser: BrowserSession | None = None

        # Stats
        self.applied_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.reviewed_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Initialize all components."""
        # LLM router
        self.router = LLMRouter()
        await self.router.initialize()

        # Resume manager
        self.resume_manager = ResumeManager(self.router)

        # Scorer with config thresholds
        scoring_config = self.config.get("scoring", {})
        self.scorer = JobScorer(
            resume_manager=self.resume_manager,
            auto_apply_min=scoring_config.get("auto_apply_min", DEFAULT_AUTO_APPLY_MIN),
            review_min=scoring_config.get("review_min", DEFAULT_REVIEW_MIN),
            cli_auto_apply_min=scoring_config.get(
                "cli_auto_apply_min", DEFAULT_CLI_AUTO_APPLY_MIN
            ),
        )

        # Evolution engine
        self.evolution = EvolutionEngine()

        # Browser
        self.browser = BrowserSession()
        await self.browser.start()

    async def stop(self):
        """Clean up resources."""
        if self.browser:
            await self.browser.stop()

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self):
        """Execute the full application pipeline."""
        self.events.emit(RUN_STARTED, dry_run=self.dry_run)

        try:
            await self.start()

            # Check daily limit
            todays_count = get_todays_application_count()
            max_daily = self.config.get(
                "max_applications_per_day", MAX_APPLICATIONS_PER_DAY
            )
            remaining = max_daily - todays_count
            if remaining <= 0:
                self.events.emit(RUN_FINISHED, reason="Daily limit reached")
                return

            # Gather run parameters
            enabled = self.config.get("enabled_platforms", ["linkedin"])
            keywords = self.config.get("search_keywords", [])
            location = self.config.get("location", "")
            personal_info = self.config.get("personal_info", {})

            # Merge .env credentials into config
            self._load_credentials()

            # Run each platform with error isolation
            for platform_key in enabled:
                if self.applied_count >= remaining:
                    break

                platform_cls = PLATFORM_REGISTRY.get(platform_key)
                if not platform_cls:
                    continue

                self.events.emit(PLATFORM_STARTED, platform=platform_key)

                try:
                    await self._run_platform(
                        platform_cls,
                        platform_key,
                        keywords,
                        location,
                        personal_info,
                        remaining - self.applied_count,
                    )
                except Exception as e:
                    self.events.emit(
                        PLATFORM_ERROR, platform=platform_key, error=str(e)
                    )
                finally:
                    self.events.emit(PLATFORM_FINISHED, platform=platform_key)

            # Check for evolution triggers after the run
            triggers = self.evolution.check_triggers()
            if triggers:
                self.events.emit(EVOLUTION_TRIGGERS, triggers=triggers)

        except Exception as e:
            self.events.emit(RUN_FINISHED, reason=f"Error: {e}")
            raise
        finally:
            await self.stop()
            self.events.emit(
                RUN_FINISHED,
                reason="Complete",
                applied=self.applied_count,
                skipped=self.skipped_count,
                failed=self.failed_count,
            )

    # ------------------------------------------------------------------
    # Per-platform pipeline
    # ------------------------------------------------------------------

    async def _run_platform(
        self, platform_cls, platform_key, keywords, location, personal_info, budget
    ):
        """Run the pipeline for a single platform."""
        platform = platform_cls(
            context=self.browser.context,
            config=self.config,
        )

        # Ensure logged in
        self.events.emit(PLATFORM_LOGIN_NEEDED, platform=platform_key)
        logged_in = await platform.ensure_logged_in()
        if not logged_in:
            self.events.emit(PLATFORM_LOGIN_FAILED, platform=platform_key)
            return

        applied_this_platform = 0

        for keyword in keywords:
            if applied_this_platform >= budget:
                break

            self.events.emit(
                SEARCH_STARTED, platform=platform_key, keyword=keyword
            )

            # Discover jobs
            jobs = await discover_jobs(platform, keyword, location)
            self.events.emit(
                JOBS_FOUND, platform=platform_key, keyword=keyword, count=len(jobs)
            )

            for job in jobs:
                if applied_this_platform >= budget:
                    break

                # Fetch description (also checks liveness on the same page)
                job = await fetch_description(platform, job)

                # Check for CAPTCHA before proceeding
                page = await platform.get_page()
                if await platform.detect_captcha(page):
                    self.events.emit(CAPTCHA_DETECTED, platform=platform_key)
                    return  # Hard stop

                # Skip dead listings — don't waste LLM calls or apply quota
                if job.liveness == "dead":
                    self.skipped_count += 1
                    save(
                        Application(
                            job_id=job.job_id,
                            status="skipped",
                            source=platform_key,
                            resume_used="",
                            score=0,
                            failure_reason="dead listing",
                        )
                    )
                    continue

                # Score the job against all resumes
                job_score = await self.scorer.score(
                    job.description, cli_mode=self.cli_mode
                )
                self.events.emit(JOB_SCORED, job=job, score=job_score)

                # Decision gate
                if job_score.decision == ScoreDecision.SKIP:
                    self.skipped_count += 1
                    save(
                        Application(
                            job_id=job.job_id,
                            status="skipped",
                            source=platform_key,
                            resume_used=job_score.resume_label,
                            score=job_score.score,
                        )
                    )
                    continue

                if job_score.decision == ScoreDecision.USER_REVIEW:
                    if self.cli_mode:
                        # In CLI mode, skip USER_REVIEW jobs
                        self.skipped_count += 1
                        save(
                            Application(
                                job_id=job.job_id,
                                status="skipped",
                                source=platform_key,
                                resume_used=job_score.resume_label,
                                score=job_score.score,
                            )
                        )
                        continue
                    else:
                        # In GUI mode, wait for user decision
                        decision = await self.events.emit_and_wait(
                            USER_REVIEW_NEEDED,
                            job=job,
                            score=job_score,
                            timeout=300.0,
                        )
                        if decision != "apply":
                            self.skipped_count += 1
                            continue

                # Get the best resume info
                resume_info = self.resume_manager.get_resume(job_score.resume_label)
                if not resume_info:
                    self.failed_count += 1
                    continue

                resume_text = self.resume_manager.get_resume_text(
                    job_score.resume_label
                )

                self.events.emit(
                    APPLICATION_STARTED, job=job, resume=job_score.resume_label
                )

                # Apply
                app = await apply_to_job(
                    platform=platform,
                    job=job,
                    resume_path=str(resume_info.file_path),
                    resume_text=resume_text,
                    resume_label=job_score.resume_label,
                    personal_info=personal_info,
                    router=self.router,
                    dry_run=self.dry_run,
                )
                app.score = job_score.score
                if job_score.dimensions:
                    import json as _json
                    app.dimensions_json = _json.dumps([
                        {
                            "name": d.name,
                            "score": d.score,
                            "weight": d.weight,
                            "explanation": d.explanation,
                        }
                        for d in job_score.dimensions
                    ])

                if app.status in ("applied", "dry_run"):
                    self.applied_count += 1
                    applied_this_platform += 1
                else:
                    self.failed_count += 1

                self.events.emit(APPLICATION_COMPLETE, job=job, application=app)

                # Generate STAR+R interview stories for real submissions.
                # Non-blocking: failure here must never affect the run.
                if app.status == "applied":
                    try:
                        from auto_applier.resume.story_bank import (
                            StoryGenerator, append_stories,
                        )
                        stories = await StoryGenerator(self.router).generate(
                            resume_text=resume_text,
                            job_description=job.description,
                            company_name=job.company,
                            job_title=job.title,
                            job_id=job.job_id,
                            resume_label=job_score.resume_label,
                        )
                        append_stories(stories)
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_credentials(self):
        """Load platform credentials from .env into config."""
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=True)

        platforms = self.config.setdefault("platforms", {})
        for key in self.config.get("enabled_platforms", []):
            prefix = key.upper()
            entry = platforms.setdefault(key, {})
            entry["email"] = os.getenv(f"{prefix}_EMAIL", entry.get("email", ""))
            entry["password"] = os.getenv(f"{prefix}_PASSWORD", "")
