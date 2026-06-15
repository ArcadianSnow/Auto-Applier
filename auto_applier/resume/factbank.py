"""Master fact bank — the single source of truth for résumé generation (spec §6b).

Structured deliberately: the fabrication guard's deterministic power scales directly with
how well the bank enumerates companies, date spans, skills, and *allowed metrics* (the
impact numbers the user actually owns). Generation may select/omit/reorder/rephrase from
this bank but may NEVER introduce a company/title/date/credential/skill/number not in it
(the load-bearing fabrication invariant).

The bank is seeded from one or more uploaded résumés (+ optional profile export), merged,
and user-reviewed in onboarding (Phase 4). Phase 1 loads it from a JSON file so the slice
can run end-to-end. Region-neutral: work-auth/sponsorship are explicit, never defaulted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkEntry:
    company: str
    title: str
    start: str            # "2019-03" | "2019" | ""
    end: str              # "2022-06" | "Present" | ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class EducationEntry:
    institution: str
    degree: str           # e.g. "B.S. Computer Science"
    field_of_study: str = ""
    start: str = ""
    end: str = ""


@dataclass
class Contact:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict[str, str] = field(default_factory=dict)  # {"LinkedIn": url, ...}


@dataclass
class FactBank:
    contact: Contact = field(default_factory=Contact)
    work_history: list[WorkEntry] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    #: The impact numbers the user actually owns ("managed $2M budget", "team of 10").
    #: The guard's number check is an allow-list against this — invented/inflated → fail.
    allowed_metrics: list[str] = field(default_factory=list)
    #: Explicitly captured in onboarding — NO silent default (corrects the v2 US-yes bug).
    work_authorization: str = ""      # e.g. "US citizen" | "H-1B" | ""
    requires_sponsorship: bool | None = None
    #: Voluntary EEO self-ID; blank ⇒ "prefer not to answer". Never mirrored to telemetry.
    eeo: dict[str, str] = field(default_factory=dict)
    #: Relocation preferences for "willing to relocate to <country>?" screeners. Keys
    #: "willing"/"unwilling" hold country names; the residence/authorized country is always
    #: "Yes", an unwilling one is "No", anything else bails to the human (never guessed).
    relocation: dict[str, list] = field(default_factory=dict)

    # -- convenience accessors used by the guard ---------------------------
    def companies(self) -> list[str]:
        return [w.company for w in self.work_history if w.company]

    def titles(self) -> list[str]:
        return [w.title for w in self.work_history if w.title]

    def degrees(self) -> list[str]:
        return [e.degree for e in self.education if e.degree]

    def institutions(self) -> list[str]:
        return [e.institution for e in self.education if e.institution]

    def entry_for_company(self, company: str) -> WorkEntry | None:
        for w in self.work_history:
            if w.company == company:
                return w
        return None

    # -- persistence -------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "FactBank":
        return cls(
            contact=Contact(**data.get("contact", {})),
            work_history=[WorkEntry(**w) for w in data.get("work_history", [])],
            education=[EducationEntry(**e) for e in data.get("education", [])],
            skills=list(data.get("skills", [])),
            certifications=list(data.get("certifications", [])),
            allowed_metrics=list(data.get("allowed_metrics", [])),
            work_authorization=data.get("work_authorization", ""),
            requires_sponsorship=data.get("requires_sponsorship"),
            eeo=dict(data.get("eeo", {})),
            relocation=dict(data.get("relocation", {})),
        )

    @classmethod
    def load(cls, path: Path | str) -> "FactBank":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
