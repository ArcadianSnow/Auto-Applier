"""Typed configuration for v3 (Pydantic). See ``docs/v3-architecture.md`` §10.

``load_settings()`` is the single entry point: it reads ``<data_dir>/user_config.json``
(the inspectable, primary config), merges secrets from ``.env`` (GEMINI_API_KEY), and
validates everything up front so ``doctor`` can fail fast on bad config.
"""

from av3.config.settings import (
    LLMConfig,
    PacingConfig,
    RetentionConfig,
    SchedulerConfig,
    ScoringConfig,
    ScoringWeights,
    Settings,
    StrategyConfig,
    TargetingConfig,
    TelemetryConfig,
    WebConfig,
    load_settings,
)
from av3.config.strategy import (
    EffectivePacing,
    PROFILE_PRESETS,
    RiskBias,
    StrategyProfile,
    resolve_strategy,
)

__all__ = [
    "LLMConfig",
    "PacingConfig",
    "RetentionConfig",
    "SchedulerConfig",
    "ScoringConfig",
    "ScoringWeights",
    "Settings",
    "StrategyConfig",
    "TargetingConfig",
    "TelemetryConfig",
    "WebConfig",
    "load_settings",
    # strategy profiles (§8a)
    "EffectivePacing",
    "PROFILE_PRESETS",
    "RiskBias",
    "StrategyProfile",
    "resolve_strategy",
]
