"""Pydantic Settings models for v3.

Design (spec §10): smart defaults out of the box; power users retune in
``user_config.json``. Validation runs on construction so ``doctor`` fails fast.
Secrets live only in ``.env`` (never in the JSON), matching v2's credential flow.

Precedence: ``user_config.json`` is the primary, inspectable config. ``.env`` supplies
secrets (currently ``GEMINI_API_KEY``). ``AV3_DATA_DIR`` env var can relocate the data
dir (used by tests and alternate installs).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

DEFAULT_DATA_DIR = Path("data/v3")


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
    """LLM backend config. Fallback chain Ollama → Gemini → rule (spec §6, ported from v2)."""

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"
    embed_model: str = "nomic-embed-text"  # spec resolved default (fast over accurate)
    gemini_api_key: str | None = None  # injected from .env, never stored in JSON


class PacingConfig(BaseModel):
    """v3.0 fixed pacing (spec §8a — Pareto strategy profiles are v3.1).

    Safety floor (manual login, headed, never retry through CAPTCHA, downgrade on
    detection) is NOT represented here — it is never tunable by config.
    """

    min_delay_s: float = 60.0
    max_delay_s: float = 180.0
    daily_target: int = 30  # soft goal, never a hard wall
    max_per_company_per_day: int = 2  # re-apply rate limit (spec §7)

    @model_validator(mode="after")
    def _delays_ordered(self) -> "PacingConfig":
        if self.min_delay_s > self.max_delay_s:
            raise ValueError("min_delay_s must be <= max_delay_s")
        return self


class TelemetryConfig(BaseModel):
    """Opt-in remote error mirror (spec §9). Default OFF — only network egress in the product."""

    enabled: bool = False
    handle: str | None = None  # raw name stays local; we send sha256(handle)[:10]
    relay_url: str | None = None


class Settings(BaseModel):
    """Root settings object. Construct via ``load_settings()``."""

    data_dir: Path = DEFAULT_DATA_DIR
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)

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
    def config_path(self) -> Path:
        return self.data_dir / "user_config.json"


def load_settings(data_dir: Path | str | None = None) -> Settings:
    """Load and validate settings.

    Order: resolve data_dir (arg → ``AV3_DATA_DIR`` env → default) → read
    ``user_config.json`` if present → overlay secrets from ``.env``. Validation
    (weight sums, ordered thresholds) raises on bad config — caught by ``doctor``.
    """
    load_dotenv()  # populate os.environ from .env if present (no-op if absent)

    if data_dir is None:
        data_dir = os.environ.get("AV3_DATA_DIR", str(DEFAULT_DATA_DIR))
    data_dir = Path(data_dir)

    cfg_path = data_dir / "user_config.json"
    file_data: dict = {}
    if cfg_path.exists():
        file_data = json.loads(cfg_path.read_text(encoding="utf-8"))

    file_data["data_dir"] = str(data_dir)

    # Inject secrets from environment (never read from the JSON file).
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        file_data.setdefault("llm", {})
        if isinstance(file_data["llm"], dict):
            file_data["llm"].setdefault("gemini_api_key", gemini_key)

    return Settings(**file_data)
