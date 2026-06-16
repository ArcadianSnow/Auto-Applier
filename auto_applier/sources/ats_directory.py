"""Bundled ATS company directory — the offline slug source for ``av3 seed-boards``.

A company→ATS→slug table (~9.9k companies across Greenhouse/Lever/Ashby) shipped as
``data/ats_companies.csv`` so seeding needs **no runtime web egress** — the only network in
seeding is the confirm-probe against the same public read APIs discovery already uses
(``research/ats-discovery-seeding.md``). The ``slug`` column is exactly the board token
``DiscoverWorker`` sweeps.

Provenance: company slugs derived (build-time) from the MIT-licensed dataset
``github.com/kalil0321/ats-scrapers`` (per-ATS CSVs). Dead/changed slugs are harmless — the
seeder confirm-probes every candidate before keeping it, so a stale row just gets dropped.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from importlib import resources

__all__ = ["DirectoryEntry", "load_ats_directory"]


@dataclass(frozen=True)
class DirectoryEntry:
    """One row of the bundled directory."""

    ats: str   # "greenhouse" | "lever" | "ashby"
    name: str  # company display name
    slug: str  # the board token / site / slug (what we probe + seed)


def load_ats_directory(
    *, ats: set[str] | None = None, name_contains: str | None = None,
) -> list[DirectoryEntry]:
    """Load the bundled directory → list of :class:`DirectoryEntry` in dataset order.

    ``ats`` (when given) keeps only those ATSes; ``name_contains`` keeps only companies
    whose display name contains the substring (case-insensitive). Rows with an empty slug
    are skipped.
    """
    raw = (
        resources.files("auto_applier.data")
        .joinpath("ats_companies.csv")
        .read_text(encoding="utf-8")
    )
    kw = name_contains.lower().strip() if name_contains else ""
    out: list[DirectoryEntry] = []
    for row in csv.DictReader(io.StringIO(raw)):
        a = (row.get("ats") or "").strip()
        slug = (row.get("slug") or "").strip()
        name = (row.get("name") or "").strip()
        if not slug:
            continue
        if ats is not None and a not in ats:
            continue
        if kw and kw not in name.lower():
            continue
        out.append(DirectoryEntry(a, name, slug))
    return out
