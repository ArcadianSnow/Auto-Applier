"""Board seeder — ``av3 seed-boards`` (research/future-directions.md "Direction 1";
research/ats-discovery-seeding.md Phase-2 "open-dataset bulk seeding").

**Why:** a fresh user's discovery sweeps the small fixed ``targeting.*_boards`` starter set,
so they get jobs from a handful of arbitrary companies — irrelevant to their field. This
turns the user's *targeting* into a **verified, relevant** set of board slugs, so discovery
returns jobs in THEIR field. The gate to anyone-but-the-author using the product.

**Mechanic (reuses discovery — no reinvention):** take candidate slugs from the bundled
:mod:`auto_applier.sources.ats_directory`, confirm-probe each via the SAME
``Source.discover(token)`` discovery uses (self-throttled ~1 req/s, public read API, ToS-clean),
and keep a board iff it is **live** AND — when a title filter is set (the default) — currently
has ≥1 posting whose title matches via :func:`title_matches`. Verified slugs merge into
``settings.targeting.*_boards`` (dedup, existing kept).

**Cost/politeness:** the only network is the probe against the public read endpoints discovery
already hits. A probe cache records **dead** slugs so re-runs never re-hammer a 404; live slugs
are re-probed (their open postings + relevance change run-to-run). Candidates are shuffled so a
bounded ``limit`` is a representative sample across the dataset, and re-running grows the list.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from auto_applier.config.settings import Settings
from auto_applier.pipeline.discover_worker import title_matches
from auto_applier.sources import AshbySource, GreenhouseSource, LeverSource
from auto_applier.sources.ats_directory import DirectoryEntry, load_ats_directory

__all__ = ["BoardSeeder", "SeedSummary", "merge_boards"]

_SOURCE_CLASSES = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
}
# ats name -> the TargetingConfig list attribute it seeds.
_TARGETING_KEY = {
    "greenhouse": "greenhouse_boards",
    "lever": "lever_boards",
    "ashby": "ashby_boards",
}


def merge_boards(existing: list[str], kept: list[str]) -> list[str]:
    """Merge ``kept`` slugs into ``existing``, preserving order, de-duping case-insensitively
    (GH/Lever tokens are case-insensitive; Ashby preserves case so we keep the first-seen
    casing). Existing entries always survive."""
    seen = {s.lower() for s in existing}
    out = list(existing)
    for s in kept:
        if s.lower() not in seen:
            out.append(s)
            seen.add(s.lower())
    return out


@dataclass
class SeedSummary:
    """One :meth:`BoardSeeder.run` outcome — observable, not side-effect-only."""

    probed: int = 0           # candidates actually confirm-probed this run
    kept: int = 0             # live + (relevant, or any-live mode) → seedable
    dead: int = 0             # discover raised / 404 / bad slug
    live_empty: int = 0       # valid board but zero open postings
    live_irrelevant: int = 0  # has postings but none match the title filter
    cached_skip: int = 0      # skipped: cached-dead from a prior run
    added: dict[str, list[str]] = field(default_factory=dict)  # ats -> new slugs kept
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


class BoardSeeder:
    """Confirm-probe candidate slugs and collect the live, relevant ones.

    Construct, call :meth:`run`. Inject ``sources`` (``{ats: obj}``) and/or ``directory`` in
    tests to avoid real HTTP/the bundled file. ``probe_cache`` (a ``{ats:slug -> "live"|"dead"}``
    dict) is read+updated in place so the CLI can persist it across runs.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        titles: list[str] | None = None,
        relevant_only: bool = True,
        ats: set[str] | None = None,
        name_contains: str | None = None,
        limit: int = 200,
        rng: random.Random | None = None,
        sources: dict[str, object] | None = None,
        directory: list[DirectoryEntry] | None = None,
        probe_cache: dict[str, str] | None = None,
    ):
        self._settings = settings
        self._titles = titles if titles is not None else list(settings.targeting.titles)
        self._relevant_only = relevant_only
        self._ats = ats or set(_SOURCE_CLASSES)
        self._name_contains = name_contains
        self._limit = limit
        self._rng = rng or random.Random()
        self._injected_sources = sources
        self._directory = directory
        self._cache = probe_cache if probe_cache is not None else {}

    # -- public ------------------------------------------------------------

    def candidates(self) -> list[DirectoryEntry]:
        """The directory, filtered to the wanted ATSes/name, with already-configured slugs
        removed, shuffled (so a bounded ``limit`` samples across the dataset, not just the
        alphabetical head), then capped to ``limit``."""
        entries = self._directory
        if entries is None:
            entries = load_ats_directory()
        kw = (self._name_contains or "").lower().strip()
        entries = [
            e for e in entries
            if e.ats in self._ats and (not kw or kw in e.name.lower())
        ]
        configured = self._configured_slugs()
        fresh = [e for e in entries if (e.ats, e.slug.lower()) not in configured]
        self._rng.shuffle(fresh)
        return fresh[: self._limit]

    def run(self) -> SeedSummary:
        summary = SeedSummary()
        t0 = time.perf_counter()
        cands = self.candidates()
        if not cands:
            summary.notes.append(
                "no candidates (dataset empty for the chosen ATS/filter, or all already configured)"
            )
            summary.elapsed_s = time.perf_counter() - t0
            return summary

        sources = self._build_sources()
        kept: dict[str, list[str]] = {}
        try:
            for e in cands:
                src = sources.get(e.ats)
                if src is None:
                    continue
                key = f"{e.ats}:{e.slug}"
                if self._cache.get(key) == "dead":
                    summary.cached_skip += 1
                    continue
                summary.probed += 1
                status, keep = self._probe_one(e, src, summary)
                self._cache[key] = status
                if keep:
                    kept.setdefault(e.ats, []).append(e.slug)
        finally:
            self._close_sources(sources)

        summary.added = kept
        summary.kept = sum(len(v) for v in kept.values())
        summary.elapsed_s = time.perf_counter() - t0
        return summary

    def merged_targeting(self, summary: SeedSummary) -> dict[str, list[str]]:
        """The ``targeting.*_boards`` lists after merging this run's kept slugs (pure — does
        not persist; the CLI writes them to ``user_config.json``)."""
        t = self._settings.targeting
        return {
            key: merge_boards(list(getattr(t, key)), summary.added.get(ats, []))
            for ats, key in _TARGETING_KEY.items()
        }

    # -- internals ---------------------------------------------------------

    def _probe_one(self, e: DirectoryEntry, src, summary: SeedSummary) -> tuple[str, bool]:
        """Probe one slug. Returns ``(liveness, keep)`` where liveness ∈ {"live","dead"} (for
        the cache) and ``keep`` is whether to seed it. Any discover error → dead (not seedable).
        Relevance (title match) does NOT make a board "dead" — it stays "live" so a later run
        with different titles can reconsider it."""
        try:
            listings = src.discover(e.slug)
        except Exception:  # noqa: BLE001 — dead/invalid slug or network blip: not seedable
            summary.dead += 1
            return ("dead", False)
        if not listings:
            summary.live_empty += 1
            return ("live", False)
        if self._relevant_only and self._titles:
            if not any(
                title_matches(getattr(lst, "title", "") or "", self._titles) for lst in listings
            ):
                summary.live_irrelevant += 1
                return ("live", False)
        return ("live", True)

    def _configured_slugs(self) -> set[tuple[str, str]]:
        t = self._settings.targeting
        out: set[tuple[str, str]] = set()
        for ats, key in _TARGETING_KEY.items():
            for s in getattr(t, key):
                out.add((ats, s.lower()))
        return out

    def _build_sources(self) -> dict[str, object]:
        if self._injected_sources is not None:
            return self._injected_sources
        return {ats: _SOURCE_CLASSES[ats]() for ats in self._ats if ats in _SOURCE_CLASSES}

    def _close_sources(self, sources: dict[str, object]) -> None:
        if self._injected_sources is not None:
            return  # test-owned
        for src in sources.values():
            close = getattr(src, "close", None)
            if callable(close):
                close()
