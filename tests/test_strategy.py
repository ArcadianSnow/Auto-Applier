"""Strategy profiles (spec §8a, Phase 6 2/M) — config + resolution contract.

Covers the profile→knobs mapping in :mod:`auto_applier.config.strategy`:
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

from auto_applier.config import Settings, load_settings
from auto_applier.config.settings import PacingConfig, StrategyConfig
from auto_applier.config.strategy import (
    PROFILE_PRESETS,
    EffectivePacing,
    RiskBias,
    SessionRotationPolicy,
    StrategyProfile,
    resolve_strategy,
)


class _Clock:
    """Deterministic injectable monotonic clock — reads return the current time WITHOUT
    advancing; the test moves time forward explicitly with :meth:`advance`. Matches how
    SessionRotationPolicy reads ``now()`` (idempotent reads within one decision)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


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


# --------------------------------------------------------------- concurrency + rotation knobs (8/M)

def test_presets_carry_concurrency_and_rotation_knobs():
    """8/M: each named preset carries a concurrency ceiling + a session-rotation budget.
    Balanced MUST stay (1, 0.0) — the v3.0 backward-compat invariant for the new knobs."""
    cau = PROFILE_PRESETS[StrategyProfile.CAUTIOUS]
    bal = PROFILE_PRESETS[StrategyProfile.BALANCED]
    agg = PROFILE_PRESETS[StrategyProfile.AGGRESSIVE]
    assert (bal.concurrency, bal.session_rotation_min) == (1, 0.0)  # v3.0 invariant
    assert cau.concurrency == 1 and cau.session_rotation_min > 0.0
    assert agg.concurrency == 3 and agg.session_rotation_min > cau.session_rotation_min


def test_custom_profile_passes_through_concurrency_and_rotation():
    """CUSTOM resolves the new knobs from the hand-set settings.pacing, like every other
    custom-carried knob."""
    s = Settings(
        strategy=StrategyConfig(profile=StrategyProfile.CUSTOM),
        pacing=PacingConfig(concurrency=4, session_rotation_min=12.5),
    )
    ep = resolve_strategy(s)
    assert ep.concurrency == 4
    assert ep.session_rotation_min == 12.5


def test_session_rotation_disabled_when_zero():
    """rotation_min <= 0 → policy is inert: never rotates no matter how much time passes
    (the Balanced / v3.0 default)."""
    clk = _Clock()
    rot = SessionRotationPolicy(0.0, now=clk)
    assert rot.enabled is False
    rot.on_source("lever")
    clk.advance(10_000.0)
    assert rot.should_rotate() is False


def test_session_rotation_fires_after_budget_elapses():
    """With a 10-min budget, should_rotate flips True once 600s have elapsed on the
    source — and not a moment before."""
    clk = _Clock()
    rot = SessionRotationPolicy(10.0, now=clk)  # 600s
    assert rot.enabled is True
    rot.on_source("lever")  # started at t=0
    assert rot.should_rotate() is False
    clk.advance(599.0)
    assert rot.should_rotate() is False
    clk.advance(1.0)  # exactly at budget
    assert rot.should_rotate() is True


def test_session_rotation_resets_timer_on_source_change():
    """Switching to a NEW source restarts the per-source budget — rotation is per source,
    not a global wall clock."""
    clk = _Clock()
    rot = SessionRotationPolicy(10.0, now=clk)  # 600s
    rot.on_source("lever")  # started t=0
    clk.advance(600.0)
    assert rot.should_rotate() is True
    rot.on_source("greenhouse")  # source changed → timer restarts at t=600
    assert rot.should_rotate() is False
    clk.advance(600.0)  # t=1200, 600s on greenhouse
    assert rot.should_rotate() is True


def test_session_rotation_same_source_does_not_reset_timer():
    """Processing more jobs of the SAME source must NOT reset the timer — otherwise the
    budget could never elapse on a single-source queue."""
    clk = _Clock()
    rot = SessionRotationPolicy(10.0, now=clk)  # 600s
    rot.on_source("lever")  # started t=0
    clk.advance(600.0)
    rot.on_source("lever")  # same source → timer NOT reset
    assert rot.should_rotate() is True
