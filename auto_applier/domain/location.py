"""Location-fit classifier for the digest view (read-side, deterministic).

The LLM scorer rates JD-vs-profile *fit*; it deliberately does NOT encode a user's
geographic preference (a great-fit job in a city you'd never move to still scores high).
This module is the separate, deterministic layer that ranks a posting's *location* against
a user's actual preferences, so the digest can float the right places up and sink the rest.

Tuned here for the canonical case (the primary user): remote-first, with a Horizon-2 goal of
relocating to specific EU countries. Priority ladder (0 = best fit .. 4 = worst):

  0  remote + a target-EU country     → Horizon-1 and Horizon-2 in one posting (the jackpot)
  1  remote + US / Americas / global  → the solid remote-now baseline
  2  on-site/hybrid in a target-EU    → relocation via the role itself
  3  remote + some other country      → remote but not a target geography
  4  on-site + non-target             → far-flung on-site (Bangalore/Seoul/etc.) — sinks

The target set lives in ``TARGET_EU`` — edit it to retarget. Keyword matching is intentionally
simple + substring-based; location strings in ATS feeds are short and messy ("Remote - Netherlands",
"United States (remote)", "Bengaluru, India"), so a small curated keyword set beats a parser.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LocationFit", "classify_location", "FILTER_MODES", "passes_filter"]

# Horizon-2 target countries + their main tech hubs (lowercased substrings).
TARGET_EU = {
    "netherlands", "amsterdam", "holland", "the hague", "rotterdam", "eindhoven", "utrecht",
    "germany", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne",
    "ireland", "dublin", "cork",
    "france", "paris", "lyon",
    "denmark", "copenhagen", "københavn",
}

# Remote in the US / Americas / unspecified-global — the solid current-baseline tier.
US_GLOBAL = {
    "united states", "u.s.", "u.s", "usa", "us-remote", "americas", "north america",
    "remote, us", "remote - us", "remote (us", "(us)", "us)",
}

# Named non-target, non-US places — used to distinguish "remote-elsewhere" (tier 3)
# from "remote-US/global" (tier 1). Not exhaustive; anything unmatched + remote → tier 1.
NON_TARGET_HINTS = {
    "united kingdom", "england", "london", "scotland", "manchester",
    "canada", "toronto", "vancouver", "ontario",
    "spain", "madrid", "barcelona", "portugal", "lisbon", "porto",
    "poland", "warsaw", "kraków", "krakow", "romania", "bucharest",
    "india", "bengaluru", "bangalore", "gurgaon", "gurugram", "hyderabad", "pune", "mumbai",
    "australia", "sydney", "melbourne", "new zealand",
    "singapore", "japan", "tokyo", "south korea", "seoul", "korea",
    "brazil", "são paulo", "sao paulo", "mexico", "argentina",
    "philippines", "manila", "israel", "tel aviv",
}

_REMOTE = ("remote", "work from home", "wfh", "work-from-anywhere", "anywhere", "distributed")


@dataclass(frozen=True)
class LocationFit:
    """A posting location's fit against the user's preferences. ``priority`` 0 = best."""

    priority: int
    label: str


def _has(text: str, keywords) -> bool:
    return any(k in text for k in keywords)


def classify_location(loc: str | None) -> LocationFit:
    """Map a raw ATS location string to a :class:`LocationFit` (see module docstring)."""
    s = (loc or "").lower().strip()
    if not s:
        # Blank/unknown: optimistic — many remote posts omit a location. Don't auto-sink.
        return LocationFit(1, "remote • unspecified")

    remote = _has(s, _REMOTE)
    target = _has(s, TARGET_EU)

    if target and remote:
        return LocationFit(0, "remote • target EU")
    if target and not remote:
        return LocationFit(2, "on-site • target EU (relocate)")
    if remote:
        if _has(s, US_GLOBAL):
            return LocationFit(1, "remote • US/Americas")
        if _has(s, NON_TARGET_HINTS):
            return LocationFit(3, "remote • other country")
        return LocationFit(1, "remote • global/unspecified")
    return LocationFit(4, "on-site • other")


# Named filter modes for the digest --location option. Each maps a LocationFit -> keep?
FILTER_MODES = {
    "all":     lambda f: True,                       # show everything (still labeled)
    "remote":  lambda f: f.priority in (0, 1, 3),    # any remote
    "targets": lambda f: f.priority in (0, 1, 2),    # remote-US/global + target-EU (remote or relocate)
    "eu":      lambda f: f.priority in (0, 2),        # target-EU only (remote or relocate)
}


def passes_filter(fit: LocationFit, mode: str) -> bool:
    """True if ``fit`` is kept under the named filter ``mode`` (unknown mode → keep)."""
    return FILTER_MODES.get(mode, FILTER_MODES["all"])(fit)
