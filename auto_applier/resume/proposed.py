"""The COMPLETE proposed application per job — batched assisted review, Phase 1 (prep-complete).

Design: ``.claude/skills/auto-applier/research/batched-assisted-review.md``.

The batched assisted-review flow surfaces an "In Progress" page where the owner verifies /
corrects / submits each prepared job. That page needs the **complete proposed application** —
every field with its value, source, confidence, and whether it still needs the owner's eyes —
not just the confident subset the bot typed into the live form. This module computes and persists
that set.

The split (owner decision 2, 2026-06-24):

  * the **live form** gets the *confident subset* — deterministic fills + audited-inferred answers,
    exactly as today (this module does NOT change that; submit behavior is untouched);
  * the **page** is *complete* — it ALSO carries a full first DRAFT for every open-ended gap the
    bot left blank in the live form (decision 4: "draft everything; owner verifies"). The owner is
    the submit gate, so an essay draft the owner edits beats a blank the owner writes from scratch.

How "draft everything" stays honest: drafting routes through
:meth:`AnswerResolver.draft_open_ended`, a strict superset of the resolver's own gating — it
refuses to draft a sensitive field, a 'how did you hear?' picker, or any non-essay question. So the
page can never fabricate a value the resolver itself would refuse to fill. Sensitive / how-heard /
unanswerable gaps appear on the page as *needs-your-input* rows carrying the bail reason, never a
manufactured answer.

Persistence is a **per-job JSON artifact** (``artifacts/proposed/<job_id>.json``) — generated
content lives as a file, the same convention as the per-job résumé / cover letter (spec §4). It is
**local-only**, never mirrored to telemetry (same posture as the answer values + EEO data).

Phase 1 is deliberately browser-free + pure: given a job's discovered questions and their (already
computed) confident resolutions, :func:`build_proposed_application` drafts the gaps and assembles
the set. The batch barrier (Phase 2) and the "In Progress" page (Phase 3) build on top of this.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from auto_applier.config.settings import Settings
from auto_applier.domain.models import utcnow_iso
from auto_applier.resume.answer_resolver import AnswerResolver
from auto_applier.sources.browser.apply_base import Applicant, CustomQuestion

logger = logging.getLogger(__name__)

__all__ = [
    "ProposedApplication",
    "ProposedField",
    "build_proposed_application",
    "load_proposed",
    "proposed_path",
    "save_proposed",
]

#: Kinds for the synthesized (non-custom-question) rows, so the page can render them distinctly
#: from a discovered form field. Standard = name/email/phone (identity); document = résumé / cover.
_KIND_STANDARD = "standard"
_KIND_DOCUMENT = "document"


@dataclass
class ProposedField:
    """One row of the complete proposed application — what the "In Progress" page renders per field.

    A field is exactly one of three dispositions, captured by the two booleans:

      * **confident** (``needs_verify=False``): a deterministic fill or an audited-inferred answer
        the bot is confident in — this is the subset that also went onto the live form;
      * **draft** (``is_draft=True`` ⇒ ``needs_verify=True``): an aggressively-drafted essay the
        owner must read + edit before it is used (it is on the page, never on the auto-submit path);
      * **needs input** (``needs_verify=True``, ``is_draft=False``): a gap with no confident answer
        and no safe draft (sensitive / how-heard / unanswered screener) — the owner supplies it.
    """

    key: str            # stable-ish handle: "q:<field_id>" | "applicant:email" | "doc:resume"
    label: str          # the visible question / field label
    value: str          # the proposed value or draft text ("" when truly blank)
    kind: str           # "input" | "textarea" | "select" | "combobox" | "radio" | standard/document
    source: str         # ResolutionSource value, or "applicant" / "document"
    confidence: float
    required: bool
    needs_verify: bool  # the owner must check / supply this before it is used
    is_draft: bool      # an aggressively-drafted essay (always needs_verify)
    options: list[str] = field(default_factory=list)  # choices for a select/radio/combobox
    note: str = ""      # the resolver's note (why it filled / drafted / bailed)

    @classmethod
    def from_resolution(cls, question: CustomQuestion, resolution) -> "ProposedField":
        """Map a discovered question + its (possibly draft-upgraded) :class:`Resolution`."""
        fills = bool(getattr(resolution, "fills", False))
        is_draft = bool(getattr(resolution, "draft", False))
        value = getattr(resolution, "value", None)
        source = getattr(resolution, "source", None)
        return cls(
            key=f"q:{question.field_id}",
            label=question.label,
            value="" if value is None else str(value),
            kind=question.kind,
            source=getattr(source, "value", str(source)),
            confidence=float(getattr(resolution, "confidence", 0.0)),
            required=bool(question.required),
            # Confident fills (deterministic / audited-inferred) are trusted; a draft or any
            # non-filling bail needs the owner's eyes.
            needs_verify=is_draft or not fills,
            is_draft=is_draft,
            options=list(getattr(question, "options", None) or []),
            note=getattr(resolution, "note", "") or "",
        )


@dataclass
class ProposedApplication:
    """The complete proposed application for one job — what the "In Progress" page renders.

    Carries the identity/document context (top-level paths) plus one :class:`ProposedField` per
    standard field and per discovered custom question. Persisted as a per-job JSON artifact so the
    page survives a refresh.
    """

    job_id: str
    fields: list[ProposedField]
    resume_path: str = ""
    cover_letter_path: str = ""
    built_at: str = field(default_factory=utcnow_iso)

    # -- summary (the page header + the apply-outcome line) ----------------

    @property
    def confident_fields(self) -> list[ProposedField]:
        """Fields the bot filled confidently (the live-form subset) — value present, no verify."""
        return [f for f in self.fields if f.value and not f.needs_verify]

    @property
    def draft_fields(self) -> list[ProposedField]:
        return [f for f in self.fields if f.is_draft]

    @property
    def needs_verify_fields(self) -> list[ProposedField]:
        return [f for f in self.fields if f.needs_verify]

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.fields),
            "confident": len(self.confident_fields),
            "drafted": len(self.draft_fields),
            "needs_verify": len(self.needs_verify_fields),
        }

    # -- (de)serialization ------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "resume_path": self.resume_path,
            "cover_letter_path": self.cover_letter_path,
            "built_at": self.built_at,
            "fields": [asdict(f) for f in self.fields],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProposedApplication":
        raw_fields = data.get("fields") or []
        fields_ = [
            ProposedField(
                key=str(d.get("key", "")),
                label=str(d.get("label", "")),
                value=str(d.get("value", "")),
                kind=str(d.get("kind", "")),
                source=str(d.get("source", "")),
                confidence=float(d.get("confidence", 0.0) or 0.0),
                required=bool(d.get("required", False)),
                needs_verify=bool(d.get("needs_verify", False)),
                is_draft=bool(d.get("is_draft", False)),
                options=list(d.get("options") or []),
                note=str(d.get("note", "")),
            )
            for d in raw_fields
            if isinstance(d, dict)
        ]
        return cls(
            job_id=str(data.get("job_id", "")),
            fields=fields_,
            resume_path=str(data.get("resume_path", "")),
            cover_letter_path=str(data.get("cover_letter_path", "")),
            built_at=str(data.get("built_at", "")),
        )


# --------------------------------------------------------------- the builder

def _standard_fields(
    applicant: Applicant, resume_path: str, cover_letter_path: str
) -> list[ProposedField]:
    """The synthesized identity + document rows every application carries.

    Deterministic contact facts (name/email/phone from the fact-bank-derived applicant) and the
    per-job résumé / cover letter the worker resolved. A required identity field or the résumé being
    blank flags ``needs_verify`` (the owner must supply it); the cover letter is supplementary, so a
    missing one is not a verify flag.
    """
    def std(key: str, label: str, value: str, *, required: bool) -> ProposedField:
        return ProposedField(
            key=key, label=label, value=value or "", kind=_KIND_STANDARD,
            source="applicant", confidence=1.0, required=required,
            needs_verify=required and not value, is_draft=False,
        )

    def doc(key: str, label: str, path: str, *, required: bool) -> ProposedField:
        return ProposedField(
            key=key, label=label, value=path or "", kind=_KIND_DOCUMENT,
            source="document", confidence=1.0, required=required,
            needs_verify=required and not path, is_draft=False,
            note="" if path else ("attach a file" if required else "optional — none attached"),
        )

    return [
        std("applicant:name", "Full name", applicant.full_name, required=True),
        std("applicant:email", "Email", applicant.email, required=True),
        std("applicant:phone", "Phone", applicant.phone or "", required=False),
        doc("doc:resume", "Résumé", resume_path, required=True),
        doc("doc:cover_letter", "Cover letter", cover_letter_path, required=False),
    ]


async def build_proposed_application(
    *,
    job_id: str,
    applicant: Applicant,
    resume_path: str,
    cover_letter_path: str,
    questions: list[CustomQuestion],
    resolutions: list,
    resolver: AnswerResolver | None = None,
    built_at: str = "",
) -> ProposedApplication:
    """Assemble the COMPLETE proposed application for ``job_id`` (batched assisted review, Phase 1).

    ``questions`` + ``resolutions`` are the driver's already-computed pair (aligned, in order) — the
    SAME confident subset the live form was filled with, so the page's confident rows match the live
    form exactly (decision 2). For every question whose confident resolution did NOT fill, this
    additionally asks ``resolver`` to draft it via :meth:`AnswerResolver.draft_open_ended` — which
    upgrades an open-ended *essay* gap to a full first draft and leaves everything else (sensitive /
    how-heard / non-essay) as a needs-input row. Pass ``resolver=None`` to skip drafting entirely
    (bails stay bails) — useful for a pure, no-LLM unit test.

    Browser-free + side-effect-free (it does not persist — call :func:`save_proposed`). Never
    raises on a per-question draft failure: the resolver's draft path swallows its own errors and
    returns ``None``, so a flaky LLM just leaves that gap as a needs-input row.
    """
    fields = _standard_fields(applicant, resume_path, cover_letter_path)
    for question, resolution in zip(questions, resolutions):
        if resolver is not None and not getattr(resolution, "fills", False):
            # Unconditional draft (ignores draft_freeform): the owner is the submit gate on the
            # page, so every essay gap gets a first draft. Returns None for any non-essay /
            # sensitive / how-heard gap → we keep the original bail as a needs-input row.
            drafted = await resolver.draft_open_ended(question)
            if drafted is not None:
                resolution = drafted
        fields.append(ProposedField.from_resolution(question, resolution))
    return ProposedApplication(
        job_id=job_id,
        fields=fields,
        resume_path=resume_path or "",
        cover_letter_path=cover_letter_path or "",
        built_at=built_at or utcnow_iso(),
    )


# --------------------------------------------------------------- persistence
#
# A per-job JSON artifact (generated content lives as a file, spec §4). Local-only — never
# mirrored to telemetry (same as the answer values + EEO). The "In Progress" page reads it back so
# it survives a refresh; the batch grouping + per-job disposition (DB state) come in later phases.

def _proposed_dir(settings: Settings) -> Path:
    return settings.artifacts_dir / "proposed"


def proposed_path(settings: Settings, job_id: str) -> Path:
    """Where a job's complete proposed application is persisted: ``artifacts/proposed/<job_id>.json``."""
    return _proposed_dir(settings) / f"{job_id}.json"


def save_proposed(settings: Settings, proposed: ProposedApplication) -> Path:
    """Write the proposed application to its per-job JSON artifact (creating the folder). Returns
    the path. Atomic-ish: writes to a temp sibling then replaces, so a crash mid-write never leaves
    a half-written file the page would fail to parse."""
    path = proposed_path(settings, proposed.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proposed.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load_proposed(settings: Settings, job_id: str) -> ProposedApplication | None:
    """Read back a job's proposed application, or ``None`` when it is absent / unreadable / corrupt
    (a missing or half-written artifact must never break a caller — the page degrades gracefully)."""
    path = proposed_path(settings, job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return ProposedApplication.from_dict(data)
