"""Onboarding state + persistence helpers (spec §11a).

The first-run wizard walks: profile → fact bank (work history + skills +
work-authorization) → targeting → telemetry opt-in → web prefs (F6 / idle).
Power users can skip to the dashboard at any step (spec §11a "guided but
skippable").

The wizard is a thin layer over existing files — there is **no separate
"onboarding state" table**. The wizard reads/writes the same artifacts the
rest of v3 reads from:

* ``data/profile/master.json``    — fact bank (spec §6b)
* ``data/user_config.json``       — settings (targeting, telemetry, web)

That way "did the user finish onboarding?" reduces to "do the fact-bank
prerequisites exist?" — the same question the CLI's ``serve_cmd`` already
asks before starting the scheduler, and the dashboard banner uses to
prompt for onboarding. No drift between "wizard says done" and "scheduler
won't start."

**Why step-wise endpoints (not one big PUT):** the user can close the tab
mid-step and reopen later — each step persists immediately so we don't
lose work. Each endpoint is idempotent; reposting the same step just
overwrites. The wizard tracks "which step am I on" client-side because
that's UI state, not domain state.

**Why we DON'T LLM-parse a pasted résumé in v3.0:** the spec lists "upload
résumé → review the extracted fact bank" as the ideal flow, but the
extraction step is research-heavy (which model, prompt-version pinning,
how to merge variants — see spec §6b "Fact-bank merge conflicts"). v3.0
collects the structured fields manually; v3.1's "upload + LLM extract +
review" lands once the prompt + variant-merge has its own eval harness.
Pasting raw résumé text into the work-history textarea is supported as a
copy-paste convenience — the user is still in control of every field.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from auto_applier.resume.factbank import (
    Contact,
    EducationEntry,
    FactBank,
    WorkEntry,
)

__all__ = [
    "OnboardingStatus",
    "fact_bank_path",
    "load_fact_bank",
    "load_user_config",
    "onboarding_status",
    "save_fact_bank",
    "save_user_config",
]


def fact_bank_path(data_dir: Path) -> Path:
    """The canonical location of master.json. ``serve_cmd`` and the wizard
    both read/write through this helper so a future location change ripples
    in one place."""
    return data_dir / "profile" / "master.json"


# --------------------------------------------------------------- persistence


def load_fact_bank(data_dir: Path) -> FactBank:
    """Read ``master.json`` or return an empty :class:`FactBank` if it's
    not there yet (wizard's blank-state default). Always returns a real
    FactBank so callers don't have to special-case the empty path."""
    p = fact_bank_path(data_dir)
    if not p.exists():
        return FactBank()
    return FactBank.load(p)


def save_fact_bank(data_dir: Path, bank: FactBank) -> None:
    """Persist the fact bank as JSON, creating the parent dir if needed.

    Atomically: write to a sibling tmp file then os.replace so a crash mid-
    write doesn't leave a half-written master.json that breaks the next
    load. The fact bank is a load-bearing artifact — the apply path reads
    every field — so the durability story matters even though the file is
    small."""
    p = fact_bank_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _fact_bank_to_dict(bank)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_user_config(data_dir: Path) -> dict:
    """Read ``user_config.json`` as a plain dict (or return ``{}`` if it
    doesn't exist yet). Returning a dict (vs. a Settings object) is
    deliberate — the wizard merges PARTIAL updates into the file without
    re-validating the whole settings tree on every save (which would
    reject a half-filled wizard state)."""
    p = data_dir / "user_config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Don't blow up the wizard on a hand-edited corrupt file — back it
        # up out of the way and start fresh from {} so the user can
        # re-onboard. The original is preserved at user_config.json.broken
        # for forensics.
        p.replace(p.with_suffix(".json.broken"))
        return {}


def save_user_config(data_dir: Path, config: dict) -> None:
    """Persist ``user_config.json`` atomically. The wizard calls this after
    merging its partial update into the dict from :func:`load_user_config`
    — the wizard never overwrites the whole file with just the step's
    keys."""
    p = data_dir / "user_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------- status


class OnboardingStatus:
    """Snapshot of which onboarding steps are complete.

    Drives:
      * The dashboard banner ("finish onboarding") when ``not is_complete``.
      * The wizard's "you can skip" copy when at least the contact +
        work-auth are filled (the absolute minimum the apply worker needs
        to fill standard fields).
      * The CLI's ``serve_cmd`` scheduler-ready gate (already keys off the
        fact-bank file existing; this exposes the same answer via the
        web API so the dashboard doesn't have to call into the CLI).
    """

    def __init__(self, *, bank: FactBank, has_resume: bool, config: dict):
        self.bank = bank
        self.has_resume = has_resume
        self.config = config

    @property
    def has_contact(self) -> bool:
        c = self.bank.contact
        return bool(c.name and c.email)

    @property
    def has_work_history(self) -> bool:
        return len(self.bank.work_history) > 0

    @property
    def has_skills(self) -> bool:
        return len(self.bank.skills) > 0

    @property
    def has_work_auth(self) -> bool:
        # work_authorization can legitimately be the empty string for a
        # user who deferred answering; ``has_work_auth`` is True iff the
        # field was explicitly set (any non-empty string) OR sponsorship
        # was explicitly set (bool, not None). NO silent default
        # (spec §6b — "captured explicitly in onboarding, no silent
        # default").
        return (
            bool(self.bank.work_authorization)
            or self.bank.requires_sponsorship is not None
        )

    @property
    def has_targeting(self) -> bool:
        t = self.config.get("targeting") or {}
        return bool(t.get("titles") or t.get("locations"))

    @property
    def has_telemetry_decision(self) -> bool:
        # Telemetry is opt-in — having the key present at all means the
        # user made an explicit decision (even if they declined). The
        # wizard writes it as ``{enabled: bool}`` so the absence of the
        # key in user_config means "the user hasn't been asked yet".
        return "telemetry" in self.config

    @property
    def is_complete(self) -> bool:
        """The minimum for the scheduler to start cleanly. Web prefs
        (hotkey/idle) are NOT in this gate — they have sensible defaults
        and the wizard collects them as a refinement step."""
        return (
            self.has_contact
            and self.has_work_history
            and self.has_skills
            and self.has_work_auth
            and self.has_targeting
            and self.has_telemetry_decision
        )

    def to_dict(self) -> dict:
        return {
            "has_contact": self.has_contact,
            "has_work_history": self.has_work_history,
            "has_skills": self.has_skills,
            "has_work_auth": self.has_work_auth,
            "has_targeting": self.has_targeting,
            "has_telemetry_decision": self.has_telemetry_decision,
            "has_resume": self.has_resume,
            "is_complete": self.is_complete,
            # The wizard renders the current values as defaults so the
            # user sees what's already saved when they re-open the
            # tab. Echo the dataclasses as plain dicts.
            "contact": {
                "name": self.bank.contact.name,
                "email": self.bank.contact.email,
                "phone": self.bank.contact.phone,
                "location": self.bank.contact.location,
                "links": dict(self.bank.contact.links),
            },
            "work_history": [
                {
                    "company": w.company,
                    "title": w.title,
                    "start": w.start,
                    "end": w.end,
                    "bullets": list(w.bullets),
                }
                for w in self.bank.work_history
            ],
            "education": [
                {
                    "institution": e.institution,
                    "degree": e.degree,
                    "field_of_study": e.field_of_study,
                    "start": e.start,
                    "end": e.end,
                }
                for e in self.bank.education
            ],
            "skills": list(self.bank.skills),
            "certifications": list(self.bank.certifications),
            "work_authorization": self.bank.work_authorization,
            "requires_sponsorship": self.bank.requires_sponsorship,
            "primary_nationality": self.bank.primary_nationality,
            "notice_period": self.bank.notice_period,
            "languages": list(self.bank.languages),
            "availability": self.bank.availability,
            "eeo": dict(self.bank.eeo),
            "targeting": self.config.get("targeting") or {},
            "telemetry": self.config.get("telemetry") or {},
            "web": self.config.get("web") or {},
            # inbox is non-secret config only (host/port/user/enabled). The app
            # password lives in .env and is NEVER echoed back here.
            "inbox": self.config.get("inbox") or {},
        }


def onboarding_status(data_dir: Path) -> OnboardingStatus:
    """Build the status snapshot. Cheap: just two file reads."""
    bank = load_fact_bank(data_dir)
    config = load_user_config(data_dir)
    # "Has the user uploaded a résumé file?" — separate from the fact
    # bank because v3.0's wizard doesn't auto-extract from uploads. We
    # keep the check so a future upload-and-extract flow has a
    # status field to surface.
    resume_path = (data_dir / "artifacts" / "resume.pdf")
    return OnboardingStatus(
        bank=bank, has_resume=resume_path.exists(), config=config,
    )


# --------------------------------------------------------------- helpers

def _fact_bank_to_dict(bank: FactBank) -> dict:
    """Mirror of :meth:`FactBank.from_dict` for the write path. Keeping it
    here (not on the dataclass) avoids pulling json + Path imports into
    the resume module just for the wizard's needs."""
    return {
        "contact": {
            "name": bank.contact.name,
            "email": bank.contact.email,
            "phone": bank.contact.phone,
            "location": bank.contact.location,
            "links": dict(bank.contact.links),
        },
        "work_history": [
            {
                "company": w.company,
                "title": w.title,
                "start": w.start,
                "end": w.end,
                "bullets": list(w.bullets),
            }
            for w in bank.work_history
        ],
        "education": [
            {
                "institution": e.institution,
                "degree": e.degree,
                "field_of_study": e.field_of_study,
                "start": e.start,
                "end": e.end,
            }
            for e in bank.education
        ],
        "skills": list(bank.skills),
        "certifications": list(bank.certifications),
        "allowed_metrics": list(bank.allowed_metrics),
        "work_authorization": bank.work_authorization,
        "requires_sponsorship": bank.requires_sponsorship,
        "primary_nationality": bank.primary_nationality,
        "notice_period": bank.notice_period,
        "languages": list(bank.languages),
        "availability": bank.availability,
        "eeo": dict(bank.eeo),
        # Was accepted on read (from_dict) but never written back — round-trip the relocation
        # preferences too so they survive a save.
        "relocation": {k: list(v) for k, v in (bank.relocation or {}).items()},
    }


def merge_contact(bank: FactBank, payload: dict) -> FactBank:
    """Apply a contact-step payload into the fact bank. Empty strings
    clear the existing values — that's deliberate, the user might have
    typed a wrong email and need to blank it. Skipping the merge for
    missing keys preserves whatever's there."""
    bank.contact = Contact(
        name=payload.get("name", bank.contact.name),
        email=payload.get("email", bank.contact.email),
        phone=payload.get("phone", bank.contact.phone),
        location=payload.get("location", bank.contact.location),
        links=payload.get("links", bank.contact.links) or {},
    )
    return bank


def merge_work_history(bank: FactBank, payload: list[dict]) -> FactBank:
    """Replace the work-history list wholesale. The wizard always sends
    the full list (it's render-on-load + edit-on-page) so partial-merge
    semantics would be confusing — the user can see exactly what they're
    about to save."""
    bank.work_history = [
        WorkEntry(
            company=w.get("company", ""),
            title=w.get("title", ""),
            start=w.get("start", ""),
            end=w.get("end", ""),
            bullets=list(w.get("bullets", []) or []),
        )
        for w in payload
    ]
    return bank


def merge_education(bank: FactBank, payload: list[dict]) -> FactBank:
    """Replace education list wholesale — same wholesale semantics as
    work history."""
    bank.education = [
        EducationEntry(
            institution=e.get("institution", ""),
            degree=e.get("degree", ""),
            field_of_study=e.get("field_of_study", ""),
            start=e.get("start", ""),
            end=e.get("end", ""),
        )
        for e in payload
    ]
    return bank


def merge_skills(bank: FactBank, skills: list[str]) -> FactBank:
    """Skills are a flat list — replace wholesale, drop empties + dedupe
    case-insensitively. The fabrication guard's keyword check runs over
    this list, so a noisy skill list with case variants would generate
    false positives later."""
    seen_keys: set[str] = set()
    cleaned: list[str] = []
    for s in skills:
        s = (s or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cleaned.append(s)
    bank.skills = cleaned
    return bank


def merge_work_auth(bank: FactBank, payload: dict) -> FactBank:
    """Work-authorization step. ``work_authorization`` is a string the
    user types; ``requires_sponsorship`` is a tri-state (True / False /
    None) so the wizard can distinguish "no, doesn't need sponsorship"
    from "user skipped the question." We DELIBERATELY accept ``None`` so
    the spec's "no silent default" invariant holds — the apply path's
    answer resolver treats a None as "bail to REVIEW on the sponsorship
    question" rather than auto-answering No.
    """
    if "work_authorization" in payload:
        bank.work_authorization = payload["work_authorization"] or ""
    if "requires_sponsorship" in payload:
        raw = payload["requires_sponsorship"]
        bank.requires_sponsorship = (
            None if raw is None else bool(raw)
        )
    return bank


def merge_extras(bank: FactBank, payload: dict) -> FactBank:
    """Optional onboarding extras so the answer resolver can fill common screener fields instead
    of bailing them to REVIEW. All fields are optional — an empty value clears it (the resolver
    then bails that field to assisted, never guessing). Gender is a voluntary EEO self-ID stored
    in the free-form ``eeo`` dict; left blank it stays "prefer not to answer" (honesty invariant)."""
    if "primary_nationality" in payload:
        bank.primary_nationality = (payload.get("primary_nationality") or "").strip()
    if "notice_period" in payload:
        bank.notice_period = (payload.get("notice_period") or "").strip()
    if "availability" in payload:
        bank.availability = (payload.get("availability") or "").strip()
    if "languages" in payload:
        # Accept a list (wizard) or a comma/newline-separated string (CLI/paste); trim + drop
        # empties, dedupe case-insensitively. Blank ⇒ the resolver re-applies the English default.
        raw = payload.get("languages")
        if isinstance(raw, str):
            raw = re.split(r"[,\n;]+", raw)
        seen: set[str] = set()
        langs: list[str] = []
        for item in raw or []:
            s = str(item).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                langs.append(s)
        bank.languages = langs
    if "gender" in payload:
        gender = (payload.get("gender") or "").strip()
        if gender:
            bank.eeo = {**bank.eeo, "gender": gender}
        else:
            bank.eeo = {k: v for k, v in bank.eeo.items() if k != "gender"}
    return bank
