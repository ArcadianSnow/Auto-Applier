"""Strategy profiles (spec §8a, Phase 6 2/M) — config + resolution contract.

Covers the profile→knobs mapping in :mod:`av3.config.strategy`:
  * Balanced is the default AND its preset equals the v3.0 PacingConfig defaults
    (the backward-compat invariant — a fresh install behaves exactly as v3.0).
  * Cautious / Aggressive presets move the knobs in the documented directions.
  * Custom falls through to the user's hand-set settings.pacing (incl. risk_bias).
  * The profile selector round-trips through user_config.json as a plain string.

Worker-level consumption (delays / per-company cap / soft daily target / risk-bias →
mode) is covered in test_apply_worker.py.
"""

from __future__ import annotations

import json

from av3.config import Settings, load_settings
from av3.config.settings import PacingConfig, StrategyConfig
from av3.config.strategy import (
    PROFILE_PRESETS,
    EffectivePacing,
    RiskBias,
    StrategyProfile,
    resolve_strategy,
)


# --------------------------------------------------------------- defaults / backward-compat

def test_default_profile_is_balanced():
    assert Settings().strategy.profile is StrategyProfile.BALANCED


def test_balanced_preset_equals_pacing_defaults():
    """THE backward-compat invariant: the Balanced preset must equal the v3.0 PacingConfig
    defaults, so a fresh install with the default profile behaves byte-for-byte as v3.0."""
    pacing = PacingConfig()
    bal = PROFILE_PRESETS[StrategyProfile.BALANCED]
    assert (bal.min_delay_s, bal.max_delay_s, bal.daily_target, bal.max_per_company_per_day) == (
        pacing.min_delay_s, pacing.max_delay_s, pacing.daily_target, pacing.max_per_company_per_day,
    )
    assert bal.risk_bias is RiskBias.BALANCED


def test_resolve_default_settings_yields_balanced():
    ep = resolve_strategy(Settings())
    assert isinstance(ep, EffectivePacing)
    assert ep.profile is StrategyProfile.BALANCED
    assert ep.min_delay_s == 60.0 and ep.max_delay_s == 180.0
    assert ep.daily_target == 30 and ep.max_per_company_per_day == 2


# --------------------------------------------------------------- named presets

def test_cautious_is_slower_lower_volume_and_leans_assisted():
    bal = PROFILE_PRESETS[StrategyProfile.BALANCED]
    cau = PROFILE_PRESETS[StrategyProfile.CAUTIOUS]
    assert cau.min_delay_s > bal.min_delay_s
    assert cau.max_delay_s > bal.max_delay_s
    assert cau.daily_target < bal.daily_target
    assert cau.max_per_company_per_day <= bal.max_per_company_per_day
    assert cau.risk_bias is RiskBias.LEANS_ASSISTED


def test_aggressive_is_faster_higher_volume_and_leans_auto():
    bal = PROFILE_PRESETS[StrategyProfile.BALANCED]
    agg = PROFILE_PRESETS[StrategyProfile.AGGRESSIVE]
    assert agg.min_delay_s < bal.min_delay_s
    assert agg.max_delay_s < bal.max_delay_s
    assert agg.daily_target > bal.daily_target
    assert agg.max_per_company_per_day >= bal.max_per_company_per_day
    assert agg.risk_bias is RiskBias.LEANS_AUTO


def test_resolve_returns_the_exact_preset_object_for_named_profiles():
    for p in (StrategyProfile.CAUTIOUS, StrategyProfile.BALANCED, StrategyProfile.AGGRESSIVE):
        s = Settings(strategy=StrategyConfig(profile=p))
        assert resolve_strategy(s) is PROFILE_PRESETS[p]


def test_named_profile_ignores_hand_set_pacing():
    """A named profile uses its preset and IGNORES settings.pacing (the user must pick
    'custom' to use hand-set numbers — documented in §8a)."""
    s = Settings(
        strategy=StrategyConfig(profile=StrategyProfile.AGGRESSIVE),
        pacing=PacingConfig(min_delay_s=999.0, max_delay_s=1000.0),
    )
    ep = resolve_strategy(s)
    assert ep.min_delay_s == PROFILE_PRESETS[StrategyProfile.AGGRESSIVE].min_delay_s
    assert ep.min_delay_s != 999.0


# --------------------------------------------------------------- custom

def test_custom_profile_uses_hand_set_pacing():
    s = Settings(
        strategy=StrategyConfig(profile=StrategyProfile.CUSTOM),
        pacing=PacingConfig(
            min_delay_s=5.0, max_delay_s=9.0, daily_target=7,
            max_per_company_per_day=4, risk_bias=RiskBias.LEANS_AUTO,
        ),
    )
    ep = resolve_strategy(s)
    assert ep.profile is StrategyProfile.CUSTOM
    assert ep.min_delay_s == 5.0 and ep.max_delay_s == 9.0
    assert ep.daily_target == 7 and ep.max_per_company_per_day == 4
    assert ep.risk_bias is RiskBias.LEANS_AUTO


# --------------------------------------------------------------- config round-trip

def test_profile_round_trips_through_user_config(data_dir, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    (data_dir / "user_config.json").write_text(
        json.dumps({"strategy": {"profile": "aggressive"}}), encoding="utf-8"
    )
    s = load_settings()
    assert s.strategy.profile is StrategyProfile.AGGRESSIVE
    assert resolve_strategy(s).daily_target == PROFILE_PRESETS[StrategyProfile.AGGRESSIVE].daily_target


def test_custom_risk_bias_round_trips_through_user_config(data_dir, monkeypatch):
    monkeypatch.setenv("AV3_DATA_DIR", str(data_dir))
    (data_dir / "user_config.json").write_text(
        json.dumps({
            "strategy": {"profile": "custom"},
            "pacing": {"risk_bias": "leans_assisted"},
        }),
        encoding="utf-8",
    )
    s = load_settings()
    assert resolve_strategy(s).risk_bias is RiskBias.LEANS_ASSISTED
