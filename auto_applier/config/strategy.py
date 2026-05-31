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

**Scope (Phase 6 2/M + 8/M).** 2/M wired the knobs that already had consumption points:
inter-apply **delay range**, soft **daily target**, **per-company/day** cap, and
**risk-router bias** (→ auto vs. assisted starting mode). 8/M adds the two
scheduler-architecture knobs from the spec table:

  * **concurrency** — how many applies a profile is willing to run in parallel. The v3.0
    apply worker still drains sequentially, so today this is a *declared ceiling* the
    presets carry (Cautious 1, Balanced 1, Aggressive 3) for the scheduler/dashboard to
    read; a future parallel drainer reads it without re-plumbing the profile.
  * **session rotation** — ``session_rotation_min``: time-box the apply session on one
    source, then rotate. Enforced by :class:`SessionRotationPolicy` (pure, clock-injectable)
    which the apply worker consults at the top of its per-job loop: once the budget on the
    current source elapses, the worker *softly* defers the rest (left in QUEUED_APPLY),
    exactly like the daily-target break. ``0.0`` = disabled = v3.0 behaviour.

Balanced keeps ``concurrency=1`` and ``session_rotation_min=0.0`` so the default profile is
still byte-for-byte v3.0.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # avoid a runtime import cycle (settings.py imports the enums below)
    from auto_applier.config.settings import Settings

__all__ = [
    "EffectivePacing",
    "RiskBias",
    "StrategyProfile",
    "PROFILE_PRESETS",
    "resolve_strategy",
    "SessionRotationPolicy",
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

    Frozen so a resolved value can't be mutated mid-run. ``concurrency`` is the profile's
    declared parallel-apply ceiling (read by the scheduler/dashboard; the v3.0 worker still
    drains sequentially). ``session_rotation_min`` is the per-source time-box in minutes
    (0.0 = disabled = v3.0 behaviour) enforced via :class:`SessionRotationPolicy`."""

    min_delay_s: float
    max_delay_s: float
    daily_target: int
    max_per_company_per_day: int
    risk_bias: RiskBias
    profile: StrategyProfile  # which profile produced this (for telemetry / dashboard)
    concurrency: int = 1  # declared parallel-apply ceiling (§8a; worker still sequential)
    session_rotation_min: float = 0.0  # per-source time-box in minutes (0 = off = v3.0)


#: Frozen per-profile presets. Balanced == the v3.0 ``PacingConfig`` defaults (so a fresh
#: install is byte-for-byte v3.0 behaviour). Cautious widens delays + drops volume + leans
#: assisted; Aggressive tightens delays + raises volume + leans auto. CUSTOM is absent on
#: purpose — it resolves from ``settings.pacing`` instead of a preset.
PROFILE_PRESETS: dict[StrategyProfile, EffectivePacing] = {
    StrategyProfile.CAUTIOUS: EffectivePacing(
        min_delay_s=120.0, max_delay_s=300.0, daily_target=10,
        max_per_company_per_day=1, risk_bias=RiskBias.LEANS_ASSISTED,
        profile=StrategyProfile.CAUTIOUS,
        concurrency=1, session_rotation_min=15.0,
    ),
    StrategyProfile.BALANCED: EffectivePacing(
        min_delay_s=60.0, max_delay_s=180.0, daily_target=30,
        max_per_company_per_day=2, risk_bias=RiskBias.BALANCED,
        profile=StrategyProfile.BALANCED,
        concurrency=1, session_rotation_min=0.0,  # MUST stay 0 = v3.0 behaviour
    ),
    StrategyProfile.AGGRESSIVE: EffectivePacing(
        min_delay_s=20.0, max_delay_s=60.0, daily_target=100,
        max_per_company_per_day=3, risk_bias=RiskBias.LEANS_AUTO,
        profile=StrategyProfile.AGGRESSIVE,
        concurrency=3, session_rotation_min=30.0,
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
            concurrency=pacing.concurrency,
            session_rotation_min=pacing.session_rotation_min,
        )
    return PROFILE_PRESETS[profile]


# --------------------------------------------------------------- session rotation

class SessionRotationPolicy:
    """Time-box the apply session on a single source, then rotate (spec §8a).

    Anti-detection intuition: a long uninterrupted run hammering one ATS is a stronger
    fingerprint than the same volume spread across sources. ``session_rotation_min`` caps
    how long the worker keeps applying to one source before it should move on; the worker
    consults this at the top of its per-job loop and, when the budget elapses, *softly*
    defers the remaining same-source jobs (they stay in QUEUED_APPLY for the next cycle) —
    the same shape as the soft daily-target break, never a hard error.

    Pure and clock-injectable (``now`` defaults to :func:`time.monotonic`) so tests drive
    it with a deterministic clock instead of real sleeps. ``rotation_min <= 0`` disables it
    entirely (``enabled`` is ``False``), which is the Balanced/v3.0 default.

    Usage::

        rot = SessionRotationPolicy(pacing.session_rotation_min)
        for job in queued:
            rot.on_source(job.source)      # (re)starts the timer when the source changes
            if rot.should_rotate():        # budget on the current source elapsed?
                defer_the_rest(); break
            ...apply...
    """

    def __init__(self, rotation_min: float, *, now: Callable[[], float] | None = None):
        self._budget_s = max(0.0, float(rotation_min)) * 60.0
        self._now = now or time.monotonic
        self._current_source: str | None = None
        self._started: float | None = None

    @property
    def enabled(self) -> bool:
        """``True`` only when a positive rotation budget is configured."""
        return self._budget_s > 0.0

    def on_source(self, source: str) -> None:
        """Note the source about to be processed. Starts (or restarts) the per-source
        timer when the source *changes*; processing more jobs of the SAME source does not
        reset it (otherwise the budget could never elapse). No-op when disabled."""
        if not self.enabled:
            return
        if source != self._current_source:
            self._current_source = source
            self._started = self._now()

    def should_rotate(self) -> bool:
        """``True`` once the elapsed time on the current source meets/exceeds the budget.
        Always ``False`` when disabled or before the first :meth:`on_source`."""
        if not self.enabled or self._started is None:
            return False
        return (self._now() - self._started) >= self._budget_s
