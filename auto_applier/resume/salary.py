"""Salary intelligence (spec §8d) — Phase 6 / v3.1.

Computes a **recommended ask** from up to three inputs (spec §8d):
  1. the user's configured range (floor + optional ceiling),
  2. the job's **posted range** (if the listing exposed one), and
  3. **market data** (optional, pluggable).

It corrects the two failure modes a naïve "just fill my floor" approach has: a user who
**low-balls** (asks below what the role pays) and one who **overshoots** (asks above the
posted ceiling and gets filtered out). The output is a single number the answer resolver
fills into a salary-expectation field, plus the reasoning for the dashboard.

**Local-first market data (project invariant).** The product's hard rule is *zero network
egress except the opt-in telemetry mirror* (CLAUDE.md / spec §2). A live wage API would
break that for the core pipeline. So the market source is a **pluggable Protocol that
defaults to OFF** (`NoMarketData` → returns `None`); the recommendation math is 100% local
and works with posted-range + user-range alone. The spec's "default free BLS OES" lands as
an **opt-in adapter** (`BlsOesMarketData`, see below) the user explicitly wires in
``user_config.json`` — it is never called unless turned on. Glassdoor / Levels.fyi stay out
(no free feed, ToS-risky), per §8d.

**Comp filter (the scoring gate, §8d).** :func:`is_below_floor` answers "is this job's
posted range below the user's floor?" — the score/decision path uses it to SKIP a job before
wasting an application. When no range is posted it returns ``False`` (can't filter → proceed,
spec §8d).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "SalaryRange",
    "SalaryRecommendation",
    "MarketDataSource",
    "NoMarketData",
    "build_market_source",
    "parse_posted_range",
    "recommend_ask",
    "is_below_floor",
    "format_ask",
]


@dataclass(frozen=True)
class SalaryRange:
    """An annual-salary range in whole USD. ``high`` may equal ``low`` for a point value."""

    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low < 0 or self.high < 0:
            raise ValueError("salary range values must be non-negative")
        if self.high < self.low:
            raise ValueError(f"range high ({self.high}) < low ({self.low})")

    @property
    def midpoint(self) -> int:
        return (self.low + self.high) // 2


@dataclass(frozen=True)
class SalaryRecommendation:
    """The computed ask + a short human-readable rationale for the dashboard/notes.

    ``amount`` is the single number to fill into a salary field. ``basis`` names which
    input drove it (``posted`` / ``market`` / ``user``) so the §8e feedback loop and the
    dashboard can explain *why* — and so a future audit can tell a market-informed ask
    from a bare user-floor fallback.
    """

    amount: int
    basis: str           # "posted" | "market" | "user"
    rationale: str


class MarketDataSource(Protocol):
    """Pluggable market-wage lookup. Returns ``None`` when it has no data for the
    (title, location) pair — the recommender then falls back to posted/user inputs.

    Implementations MUST be side-effect-light from the caller's view; a *live* source
    (e.g. BLS OES) is opt-in and the user accepts its network egress by wiring it.
    """

    def estimate(self, *, title: str, location: str) -> SalaryRange | None: ...


class NoMarketData:
    """The default market source: always ``None`` (no network, no data). Keeps the core
    pipeline local-first; the recommender works on posted-range + user-range alone."""

    def estimate(self, *, title: str, location: str) -> SalaryRange | None:  # noqa: D102
        return None


def build_market_source(name: str) -> MarketDataSource:
    """Construct the configured market source (spec §8d, ``salary.market_source``).

    Default ``"none"`` → :class:`NoMarketData` (zero egress, the project invariant). The
    spec's BLS OES adapter is **not built here yet** — when it lands it registers as
    ``"bls_oes"`` and the user opts into its network egress by naming it in config. Any
    unrecognized name falls back to :class:`NoMarketData` (fail-safe: never silently start
    making network calls). Recorded in `research/phase6-v3.1.md` §(3/M).
    """
    key = (name or "none").strip().lower()
    # Registry kept tiny on purpose; BLS OES adapter is a future opt-in entry.
    if key in ("", "none", "off"):
        return NoMarketData()
    # Unknown / not-yet-implemented source → safe local default.
    return NoMarketData()


# --------------------------------------------------------------- parsing

#: Matches the common posted-comp shapes: "$120,000 - $150,000", "120k-150k",
#: "$120K to $150K", "USD 120000–150000". Captures two magnitude groups + optional k.
_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
_RANGE_RE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM)
_SINGLE_RE = re.compile(_NUM)


def _norm(magnitude: str, k: str | None) -> int:
    """'120,000'→120000, '120'+'k'→120000, '120.5'+'k'→120500."""
    val = float(magnitude.replace(",", ""))
    if k:
        val *= 1000
    return int(round(val))


def parse_posted_range(text: str | None) -> SalaryRange | None:
    """Best-effort parse of a listing's posted-comp string into a :class:`SalaryRange`.

    Returns ``None`` when nothing salary-shaped is found (the listing didn't post comp, or
    posted only "competitive"). Tolerant of ``$``, commas, ``k``/``K``, and ``-``/``–``/``to``
    separators. A lone number becomes a point range (low == high). Sub-1000 magnitudes
    with no ``k`` are treated as hourly-or-noise and rejected (a salary field wants annual).
    """
    if not text:
        return None
    m = _RANGE_RE.search(text)
    if m:
        low = _norm(m.group(1), m.group(2))
        high = _norm(m.group(3), m.group(4))
        if high < low:
            low, high = high, low
        if low < 1000:  # "1 - 2 years", "$15-20/hr" style noise — not an annual range
            return None
        return SalaryRange(low, high)
    m = _SINGLE_RE.search(text)
    if m:
        val = _norm(m.group(1), m.group(2))
        if val < 1000:
            return None
        return SalaryRange(val, val)
    return None


# --------------------------------------------------------------- recommendation

def recommend_ask(
    *,
    user_floor: int | None,
    user_ceiling: int | None = None,
    posted: SalaryRange | None = None,
    market: SalaryRange | None = None,
) -> SalaryRecommendation | None:
    """Compute the recommended salary ask (spec §8d).

    Strategy, in priority order:

      * **Posted range present** → anchor to it: aim for the upper-middle (75th-percentile
        point of the posted band) so we don't leave money on the table, but never ask above
        the posted ``high`` (which risks an auto-filter) and never below the user's floor.
        Basis = ``posted``.
      * **No posted range, market data present** → anchor to the market midpoint, floored at
        the user's floor. Basis = ``market``.
      * **Neither** → fall back to the user's range: ceiling if set, else floor. Basis =
        ``user``.
      * **No inputs at all** (no floor, no posted, no market) → ``None`` (the resolver then
        leaves the field for REVIEW rather than inventing a number).

    The floor is a hard lower bound on the *ask* in every branch — we never recommend the
    user ask for less than they said they'd accept.
    """
    if posted is not None:
        # Upper-middle of the posted band: low + 3/4 of the span.
        target = posted.low + (posted.high - posted.low) * 3 // 4
        if user_floor is not None:
            target = max(target, user_floor)
        target = min(target, posted.high)  # never overshoot the posted ceiling
        return SalaryRecommendation(
            amount=target,
            basis="posted",
            rationale=(
                f"posted {posted.low:,}-{posted.high:,}; ask {target:,} "
                f"(upper-middle, floored at user {user_floor:,})"
                if user_floor is not None
                else f"posted {posted.low:,}-{posted.high:,}; ask {target:,} (upper-middle)"
            ),
        )

    if market is not None:
        target = market.midpoint
        if user_floor is not None:
            target = max(target, user_floor)
        return SalaryRecommendation(
            amount=target,
            basis="market",
            rationale=f"market {market.low:,}-{market.high:,}; ask {target:,} (midpoint)",
        )

    if user_ceiling is not None:
        return SalaryRecommendation(
            amount=user_ceiling,
            basis="user",
            rationale=f"no posted/market data; ask user ceiling {user_ceiling:,}",
        )
    if user_floor is not None:
        return SalaryRecommendation(
            amount=user_floor,
            basis="user",
            rationale=f"no posted/market data; ask user floor {user_floor:,}",
        )
    return None


def is_below_floor(posted: SalaryRange | None, user_floor: int | None) -> bool:
    """Comp-filter gate (spec §8d): is the posted range below the user's floor?

    True only when BOTH a floor and a posted range exist AND the posted **high** is below
    the floor (the whole band is under what the user will accept). No posted range → can't
    filter → ``False`` (proceed). No floor → no constraint → ``False``.
    """
    if user_floor is None or posted is None:
        return False
    return posted.high < user_floor


def format_ask(rec: SalaryRecommendation | None) -> str:
    """Render a recommendation as the string filled into a salary field (e.g. ``"$140,000"``).
    Empty string for ``None`` so a missing recommendation fills nothing."""
    if rec is None:
        return ""
    return f"${rec.amount:,}"
