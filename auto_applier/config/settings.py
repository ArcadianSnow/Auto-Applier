"""Pydantic Settings models for v3.

Design (spec §10): smart defaults out of the box; power users retune in
``user_config.json``. Validation runs on construction so ``doctor`` fails fast.
Any future secrets live only in ``.env`` (never in the JSON), matching v2's
credential flow; ``.env`` is still loaded, though the pipeline is now fully local
and reads no secret keys (the Gemini cloud tier was removed).

Precedence: ``user_config.json`` is the primary, inspectable config. ``AV3_DATA_DIR``
env var can relocate the data dir (used by tests and alternate installs).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from auto_applier.config.strategy import RiskBias, StrategyProfile

def _default_data_dir() -> Path:
    """A stable per-user data location, so an install SEPARATE FROM THE REPO (pip install
    + a Start Menu shortcut, no git checkout) doesn't scatter ``data/v3`` into whatever the
    current working directory happens to be. Overridden by ``AV3_DATA_DIR`` (tests + custom
    installs). Windows → ``%LOCALAPPDATA%\\AutoApplier\\data``; POSIX → ``$XDG_DATA_HOME`` or
    ``~/.local/share`` + ``auto-applier``.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AutoApplier" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "auto-applier"


DEFAULT_DATA_DIR = _default_data_dir()


class ScoringWeights(BaseModel):
    """Seven weighted scoring axes (spec §10). Must sum to ~1.0."""

    skills: float = 0.35
    experience: float = 0.20
    seniority: float = 0.15
    location: float = 0.10
    culture: float = 0.08
    growth: float = 0.07
    compensation: float = 0.05

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringWeights":
        total = (
            self.skills
            + self.experience
            + self.seniority
            + self.location
            + self.culture
            + self.growth
            + self.compensation
        )
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"scoring weights must sum to 1.0 (got {total:.3f}); adjust user_config.json"
            )
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class ScoringConfig(BaseModel):
    """Decision thresholds + axis weights (spec §10, §5)."""

    auto_apply_min: float = 7.0
    review_min: float = 4.0
    ghost_skip_threshold: float = 8.0
    weights: ScoringWeights = Field(default_factory=ScoringWeights)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "ScoringConfig":
        if self.review_min >= self.auto_apply_min:
            raise ValueError(
                f"review_min ({self.review_min}) must be < auto_apply_min ({self.auto_apply_min})"
            )
        return self


class LLMConfig(BaseModel):
    """LLM backend config. Local Ollama → deterministic bank/rule floor (spec §6).

    The former cloud secondary tier (Gemini) was removed once ``gemini-1.5-flash`` was
    retired; the product is local-first and zero-cost, so Ollama is the only model tier.
    """

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"
    embed_model: str = "nomic-embed-text"  # spec resolved default (fast over accurate)


class PacingConfig(BaseModel):
    """Pacing knobs — the carrier for the ``custom`` strategy profile (spec §8a).

    These defaults are intentionally identical to the **Balanced** preset
    (:data:`auto_applier.config.strategy.PROFILE_PRESETS`), so a fresh install with the default
    ``strategy.profile = balanced`` behaves exactly as v3.0 did. When
    ``strategy.profile != custom`` the named preset wins and these values are ignored
    (select ``custom`` to use them — see :func:`auto_applier.config.strategy.resolve_strategy`).

    Safety floor (manual login, headed, never retry through CAPTCHA, downgrade on a
    detection signal) is NOT represented here — it is never tunable by config. ``risk_bias``
    only shifts the *starting* auto-vs-assisted posture, not the floor.
    """

    min_delay_s: float = 60.0
    max_delay_s: float = 180.0
    daily_target: int = 30  # soft goal, never a hard wall
    max_per_company_per_day: int = 2  # re-apply rate limit (spec §7)
    risk_bias: RiskBias = RiskBias.BALANCED  # custom-profile starting posture (§8a)
    concurrency: int = 1  # custom-profile parallel-apply ceiling (§8a 8/M)
    session_rotation_min: float = 0.0  # custom-profile per-source time-box, min (§8a 8/M)

    @model_validator(mode="after")
    def _delays_ordered(self) -> "PacingConfig":
        if self.min_delay_s > self.max_delay_s:
            raise ValueError("min_delay_s must be <= max_delay_s")
        return self


class StrategyConfig(BaseModel):
    """Pareto strategy-profile selector (spec §8a, Phase 6 / v3.1).

    ``profile`` picks a coherent point on the throughput/detection-risk/user-effort
    frontier. The named profiles (cautious / balanced / aggressive) carry frozen presets
    in :data:`auto_applier.config.strategy.PROFILE_PRESETS`; ``custom`` falls through to the
    hand-set :class:`PacingConfig`. Default is **balanced**, whose preset equals the
    PacingConfig defaults — so the selector is inert until a user opts into another point.
    """

    profile: StrategyProfile = StrategyProfile.BALANCED


class SalaryConfig(BaseModel):
    """Salary intelligence inputs (spec §8d, Phase 6). All optional — with nothing set,
    the resolver simply has no ask to compute and bails salary questions to REVIEW.

    ``floor`` is also the **comp-filter** floor: the score worker SKIPs a job whose posted
    range is entirely below it (saves a wasted application, §8d). ``market_source`` selects
    a pluggable wage source; default ``"none"`` keeps the pipeline local-first (no egress) —
    the BLS OES adapter is an opt-in future entry (see ``build_market_source``).
    """

    floor: int | None = None       # USD/year; lower bound on the ask AND the comp-filter floor
    ceiling: int | None = None     # USD/year; used as the ask when no posted/market data
    market_source: str = "none"    # "none" (local-first default) | future "bls_oes"

    @model_validator(mode="after")
    def _floor_ceiling_ordered(self) -> "SalaryConfig":
        if self.floor is not None and self.ceiling is not None and self.floor > self.ceiling:
            raise ValueError(f"salary floor ({self.floor}) must be <= ceiling ({self.ceiling})")
        return self


class TelemetryConfig(BaseModel):
    """Opt-in remote error mirror (spec §9). Default OFF — only network egress in the product."""

    enabled: bool = False
    handle: str | None = None  # raw name stays local; we send sha256(handle)[:10]
    relay_url: str | None = None


class TargetingConfig(BaseModel):
    """Job-targeting filters (spec §6c — NL intent → structured filters).

    v3.0 ships the structured form straight (hand-edit in onboarding); the
    LLM-parse-from-NL step is a (5/M) onboarding nicety that lands later — the
    underlying storage shape is the structured filter set either way so the
    pipeline doesn't need to know which entry path was used.

    Empty lists mean "no constraint on this axis" — the discovery producer
    defaults to whatever the source's bounded breadth allows (spec §7b).
    """

    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    onsite_ok: bool = True
    salary_floor: int | None = None  # USD/year; None = no floor
    seniority: str = ""              # "junior" | "mid" | "senior" | "staff" | "" any

    # Soft preference signals (free-text phrases like "strong work-life balance",
    # "Python/Postgres stack", "no on-call") gathered by the onboarding goal-elicitation
    # chat (research/future-directions.md Direction 1, Phase B). NOT a hard filter today —
    # a forward hook for Phase C's bounded LLM ranker, which ranks dataset candidates against
    # these. Stored here so the structured-or-conversational entry path is invisible downstream.
    preferences: list[str] = Field(default_factory=list)

    # ATS board identifiers the discovery producer sweeps (research/ats-discovery-seeding.md).
    # These are per-company slugs/tokens you already know — there is no list-all endpoint
    # for any ATS. Seeded with a small confirmed-live starter set (confirm-probe sweep
    # 2026-05-26); grow them by hand or via the seeding workflow. Dead tokens are skipped
    # at discovery, so generous lists are safe. The `discover` CLI + the scheduler both
    # read these so headless `av3 run` and ad-hoc `av3 discover` stay in lockstep.
    greenhouse_boards: list[str] = Field(
        default_factory=lambda: [
            "anthropic", "cloudflare", "tripadvisor", "figma",
            "discord", "reddit", "gitlab", "robinhood",
        ]
    )
    lever_boards: list[str] = Field(
        default_factory=lambda: ["matchgroup", "highspot"]
    )
    ashby_boards: list[str] = Field(
        default_factory=lambda: ["Ashby", "Linear", "Ramp", "Vanta", "Notion", "OpenAI"]
    )


class SchedulerConfig(BaseModel):
    """Always-on staged-worker loop tuning (spec §7a — fixed pacing for v3.0).

    Drives the :class:`auto_applier.pipeline.Scheduler` cycle. Pareto strategy profiles
    (Cautious/Balanced/Aggressive) are v3.1; v3.0 ships these fixed knobs and
    stops. Quiet hours pause ONLY the apply worker (gather stages keep running
    because being-wrong in gather doesn't compound — Rule 2.6).
    """

    cycle_interval_s: float = 60.0  # seconds between staged-loop cycles
    quiet_hours: str | None = None  # "HH:MM-HH:MM" local time, or None for 24/7

    # Batched assisted review (research/batched-assisted-review.md). When ON (the web-dashboard
    # posture), the apply stage prepares ``batch_review_size`` jobs then HOLDS on the apply_gate so
    # the owner can verify / submit each on the "In Progress" page before the next N are prepared.
    # OFF (default) keeps today's behavior — apply drains continuously — so headless ``av3 run``
    # and the existing tests are unchanged; the dashboard turns it on.
    batched_review: bool = False
    batch_review_size: int = 5      # N jobs prepared per batch before the barrier holds

    @model_validator(mode="after")
    def _cycle_interval_positive(self) -> "SchedulerConfig":
        if self.cycle_interval_s <= 0:
            raise ValueError("cycle_interval_s must be > 0")
        if self.batch_review_size < 1:
            raise ValueError("batch_review_size must be >= 1")
        return self


class WebConfig(BaseModel):
    """Local web app + worker service config (spec §3, §10 — Phase 4).

    The dashboard binds to localhost by default so an unattended runner doesn't
    silently expose pipeline state to the LAN. Set ``host: "0.0.0.0"`` in
    ``user_config.json`` to enable the "control/monitor from any device's
    browser, incl. a dedicated runner box" mode from spec §2. The port default
    (8765) is chosen to avoid the common 8000/8080 collision space; tests pass
    port=0 to let the OS pick a free port.

    (3/M) adds the control-handoff knobs from spec §7a: ``hotkey_enabled`` is
    the F6 system-level hotkey (Windows-native primary; soft-fail elsewhere),
    and ``idle_detect_enabled`` is the OPTIONAL idle-detection companion that
    auto-pauses while the user is actively interacting with the machine.
    """

    host: str = "127.0.0.1"
    port: int = 8765

    # Phase 4 (3/M) — F6 control-handoff hotkey.
    # ``hotkey_enabled`` defaults ON because (a) it's the spec default,
    # (b) the Windows-only watcher soft-fails on non-Windows so flipping the
    # default doesn't break anyone, and (c) the dashboard pause button still
    # works without it.
    hotkey_enabled: bool = True
    hotkey: str = "F6"

    # Phase 4 (3/M) — optional idle-detect.
    # Defaults OFF: spec §7a explicitly says "*Optional* idle-detection
    # complements [F6]." On a shared machine many users would rather the bot
    # NOT pause on every keystroke; F6 is enough. The (5/M) onboarding wizard
    # asks.
    idle_detect_enabled: bool = False
    idle_threshold_s: float = 60.0
    idle_poll_s: float = 2.0

    @model_validator(mode="after")
    def _port_in_range(self) -> "WebConfig":
        # 0 is a sentinel meaning "let the OS pick" — used in tests so they don't
        # collide on a fixed port when running in parallel. Real defaults stay
        # in the 1024..65535 user range.
        if self.port == 0:
            return self
        if not (1024 <= self.port <= 65535):
            raise ValueError(f"port must be 1024..65535 (got {self.port})")
        return self

    @model_validator(mode="after")
    def _idle_knobs_sane(self) -> "WebConfig":
        if self.idle_threshold_s <= 0:
            raise ValueError("idle_threshold_s must be > 0")
        if self.idle_poll_s <= 0:
            raise ValueError("idle_poll_s must be > 0")
        return self


class RetentionConfig(BaseModel):
    """Data lifecycle (spec §4). Defaults match the spec's "e.g. 30d" guidance
    for app data and the spec's "shorter window" for events. Backups
    rotate so cron'd snapshots don't fill the disk over months."""

    ephemeral_days: int = 30          # SKIPPED/FILTERED job rows older than this go away
    events_days: int = 14             # events.db rows older than this go away (shorter — higher write rate)
    backup_keep: int = 10             # rotate snapshots: keep newest N per DB
    maintenance_interval_s: float = 3600.0  # how often the scheduler runs prune+backup (default 1 hour)

    @model_validator(mode="after")
    def _positive_windows(self) -> "RetentionConfig":
        if self.ephemeral_days <= 0:
            raise ValueError("ephemeral_days must be > 0")
        if self.events_days <= 0:
            raise ValueError("events_days must be > 0")
        if self.backup_keep <= 0:
            raise ValueError("backup_keep must be > 0")
        if self.maintenance_interval_s <= 0:
            raise ValueError("maintenance_interval_s must be > 0")
        return self


class InboxConfig(BaseModel):
    """Local-first email outcome loop config (email-outcome-loop, Direction 4).

    Non-secret IMAP connection settings. The app-password is NEVER a field here — it
    is read from ``os.environ["AV3_IMAP_PASSWORD"]`` at connect time (Phase C), matching
    the project's `.env`-only secrets rule (never in user_config.json).

    Default OFF + Gmail defaults: a fresh install reads no mail until the user opts in
    (sets ``enabled`` + ``user``). The offline ``av3 inbox --eml`` path works regardless,
    so Phase B is fully exercisable without ever enabling this.
    """

    enabled: bool = False
    host: str = "imap.gmail.com"
    port: int = 993
    user: str | None = None
    folder: str = "INBOX"
    since_days: int = 30
    poll_interval_s: float = 300.0

    @model_validator(mode="after")
    def _knobs_sane(self) -> "InboxConfig":
        if not (1 <= self.port <= 65535):
            raise ValueError(f"inbox.port must be 1..65535 (got {self.port})")
        if self.since_days <= 0:
            raise ValueError("inbox.since_days must be > 0")
        if self.poll_interval_s <= 0:
            raise ValueError("inbox.poll_interval_s must be > 0")
        return self


class Settings(BaseModel):
    """Root settings object. Construct via ``load_settings()``."""

    data_dir: Path = DEFAULT_DATA_DIR
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    salary: SalaryConfig = Field(default_factory=SalaryConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    targeting: TargetingConfig = Field(default_factory=TargetingConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)

    #: Owner opt-in (default OFF): auto-fill a STATIC "which best describes you? [human/AI]"
    #: self-ID FORM FIELD with the human option. The applicant is human, and such a field is a
    #: routine form question, NOT a behavioural/risk-scored anti-bot challenge (CAPTCHA /
    #: fingerprinting are classified separately and are NEVER automated through). OFF keeps the
    #: bail-to-assisted default. (User-directed 2026-06-14; the real anti-bot path is untouched.)
    attest_human: bool = False

    #: Assisted-mode freeform drafting (BUILD 6 Phase B, default OFF). When ON, an open-ended /
    #: essay application field with no banked answer is DRAFTED by the §8f copilot and pre-filled
    #: for the human to edit, instead of bailing blank — and the draft ALWAYS forces the job to
    #: assisted, so the bot never auto-submits an AI-written essay. OFF keeps the safe bail-blank
    #: default. ("Assisted" = the AI drafts the freeform answer, the human spot-checks + submits.)
    draft_freeform_answers: bool = False

    #: Score floor for cover-letter autogen (``av3 cover --generate-all``; BUILD 5). A strong
    #: match (total ≥ this) gets a tailored, guard-checked .docx letter written and ready "just
    #: in case", written into ``uploads/<job_id>/`` only if one isn't already there (a manual
    #: ``av3 cover`` always wins — autogen only fills the gap). Default 8.0 = the user's "scored
    #: decently" bar; the CLI ``--min-score`` overrides per-run. (User-directed 2026-06-14.)
    cover_autogen_min_score: float = 8.0

    # --- derived paths (system of record + observability spine, spec §4) ---
    @property
    def app_db_path(self) -> Path:
        """Main DB: jobs, scores, applications, skill_gaps, answers."""
        return self.data_dir / "app.db"

    @property
    def events_db_path(self) -> Path:
        """Separate events.db — the observability spine. Pruned/rotated independently
        of app.db and is the highest-write-volume table (spec §9, §4 retention)."""
        return self.data_dir / "events.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / ".backups"

    @property
    def artifacts_dir(self) -> Path:
        """Generated résumés / cover letters live as files; DB stores paths (spec §4)."""
        return self.data_dir / "artifacts"

    @property
    def shortlist_dir(self) -> Path:
        """Saved manual/human-apply shortlists (``av3 shortlist`` → .md + .json views)."""
        return self.data_dir / "shortlist"

    @property
    def uploads_dir(self) -> Path:
        """Per-job, upload-ready application files (spec §6b, anti-detection §8c).

        Each job's files live under ``uploads_dir / <job_id>/`` with **generic basenames**
        (e.g. ``Cover Letter.docx``) — Playwright uploads a file under its basename, so a
        per-posting name like ``CoverLetter_Tailscale_SE_Commercial.docx`` is a mass-apply
        fingerprint; the per-job identity lives in the folder path, not the uploaded name.
        ``av3 cover`` writes here; the apply worker reads here; on a confirmed APPLIED the
        worker moves the file to ``uploads_dir / "_archive"`` with the job id appended. See
        ``auto_applier.resume.generate`` (job_cover_upload_path / existing_job_cover /
        assign_cover_letter / archive_cover_letter)."""
        return self.artifacts_dir / "uploads"

    @property
    def story_bank_path(self) -> Path:
        """STAR+R interview story bank (``av3 stories`` — file-grain prep library)."""
        return self.data_dir / "story_bank.json"

    @property
    def research_dir(self) -> Path:
        """Company-research briefings (``av3 research`` — md + json per company)."""
        return self.data_dir / "research"

    @property
    def review_batch_path(self) -> Path:
        """Durable sidecar for the batched assisted-review barrier (``ReviewBatch``): the current
        batch's id + per-job dispositions, so a mid-batch restart resumes the grouping instead of
        starting empty. Local-only control state; never mirrored to telemetry."""
        return self.data_dir / "review_batch.json"

    @property
    def browser_profile_dir(self) -> Path:
        """One persistent shared Chrome profile across all sites (spec §8c)."""
        return self.data_dir / "browser_profile"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "user_config.json"


def load_settings(data_dir: Path | str | None = None) -> Settings:
    """Load and validate settings.

    Order: resolve data_dir (arg → ``AV3_DATA_DIR`` env → default) → read
    ``user_config.json`` if present → overlay secrets from ``.env``. Validation
    (weight sums, ordered thresholds) raises on bad config — caught by ``doctor``.
    """
    load_dotenv()  # populate os.environ from a project-root/CWD .env (no-op if absent)

    if data_dir is None:
        data_dir = os.environ.get("AV3_DATA_DIR", str(DEFAULT_DATA_DIR))
    data_dir = Path(data_dir)

    # Also load a .env inside the data dir — this is where the onboarding "connect
    # email" step writes AV3_IMAP_PASSWORD, so a packaged/non-dev install (no project
    # checkout) still picks up the secret. override=False so an existing project-root
    # .env (the dev/owner setup) stays authoritative.
    data_env = data_dir / ".env"
    if data_env.exists():
        load_dotenv(data_env, override=False)

    cfg_path = data_dir / "user_config.json"
    file_data: dict = {}
    if cfg_path.exists():
        file_data = json.loads(cfg_path.read_text(encoding="utf-8"))

    file_data["data_dir"] = str(data_dir)

    return Settings(**file_data)
