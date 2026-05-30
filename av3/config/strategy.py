"""Pareto-configurable strategy profiles (spec §8a) — Phase 6 / v3.1.

v3.0 shipped *fixed* pacing (``PacingConfig``: one delay range, one daily target, one
per-company/day cap). v3.1 retires the rigid single point and exposes the
**throughput ↔ detection-risk ↔ user-effort** frontier as named **profiles**, each a
coherent point on it (spec §8a):

  | Profile    | Delays    | Daily target | Per-company/day | Risk router     |
  |------------|-----------|--------------|-----------------|-----------------|
  | Cautious   | long      | low          | 1               | leans assisted  |
  | Balanced * | moderate  | moderate     | 2               | balanced        |
  | Aggressive | short     | high         | 3               | leans auto      |
  | Custom     | hand-set in ``settings.pacing`` (incl. its ``risk_bias``)        |

  (* default — its preset values are IDENTICAL to the v3.0 ``PacingConfig`` defaults, so
  a fresh install behaves exactly as v3.0 did. Choosing a non-default profile is opt-in.)

**Why a resolver, not branching at every call site.** The apply worker (and any future
consumer) asks :func:`resolve_strategy` once for an :class:`EffectivePacing` and reads
the concrete knobs off it — the profile→knobs mapping lives in ONE place. ``CUSTOM``
falls through to the user's hand-set ``settings.pacing`` (the historical carrier); every
other profile uses its frozen preset and *ignores* ``settings.pacing`` (documented, so a
user who wants their own numbers picks ``custom``).

**Safety floor is NOT here (spec §8a).** Manual login, headed browser, never-retry-
through-CAPTCHA, and downgrade-to-assisted *on a detection signal* are invariants — they
are never represented as tunable knobs. ``risk_bias`` only shifts the *starting* posture
(how readily the bot stays auto vs. assisted **before** any detection signal); a real
challenge still forces assisted regardless of profile.

**Scope of this sub-phase (Phase 6 2/M).** Profiles drive the knobs that already have
consumption points: inter-apply **delay range**, soft **daily target**, **per-company/
day** cap, and **risk-router bias** (→ auto vs. assisted starting mode). The two
scheduler-architecture knobs from the spec table — **concurrency** (sources in parallel)
and **session rotation** (time-box per source then rotate) — are deferred: the v3.0
scheduler drains stages sequentially and rotation needs per-source session bookkeeping
that doesn't exist yet. They land in a later sub-phase. ``EffectivePacing`` deliberately
omits fields for them so a half-wired knob can't masquerade as live.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle (settings.py imports the enums below)
    from av3.config.settings import Settings

__all__ = [
    "EffectivePacing",
    "RiskBias",
    "StrategyProfile",
    "PROFILE_PRESETS",
    "resolve_strategy",
]


class StrategyProfile(str, Enum):
    """Named point on the throughput/risk/effort frontier (spec §8a). ``str`` mixin so it
    round-trips through ``user_config.json`` as a plain string."""

    CAUTIOUS = "cautious"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class RiskBias(str, Enum):
    """Starting auto-vs-assisted posture (spec §8a "risk-router bias"). NOT the safety
    floor — a detection signal still forces assisted regardless of this value.

      * ``LEANS_ASSISTED`` — start every job in assisted (bot pre-fills, human submits),
        even when the caller requested auto. The cautious, low-effort-to-trust posture.
      * ``BALANCED`` — honour the caller's requested mode.
      * ``LEANS_AUTO`` — honour the caller's requested mode (auto where the caller asked
        for auto). Distinct from BALANCED for forward-compat: a future per-job §8 router
        will read this to bias borderline calls toward auto.
    """

    LEANS_ASSISTED = "leans_assisted"
    BALANCED = "balanced"
    LEANS_AUTO = "leans_auto"


@dataclass(frozen=True)
class EffectivePacing:
    """The concrete pacing knobs a profile resolves to — what the apply worker reads.

    Frozen so a resolved value can't be mutated mid-run. Deliberately has NO concurrency
    / session-rotation field (those knobs aren't wired yet — see module docstring)."""

    min_delay_s: float
    max_delay_s: float
    daily_target: int
    max_per_company_per_day: int
    risk_bias: RiskBias
    profile: StrategyProfile  # which profile produced this (for telemetry / dashboard)


#: Frozen per-profile presets. Balanced == the v3.0 ``PacingConfig`` defaults (so a fresh
#: install is byte-for-byte v3.0 behaviour). Cautious widens delays + drops volume + leans
#: assisted; Aggressive tightens delays + raises volume + leans auto. CUSTOM is absent on
#: purpose — it resolves from ``settings.pacing`` instead of a preset.
PROFILE_PRESETS: dict[StrategyProfile, EffectivePacing] = {
    StrategyProfile.CAUTIOUS: EffectivePacing(
        min_delay_s=120.0, max_delay_s=300.0, daily_target=10,
        max_per_company_per_day=1, risk_bias=RiskBias.LEANS_ASSISTED,
        profile=StrategyProfile.CAUTIOUS,
    ),
    StrategyProfile.BALANCED: EffectivePacing(
        min_delay_s=60.0, max_delay_s=180.0, daily_target=30,
        max_per_company_per_day=2, risk_bias=RiskBias.BALANCED,
        profile=StrategyProfile.BALANCED,
    ),
    StrategyProfile.AGGRESSIVE: EffectivePacing(
        min_delay_s=20.0, max_delay_s=60.0, daily_target=100,
        max_per_company_per_day=3, risk_bias=RiskBias.LEANS_AUTO,
        profile=StrategyProfile.AGGRESSIVE,
    ),
}


def resolve_strategy(settings: "Settings") -> EffectivePacing:
    """Resolve the active :class:`EffectivePacing` for these settings.

    ``profile == CUSTOM`` → build from the user's hand-set ``settings.pacing`` (including
    its ``risk_bias``). Any named profile → its frozen preset, ignoring ``settings.pacing``
    (a user who wants custom numbers selects ``custom`` — documented in §8a).
    """
    profile = settings.strategy.profile
    if profile is StrategyProfile.CUSTOM:
        pacing = settings.pacing
        return EffectivePacing(
            min_delay_s=pacing.min_delay_s,
            max_delay_s=pacing.max_delay_s,
            daily_target=pacing.daily_target,
            max_per_company_per_day=pacing.max_per_company_per_day,
            risk_bias=pacing.risk_bias,
            profile=StrategyProfile.CUSTOM,
        )
    return PROFILE_PRESETS[profile]
