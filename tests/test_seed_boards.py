"""Board seeder (``av3 seed-boards``) — contract tests.

Mirrors ``test_discovery.py``: we fake the *source adapters* (deterministic, keyed by slug)
so these stay focused on the seeder's contract and never hit the network:

  * a live board with a posting matching the title filter is KEPT;
  * a live board with NO matching posting is dropped (``live_irrelevant``) under the default
    relevant-only mode, but KEPT under ``--any-live``;
  * a live board with zero open postings is dropped (``live_empty``);
  * a dead/invalid slug (discover raises) is dropped (``dead``) and the sweep continues;
  * slugs already in ``targeting.*_boards`` are never re-probed;
  * the probe cache skips known-dead slugs (and is updated in place);
  * kept slugs merge into ``targeting.*_boards`` (dedup, existing kept);
  * injected (test-owned) sources are not closed.

Plus one real-bundle sanity check that the packaged ``ats_companies.csv`` loads.
"""

from __future__ import annotations

import random

from auto_applier.config.settings import Settings, TargetingConfig
from auto_applier.pipeline.seed_worker import BoardSeeder, merge_boards
from auto_applier.sources.ats_directory import DirectoryEntry, load_ats_directory


# --------------------------------------------------------------- fakes

class _Listing:
    """Stand-in adapter listing — the seeder only reads ``.title``."""

    def __init__(self, title: str):
        self.title = title


class _StubSource:
    """Deterministic source stub. ``table`` maps slug -> "raise" | list[title].
    Missing slug -> [] (live but empty)."""

    def __init__(self, table: dict):
        self.table = table
        self.calls: list[str] = []
        self.closed = False

    def discover(self, slug: str) -> list:
        self.calls.append(slug)
        v = self.table.get(slug, [])
        if v == "raise":
            raise RuntimeError(f"dead slug {slug!r}")
        return [_Listing(t) for t in v]

    def close(self) -> None:
        self.closed = True


def _settings(base: Settings, **targeting) -> Settings:
    """Copy the fixture settings with a controlled TargetingConfig (robust to a frozen
    model). Defaults: empty boards + a 'data analyst' title filter."""
    tc = TargetingConfig(
        titles=targeting.pop("titles", ["data analyst"]),
        greenhouse_boards=targeting.pop("greenhouse_boards", []),
        lever_boards=targeting.pop("lever_boards", []),
        ashby_boards=targeting.pop("ashby_boards", []),
    )
    return base.model_copy(update={"targeting": tc})


def _dir(*rows: tuple[str, str, str]) -> list[DirectoryEntry]:
    return [DirectoryEntry(ats, name, slug) for ats, name, slug in rows]


def _seeder(settings, *, directory, sources, **kw) -> BoardSeeder:
    return BoardSeeder(
        settings=settings, directory=directory, sources=sources,
        rng=random.Random(0), **kw,
    )


# --------------------------------------------------------------- real bundle

def test_bundled_directory_loads():
    entries = load_ats_directory()
    assert len(entries) > 5000                      # ~9.9k companies shipped
    assert {e.ats for e in entries} <= {"greenhouse", "lever", "ashby"}
    assert all(e.slug for e in entries)             # no empty slugs
    # the ats filter works
    gh = load_ats_directory(ats={"greenhouse"})
    assert gh and all(e.ats == "greenhouse" for e in gh)


def test_name_contains_filter():
    entries = load_ats_directory(name_contains="data")
    assert entries  # at least some company names contain "data"
    assert all("data" in e.name.lower() for e in entries)


# --------------------------------------------------------------- probe outcomes

def test_live_relevant_board_kept(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"acme": ["Senior Data Analyst, Remote"]})
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh})

    summary = seeder.run()

    assert summary.probed == 1
    assert summary.kept == 1
    assert summary.added == {"greenhouse": ["acme"]}
    assert gh.calls == ["acme"]


def test_live_irrelevant_dropped_under_relevant_only(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"acme": ["Frontend Engineer"]})  # no "data analyst" title
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh})

    summary = seeder.run()

    assert summary.live_irrelevant == 1
    assert summary.kept == 0
    assert summary.added == {}


def test_any_live_keeps_irrelevant(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"acme": ["Frontend Engineer"]})
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh}, relevant_only=False)

    summary = seeder.run()

    assert summary.kept == 1
    assert summary.added == {"greenhouse": ["acme"]}


def test_live_but_empty_board_not_kept(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"acme": []})  # valid board, zero postings
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh})

    summary = seeder.run()

    assert summary.live_empty == 1
    assert summary.kept == 0


def test_dead_slug_isolated(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"good": ["Data Analyst"], "bad": "raise"})
    seeder = _seeder(
        s,
        directory=_dir(("greenhouse", "Good", "good"), ("greenhouse", "Bad", "bad")),
        sources={"greenhouse": gh},
    )

    summary = seeder.run()

    assert summary.dead == 1
    assert summary.kept == 1
    assert summary.added == {"greenhouse": ["good"]}


def test_progress_callback_fires_per_candidate(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"a": ["Data Analyst"], "b": ["Frontend Engineer"], "c": "raise"})
    seen = []
    seeder = _seeder(
        s,
        directory=_dir(("greenhouse", "A", "a"), ("greenhouse", "B", "b"),
                       ("greenhouse", "C", "c")),
        sources={"greenhouse": gh},
        progress=lambda summ: seen.append((summ.probed, summ.kept)),
    )
    summary = seeder.run()

    assert len(seen) == 3                 # one callback per candidate
    assert seen[-1][0] == 3               # probed reached the full count
    assert summary.kept == seen[-1][1]    # the live kept count matches the final summary


# --------------------------------------------------------------- skip + cache

def test_already_configured_slug_not_reprobed(settings: Settings):
    s = _settings(settings, greenhouse_boards=["acme"])  # already configured
    gh = _StubSource({"acme": ["Data Analyst"], "newco": ["Data Analyst"]})
    seeder = _seeder(
        s,
        directory=_dir(("greenhouse", "Acme", "acme"), ("greenhouse", "NewCo", "newco")),
        sources={"greenhouse": gh},
    )

    summary = seeder.run()

    assert "acme" not in gh.calls          # already configured → never probed
    assert summary.added == {"greenhouse": ["newco"]}


def test_probe_cache_skips_known_dead_and_updates(settings: Settings):
    s = _settings(settings)
    cache = {"greenhouse:olddead": "dead"}
    gh = _StubSource({"live1": ["Data Analyst"], "olddead": "raise"})
    seeder = _seeder(
        s,
        directory=_dir(("greenhouse", "Live", "live1"), ("greenhouse", "Old", "olddead")),
        sources={"greenhouse": gh},
        probe_cache=cache,
    )

    summary = seeder.run()

    assert "olddead" not in gh.calls       # cached-dead → skipped, not re-probed
    assert summary.cached_skip == 1
    assert summary.kept == 1
    assert cache["greenhouse:live1"] == "live"   # cache updated in place


# --------------------------------------------------------------- merge + persistence shape

def test_merged_targeting_dedupes_and_keeps_existing(settings: Settings):
    s = _settings(settings, greenhouse_boards=["existing"])
    gh = _StubSource({"newco": ["Data Analyst"]})
    seeder = _seeder(
        s,
        directory=_dir(("greenhouse", "NewCo", "newco")),
        sources={"greenhouse": gh},
    )
    summary = seeder.run()
    merged = seeder.merged_targeting(summary)

    assert merged["greenhouse_boards"] == ["existing", "newco"]
    assert merged["lever_boards"] == []
    assert merged["ashby_boards"] == []


def test_merge_boards_unit():
    assert merge_boards(["a", "b"], ["b", "c"]) == ["a", "b", "c"]      # dedupe
    assert merge_boards([], ["X", "x"]) == ["X"]                          # case-insensitive
    assert merge_boards(["keep"], []) == ["keep"]                         # existing survives


# --------------------------------------------------------------- source lifecycle

def test_injected_sources_not_closed(settings: Settings):
    s = _settings(settings)
    gh = _StubSource({"acme": ["Data Analyst"]})
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh})

    seeder.run()

    assert gh.closed is False   # the seeder only closes sources it built itself


def test_no_candidates_is_a_note_not_an_error(settings: Settings):
    s = _settings(settings, greenhouse_boards=["acme"])
    gh = _StubSource({"acme": ["Data Analyst"]})
    seeder = _seeder(s, directory=_dir(("greenhouse", "Acme", "acme")),
                     sources={"greenhouse": gh})

    summary = seeder.run()

    assert summary.probed == 0
    assert summary.kept == 0
    assert summary.notes  # explains "all already configured"
