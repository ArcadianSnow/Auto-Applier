"""Application engine -- orchestrates the full discover -> score -> apply pipeline."""
import logging
import os

logger = logging.getLogger(__name__)

from auto_applier.browser.base_platform import CaptchaDetectedError
from auto_applier.browser.session import BrowserSession
from auto_applier.browser.platforms import PLATFORM_REGISTRY
from auto_applier.config import (
    MAX_APPLICATIONS_PER_DAY,
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
    PLATFORM_STARTED,
    PLATFORM_LOGIN_NEEDED,
    PLATFORM_LOGIN_FAILED,
    SEARCH_STARTED,
    JOBS_FOUND,
    JOB_SCORED,
    USER_REVIEW_NEEDED,
    REVIEW_QUEUE_READY,
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

        # Stats — must ALL be initialized here, not in request_stop().
        # The finally block in run() reads skipped_count / failed_count
        # to emit RUN_FINISHED, so any code path that reaches the
        # finally without having set these would AttributeError. The
        # old code assigned them inside request_stop(), which meant a
        # run that completed normally (no Stop click) crashed on the
        # final event emit.
        self.applied_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        # USER_REVIEW jobs collected during the run for batch review
        # after all platforms finish. Each entry is a dict of
        # {job, score, platform_key, resume_label}.
        self.pending_review: list[dict] = []
        # Cooperative stop flag. Set by request_stop() from the GUI's
        # Stop button. Checked at every natural stopping point:
        # between platforms, between keywords, between jobs. The
        # CURRENT application finishes cleanly but nothing new
        # starts after the flag is set.
        self._stop_requested: bool = False

    def request_stop(self) -> None:
        """Signal the engine to stop after the current job finishes."""
        self._stop_requested = True
        logger.info("Stop requested — will finish current job and exit.")

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

    def _apply_fast_mode(self) -> None:
        """Toggle anti-detect fast mode to match the run type.

        Real submissions need the full stealth delays so the browser
        fingerprint looks human. Dry runs don't submit anything, so
        the delays just cost the user wall-clock time for no benefit.
        """
        from auto_applier.browser.anti_detect import set_fast_mode
        set_fast_mode(self.dry_run)

    async def run(self):
        """Execute the full application pipeline."""
        self.events.emit(RUN_STARTED, dry_run=self.dry_run)

        try:
            await self.start()
            self._apply_fast_mode()

            # Per-platform daily budget. max_applications_per_day is
            # interpreted as the cap PER PLATFORM — with a limit of 3
            # and three platforms enabled, the run can apply to up to
            # 9 jobs total (3 on Indeed, 3 on Dice, 3 on ZipRecruiter).
            # This matches user intuition much better than a single
            # global budget shared across platforms.
            #
            # Dry runs get a fresh per-platform budget every invocation
            # so dry-running in the morning doesn't consume your real
            # quota for the afternoon.
            per_platform_max = self.config.get(
                "max_applications_per_day", MAX_APPLICATIONS_PER_DAY
            )

            # Gather run parameters
            enabled = self.config.get("enabled_platforms", ["linkedin"])
            keywords = self.config.get("search_keywords", [])
            location = self.config.get("location", "")
            personal_info = self.config.get("personal_info", {}) or {}

            # Merge personal_info from user_config.json directly so
            # any fields the wizard UI doesn't have widgets for
            # (zip_code, state, street_address, etc. from the fixture
            # generator) reach the form filler. user_config.json is
            # the canonical source of truth; the config dict passed
            # in here is just whatever the wizard UI captured.
            try:
                from auto_applier.config import USER_CONFIG_FILE
                import json as _json
                if USER_CONFIG_FILE.exists():
                    with open(USER_CONFIG_FILE, "r", encoding="utf-8") as _f:
                        saved = _json.load(_f)
                    saved_personal = saved.get("personal_info", {}) or {}
                    if isinstance(saved_personal, dict):
                        # saved fields go UNDER what the wizard explicitly
                        # set this session — UI overrides file.
                        merged = dict(saved_personal)
                        merged.update(personal_info)
                        personal_info = merged
            except Exception:
                pass

            # Merge .env credentials into config
            self._load_credentials()

            # Run each platform with error isolation
            for platform_key in enabled:
                # Honor the stop flag — if the user clicked Stop,
                # don't start a new platform.
                if self._stop_requested:
                    logger.info(
                        "Stop requested, skipping remaining platforms",
                    )
                    break
                # Per-platform budget check. Dry runs always get the
                # full cap; real runs subtract today's real applies
                # for this source from the cap.
                if self.dry_run:
                    platform_budget = per_platform_max
                else:
                    todays_on_platform = get_todays_application_count(
                        source=platform_key,
                    )
                    platform_budget = per_platform_max - todays_on_platform
                    if platform_budget <= 0:
                        self.events.emit(
                            PLATFORM_STARTED, platform=platform_key,
                        )
                        self.events.emit(
                            PLATFORM_FINISHED, platform=platform_key,
                        )
                        continue

                platform_cls = PLATFORM_REGISTRY.get(platform_key)
                if not platform_cls:
                    continue

                self.events.emit(PLATFORM_STARTED, platform=platform_key)

                captcha_hard_stop = False
                try:
                    await self._run_platform(
                        platform_cls,
                        platform_key,
                        keywords,
                        location,
                        personal_info,
                        platform_budget,
                    )
                except CaptchaDetectedError as e:
                    # CAPTCHA is a hard stop — continuing on another
                    # platform in the same browser session just means
                    # we'll trip the SAME detection again and dig the
                    # fingerprint hole deeper. Flag the run to stop
                    # after the finally block fires PLATFORM_FINISHED.
                    self.events.emit(
                        CAPTCHA_DETECTED, platform=platform_key, error=str(e)
                    )
                    captcha_hard_stop = True
                except Exception as e:
                    self.events.emit(
                        PLATFORM_ERROR, platform=platform_key, error=str(e)
                    )
                finally:
                    self.events.emit(PLATFORM_FINISHED, platform=platform_key)
                if captcha_hard_stop:
                    self._stop_requested = True
                    break

            # Batch-review step: if any platforms produced jobs in
            # the USER_REVIEW score band, open them now as a single
            # queue that the user walks through at the end. This is
            # much better UX than blocking each time a borderline
            # score comes up mid-pipeline.
            if self.pending_review:
                await self._process_review_queue(personal_info=personal_info)

            # Check for evolution triggers after the run
            triggers = self.evolution.check_triggers()
            if triggers:
                self.events.emit(EVOLUTION_TRIGGERS, triggers=triggers)

        except Exception as e:
            run_reason = f"Error: {e}"
            raise
        else:
            run_reason = "Complete"
        finally:
            await self.stop()
            self.events.emit(
                RUN_FINISHED,
                reason=run_reason,
                applied=self.applied_count,
                skipped=self.skipped_count,
                failed=self.failed_count,
                dry_run=self.dry_run,
            )

    # ------------------------------------------------------------------
    # Batch review queue
    # ------------------------------------------------------------------

    async def _process_review_queue(self, personal_info: dict | None = None) -> None:
        """Let the user batch-review all USER_REVIEW jobs at once.

        Emits REVIEW_QUEUE_READY with the full queue so the
        dashboard can open a single batch-review panel. Then waits
        on an event carrying the user's decisions. The GUI is
        expected to resolve the event with a list of ``job_id``
        strings that the user approved for apply; everything else
        is treated as skipped.

        CLI mode skips the batch review entirely (the CLI engine
        already auto-skips USER_REVIEW jobs during the main loop).
        Dry runs still process the queue so users can see how the
        review UX works end-to-end.
        """
        self.events.emit(
            REVIEW_QUEUE_READY,
            queue=self.pending_review,
            count=len(self.pending_review),
        )

        try:
            decisions = await self.events.emit_and_wait(
                REVIEW_QUEUE_READY,
                queue=self.pending_review,
                timeout=600.0,  # 10 minutes to work through the queue
            )
        except Exception:
            decisions = None

        # The GUI returns a dict of {job_id: "apply" | "skip"}, or
        # None on timeout. Anything missing is treated as skip.
        if not isinstance(decisions, dict):
            decisions = {}

        # Use the MERGED personal_info passed from run() — it contains
        # the wizard UI fields stacked on top of user_config.json, so
        # address/zip/etc. are present. Reading self.config here would
        # lose the saved fields that the UI doesn't expose.
        if personal_info is None:
            personal_info = self.config.get("personal_info", {}) or {}

        for item in self.pending_review:
            if self._stop_requested:
                logger.info("Stop requested, aborting remaining reviews")
                break
            job = item["job"]
            job_score = item["score"]
            platform_key = item["platform_key"]

            decision = decisions.get(job.job_id, "skip")
            if decision != "apply":
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

            # User approved — run the apply step now. This
            # re-uses the platform instance if it's still alive,
            # otherwise creates a fresh one. The browser session
            # persists across platforms, so cookies still work.
            platform_cls = PLATFORM_REGISTRY.get(platform_key)
            if not platform_cls:
                self.failed_count += 1
                continue
            platform = platform_cls(
                context=self.browser.context,
                config=self.config,
            )
            resume_info = self.resume_manager.get_resume(job_score.resume_label)
            if not resume_info:
                self.failed_count += 1
                continue
            resume_text = self.resume_manager.get_resume_text(
                job_score.resume_label
            )

            self.events.emit(
                APPLICATION_STARTED, job=job, resume=job_score.resume_label,
            )
            try:
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
                if app.status in ("applied", "dry_run"):
                    self.applied_count += 1
                else:
                    self.failed_count += 1
                self.events.emit(
                    APPLICATION_COMPLETE, job=job, application=app,
                )
            except Exception as exc:
                self.failed_count += 1
                self.events.emit(
                    APPLICATION_COMPLETE,
                    job=job,
                    application=Application(
                        job_id=job.job_id,
                        status="failed",
                        source=platform_key,
                        resume_used=job_score.resume_label,
                        score=job_score.score,
                        failure_reason=str(exc),
                    ),
                )

        # Clear the queue so a second call is a no-op.
        self.pending_review.clear()

    # ------------------------------------------------------------------
    # Title expansion helper (used by _run_platform auto-broaden path)
    # ------------------------------------------------------------------

    async def _expand_keyword_for_search(self, keyword: str) -> list[str]:
        """Get adjacent titles for a keyword that yielded few results.

        Returns a list of title strings. Empty list means expansion
        failed or produced nothing useful (e.g. seed not in static dict
        AND LLM unavailable). Results are cached by seed for the
        lifetime of this engine instance so re-visiting the same
        keyword from a second platform doesn't re-invoke the LLM.
        """
        from auto_applier.analysis.title_expansion import expand_title

        cache = getattr(self, "_expansion_cache", None)
        if cache is None:
            cache = {}
            self._expansion_cache = cache

        key = keyword.lower().strip()
        if key in cache:
            return cache[key]

        # Pull resume context from the first loaded resume, if any.
        # Makes LLM suggestions resume-aware without requiring the
        # user to have already scored against this specific JD.
        resume_text = ""
        try:
            if self.resume_manager is not None:
                resumes = self.resume_manager.list_resumes()
                if resumes:
                    resume_text = self.resume_manager.get_resume_text(
                        resumes[0].label,
                    )
        except Exception:
            pass

        result = await expand_title(
            seed=keyword,
            router=self.router,
            resume_text=resume_text,
            prefer_llm=True,
        )
        adjacents = result.adjacents if result.has_suggestions else []
        cache[key] = adjacents
        return adjacents

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

        # Build effective keyword list. When auto_expand_titles is on
        # and a keyword yields few jobs, we'll broaden on the fly.
        effective_keywords = list(keywords)
        already_expanded: set[str] = set()
        auto_expand = self.config.get("auto_expand_titles", False)
        expand_threshold = int(
            self.config.get("title_expansion_threshold", 10)
        )

        kw_index = 0
        while kw_index < len(effective_keywords):
            keyword = effective_keywords[kw_index]
            kw_index += 1

            if applied_this_platform >= budget:
                break
            if self._stop_requested:
                logger.info(
                    "Stop requested, skipping remaining keywords on %s",
                    platform_key,
                )
                break

            self.events.emit(
                SEARCH_STARTED, platform=platform_key, keyword=keyword
            )

            # Discover jobs
            jobs = await discover_jobs(
                platform, keyword, location, dry_run=self.dry_run,
            )
            self.events.emit(
                JOBS_FOUND, platform=platform_key, keyword=keyword, count=len(jobs)
            )

            # Auto-broaden: if this keyword returned fewer jobs than
            # threshold AND the user opted in via config, queue up
            # expanded titles for search too. Each keyword only gets
            # expanded once per run to avoid runaway searches.
            if (
                auto_expand
                and len(jobs) < expand_threshold
                and keyword.lower() not in already_expanded
            ):
                already_expanded.add(keyword.lower())
                try:
                    expanded = await self._expand_keyword_for_search(keyword)
                except Exception as exc:
                    logger.debug(
                        "Title expansion failed for '%s': %s", keyword, exc,
                    )
                    expanded = []
                if expanded:
                    logger.info(
                        "Low results for '%s' (%d jobs) — auto-expanding "
                        "to %d related titles: %s",
                        keyword, len(jobs), len(expanded),
                        ", ".join(expanded),
                    )
                    # Append so we walk them later in this while loop
                    for exp_title in expanded:
                        if exp_title.lower() not in {
                            k.lower() for k in effective_keywords
                        }:
                            effective_keywords.append(exp_title)

            for job in jobs:
                if applied_this_platform >= budget:
                    break
                if self._stop_requested:
                    logger.info(
                        "Stop requested, finishing %s after current job",
                        platform_key,
                    )
                    break

                # Fetch description (also checks liveness on the same page)
                job = await fetch_description(platform, job)

                # Check for CAPTCHA before proceeding
                page = await platform.get_page()
                if await platform.detect_captcha(page):
                    self.events.emit(CAPTCHA_DETECTED, platform=platform_key)
                    return  # Hard stop

                # Skip dead listings immediately — can't apply anyway,
                # so scoring would be wasted work.
                #
                # External jobs are different: we WANT to score them so
                # `cli almost` can surface high-score externals as
                # manual-apply candidates. This trades ~20-60s of LLM
                # time per external job for the ability to tell the
                # user "these 9/10 jobs are worth applying to manually."
                if job.liveness in ("dead", "external"):
                    self.skipped_count += 1
                    ext_score = 0
                    ext_resume = ""
                    if job.liveness == "external":
                        try:
                            external_score_result = await self.scorer.score(
                                job.description, cli_mode=self.cli_mode,
                            )
                            ext_score = external_score_result.score
                            ext_resume = external_score_result.resume_label
                            logger.info(
                                "Skipped (apply on company site): %s — "
                                "score %d/10 with resume '%s'",
                                job.title[:60], ext_score, ext_resume,
                            )
                        except Exception as exc:
                            logger.debug(
                                "Could not score external job %s: %s",
                                job.job_id, exc,
                            )
                            logger.info(
                                "Skipped (apply on company site): %s",
                                job.title[:60],
                            )
                    else:
                        logger.info(
                            "Skipped (this job is no longer open): %s",
                            job.title[:60],
                        )
                    reason = (
                        "This job is no longer open"
                        if job.liveness == "dead"
                        else "Company wants you to apply on their own website"
                    )
                    save(
                        Application(
                            job_id=job.job_id,
                            status="skipped",
                            source=platform_key,
                            resume_used=ext_resume,
                            score=ext_score,
                            failure_reason=reason,
                        )
                    )
                    continue

                # Ghost-job check: does this posting look real? Runs a
                # short LLM call to spot recycled/fake listings. Fails
                # open — an unavailable check never blocks real jobs.
                # Skipped on dry runs: the whole point of ghost checks
                # is saving apply quota, and dry runs don't spend
                # quota. Each skip saves 5-30 seconds of CPU LLM time.
                if not self.dry_run:
                    from auto_applier.analysis.ghost_check import (
                        GhostJobChecker, should_skip_ghost,
                    )
                    from auto_applier.config import GHOST_SKIP_THRESHOLD
                    try:
                        ghost_result = await GhostJobChecker(self.router).check(
                            job_description=job.description,
                            company_name=job.company,
                            job_title=job.title,
                        )
                    except Exception:
                        ghost_result = None
                    if ghost_result is not None:
                        job.ghost_score = ghost_result.score
                        job.ghost_verdict = ghost_result.verdict
                        if should_skip_ghost(
                            ghost_result.score,
                            ghost_result.confidence,
                            GHOST_SKIP_THRESHOLD,
                        ):
                            self.skipped_count += 1
                            save(
                                Application(
                                    job_id=job.job_id,
                                    status="skipped",
                                    source=platform_key,
                                    resume_used="",
                                    score=0,
                                    failure_reason=(
                                        f"This job may not be real "
                                        f"(ghost listing — {ghost_result.verdict})"
                                    ),
                                )
                            )
                            continue

                # Score the job against all resumes
                job_score = await self.scorer.score(
                    job.description, cli_mode=self.cli_mode
                )
                logger.info(
                    "Score: %s @ %s → %.1f (%s) [resume=%s]",
                    job.title[:50], platform_key,
                    job_score.score, job_score.decision.value,
                    job_score.resume_label,
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
                        # In GUI mode, QUEUE the job for batch review
                        # at the end of the run. Blocking mid-pipeline
                        # on every borderline score made the run feel
                        # glacial AND starved later platforms of time.
                        # Users now see a single review queue after
                        # all platforms finish scraping + scoring.
                        #
                        # The queued job counts against this platform's
                        # budget too — otherwise a platform that only
                        # turns up borderline scores could queue 50
                        # jobs while the budget is nominally 3,
                        # and the review queue would explode.
                        self.pending_review.append({
                            "job": job,
                            "score": job_score,
                            "platform_key": platform_key,
                            "resume_label": job_score.resume_label,
                        })
                        applied_this_platform += 1
                        # Emit event so dashboard can update a counter
                        self.events.emit(
                            USER_REVIEW_NEEDED, job=job, score=job_score,
                        )
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
                    logger.info(
                        "Apply: %s @ %s → %s [%d/%d fields]",
                        job.title[:50], platform_key, app.status,
                        app.fields_filled, app.fields_total,
                    )
                else:
                    self.failed_count += 1
                    logger.info(
                        "Apply FAILED: %s @ %s → %s",
                        job.title[:50], platform_key,
                        app.failure_reason[:80] if app.failure_reason else "unknown",
                    )

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
