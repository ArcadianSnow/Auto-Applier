"""The COMPLETE proposed application — batched assisted review, Phase 1 (prep-complete).

Covers (all browser-free):

  * the builder: standard identity/document rows + one row per custom question;
  * confident fills become trusted rows (no verify); non-filling gaps need verify;
  * **drafting made unconditional** — an open-ended essay gap is drafted even when the resolver's
    ``draft_freeform`` is OFF (the owner is the submit gate on the page);
  * honesty: a sensitive / how-heard / non-essay gap is NEVER drafted (stays a needs-input row);
  * ``resolver=None`` → no drafting (bails stay bails);
  * the resolver's public ``draft_open_ended`` gate in isolation, incl. the no-LLM + raise paths
    (the latter exercises the previously-undefined ``logger`` in ``_draft_open_ended``);
  * JSON persistence round-trip + graceful failure on a missing / corrupt artifact.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_applier.domain.models import Job
from auto_applier.resume.answer_resolver import (
    AnswerResolver,
    Resolution,
    ResolutionSource,
    SensitiveClass,
)
from auto_applier.resume.factbank import Contact, FactBank
from auto_applier.resume.proposed import (
    ProposedApplication,
    ProposedField,
    build_proposed_application,
    load_proposed,
    proposed_path,
    save_proposed,
)
from auto_applier.sources.browser.apply_base import Applicant, CustomQuestion


# ---- stubs / helpers --------------------------------------------------------

class _StubAnswerRepo:
    """The resolver only touches the answer repo in the bank/how-heard tiers, which these tests
    don't exercise (they pass pre-computed resolutions and only drive the draft path)."""

    def get(self, _question):
        return None

    def all(self):
        return []


class _StubLLM:
    """Returns a fixed freeform-draft payload (the shape the copilot's draft path reads)."""

    def __init__(self, answer: str = "This role fits my background and I would enjoy the work."):
        self._answer = answer
        self.calls = 0

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.calls += 1
        return {"answer": self._answer, "overclaim_risk": "none", "bank_evidence": []}


def _run(coro):
    return asyncio.run(coro)


def _bank() -> FactBank:
    return FactBank(
        contact=Contact(
            name="Pat Doe", email="pat@example.com", phone="555-0100", location="Seattle, WA",
        ),
        skills=["Python", "SQL"],
    )


def _applicant() -> Applicant:
    return Applicant("Pat", "Doe", "pat@example.com", "555-0100")


def _q(label: str, *, kind="input", required=True, field_id="q1", options=None) -> CustomQuestion:
    return CustomQuestion(
        field_id=field_id, label=label, required=required, kind=kind, options=options or [],
    )


def _fill(q: CustomQuestion, value: str, source=ResolutionSource.FACT_BANK) -> Resolution:
    return Resolution(question=q, value=value, source=source)


def _bail(q: CustomQuestion, *, sensitive=SensitiveClass.NONE, note="bailed") -> Resolution:
    return Resolution(
        question=q, value=None, source=ResolutionSource.REVIEW,
        confidence=0.0, sensitive=sensitive, needs_review=True, note=note,
    )


def _resolver(*, llm=None, draft_freeform=False, job=None) -> AnswerResolver:
    r = AnswerResolver(
        fact_bank=_bank(),
        answer_repo=_StubAnswerRepo(),
        embed_client=None,
        llm_client=llm,
        draft_freeform=draft_freeform,
    )
    r.current_job = job
    return r


# ---- builder: structure -----------------------------------------------------

def test_standard_identity_and_document_rows_are_always_present():
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(),
        resume_path="/tmp/Resume.pdf", cover_letter_path="",
        questions=[], resolutions=[], resolver=None,
    ))
    keys = {f.key for f in pa.fields}
    assert keys == {
        "applicant:name", "applicant:email", "applicant:phone",
        "doc:resume", "doc:cover_letter",
    }
    by_key = {f.key: f for f in pa.fields}
    assert by_key["applicant:name"].value == "Pat Doe"
    assert by_key["applicant:email"].needs_verify is False
    # résumé present → trusted; cover letter absent but optional → not a verify flag.
    assert by_key["doc:resume"].needs_verify is False
    assert by_key["doc:cover_letter"].needs_verify is False
    assert pa.resume_path == "/tmp/Resume.pdf"


def test_missing_required_identity_and_resume_flag_verify():
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=Applicant("", "", "", ""),
        resume_path="", cover_letter_path="",
        questions=[], resolutions=[], resolver=None,
    ))
    by_key = {f.key: f for f in pa.fields}
    assert by_key["applicant:name"].needs_verify is True
    assert by_key["applicant:email"].needs_verify is True
    assert by_key["doc:resume"].needs_verify is True      # required, absent
    assert by_key["applicant:phone"].needs_verify is False  # optional
    assert by_key["doc:cover_letter"].needs_verify is False  # optional


def test_confident_fill_is_trusted_not_verify():
    q = _q("Are you authorized to work in the US?")
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_fill(q, "Yes")], resolver=None,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.value == "Yes"
    assert field.needs_verify is False
    assert field.is_draft is False
    assert field.source == ResolutionSource.FACT_BANK.value


# ---- builder: drafting made unconditional -----------------------------------

def test_open_ended_gap_is_drafted_even_when_draft_freeform_off():
    """The whole point of Phase 1: the page drafts every essay gap regardless of the
    AUTO-submit ``draft_freeform`` switch — the owner is the submit gate there."""
    q = _q("Why do you want to work here?", kind="textarea")
    job = Job(source="greenhouse", source_job_id="x", title="Analyst", company="Acme")
    resolver = _resolver(llm=_StubLLM(), draft_freeform=False, job=job)

    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_bail(q)], resolver=resolver,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.is_draft is True
    assert field.needs_verify is True            # a draft is always the owner's to vet
    assert field.value                            # a real first draft, not blank
    assert field.source == ResolutionSource.DRAFT.value


def test_sensitive_gap_is_never_drafted():
    q = _q("What is your gender?", kind="select", options=["Male", "Female", "Prefer not to answer"])
    resolver = _resolver(llm=_StubLLM(), job=None)
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_bail(q, sensitive=SensitiveClass.EEO)], resolver=resolver,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.is_draft is False
    assert field.needs_verify is True
    assert field.value == ""


def test_how_heard_gap_is_never_drafted_despite_matching_open_ended():
    # "How did you hear about us?" matches an open-ended pattern, but honesty says never invent it.
    q = _q("How did you hear about us?", kind="select")
    resolver = _resolver(llm=_StubLLM(), job=None)
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_bail(q, note="how-heard: no source")], resolver=resolver,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.is_draft is False
    assert field.needs_verify is True


def test_non_essay_gap_is_not_drafted():
    q = _q("Do you have a valid driver's license?", kind="select", options=["Yes", "No"])
    resolver = _resolver(llm=_StubLLM(), job=None)
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_bail(q)], resolver=resolver,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.is_draft is False


def test_resolver_none_skips_drafting():
    q = _q("Why this company?", kind="textarea")
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[_bail(q)], resolver=None,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.is_draft is False
    assert field.needs_verify is True


def test_already_drafted_resolution_is_not_redrafted():
    """When ``draft_freeform`` is ON the driver already drafted the gap (fills=True), so the builder
    must reuse it rather than draft a second time."""
    q = _q("Why this company?", kind="textarea")
    drafted = Resolution(
        question=q, value="An existing draft.", source=ResolutionSource.DRAFT,
        confidence=0.0, needs_review=True, draft=True,
    )
    llm = _StubLLM()
    resolver = _resolver(llm=llm)
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q], resolutions=[drafted], resolver=resolver,
    ))
    field = next(f for f in pa.fields if f.key == "q:q1")
    assert field.value == "An existing draft."
    assert field.is_draft is True
    assert llm.calls == 0   # not re-drafted


# ---- summary ----------------------------------------------------------------

def test_summary_counts():
    q_fill = _q("Authorized to work?", field_id="q1")
    q_essay = _q("Why this role?", kind="textarea", field_id="q2")
    q_gap = _q("What is your gender?", field_id="q3")
    resolver = _resolver(llm=_StubLLM(), job=None)
    pa = _run(build_proposed_application(
        job_id="job-1", applicant=_applicant(), resume_path="/tmp/r.pdf", cover_letter_path="",
        questions=[q_fill, q_essay, q_gap],
        resolutions=[
            _fill(q_fill, "Yes"),
            _bail(q_essay),
            _bail(q_gap, sensitive=SensitiveClass.EEO),
        ],
        resolver=resolver,
    ))
    s = pa.summary()
    assert s["total"] == 8           # 5 standard + 3 questions
    assert s["drafted"] == 1         # the essay
    # confident = has a value AND no verify: name, email, phone, resume + the "Yes" fill = 5.
    # The cover letter row is blank (no cover attached) so it isn't a "confident" value.
    assert s["confident"] == 5
    assert s["needs_verify"] == 2    # the essay draft + the EEO gap


# ---- resolver.draft_open_ended gate (isolation) -----------------------------

def test_draft_open_ended_drafts_an_essay():
    q = _q("Describe a project you led", kind="textarea")
    resolver = _resolver(llm=_StubLLM(), job=None)
    res = _run(resolver.draft_open_ended(q))
    assert res is not None
    assert res.draft is True
    assert res.needs_review is True
    assert res.source is ResolutionSource.DRAFT


@pytest.mark.parametrize("label, kind, options", [
    ("What is your gender?", "select", ["Male", "Female"]),     # sensitive
    ("How did you hear about us?", "select", None),             # how-heard
    ("Years of experience", "input", None),                    # not open-ended
])
def test_draft_open_ended_returns_none_for_undraftable(label, kind, options):
    resolver = _resolver(llm=_StubLLM(), job=None)
    assert _run(resolver.draft_open_ended(_q(label, kind=kind, options=options))) is None


def test_draft_open_ended_returns_none_without_llm():
    resolver = _resolver(llm=None)
    assert _run(resolver.draft_open_ended(_q("Why us?", kind="textarea"))) is None


def test_draft_open_ended_swallows_copilot_error(monkeypatch):
    """A copilot explosion must return None, not raise — and must not NameError on the module
    ``logger`` that ``_draft_open_ended``'s except branch logs through (the latent bug Phase 1 fixed)."""
    async def _boom(self, *a, **k):
        raise RuntimeError("copilot exploded")

    monkeypatch.setattr("auto_applier.copilot.Copilot.answer", _boom)
    resolver = _resolver(llm=_StubLLM(), job=None)
    assert _run(resolver.draft_open_ended(_q("Why us?", kind="textarea"))) is None


# ---- persistence ------------------------------------------------------------

def test_save_load_round_trip(settings):
    q = _q("Why this company?", kind="textarea")
    resolver = _resolver(llm=_StubLLM(), job=None)
    pa = _run(build_proposed_application(
        job_id="job-xyz", applicant=_applicant(), resume_path="/tmp/r.pdf",
        cover_letter_path="/tmp/c.txt", questions=[q], resolutions=[_bail(q)], resolver=resolver,
    ))
    path = save_proposed(settings, pa)
    assert path == proposed_path(settings, "job-xyz")
    assert path.exists()

    loaded = load_proposed(settings, "job-xyz")
    assert loaded is not None
    assert loaded.to_dict() == pa.to_dict()
    assert loaded.job_id == "job-xyz"
    assert any(f.is_draft for f in loaded.fields)


def test_load_missing_returns_none(settings):
    assert load_proposed(settings, "nope") is None


def test_load_corrupt_returns_none(settings):
    path = proposed_path(settings, "bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_proposed(settings, "bad") is None


def test_from_dict_tolerates_partial_payload():
    pa = ProposedApplication.from_dict({"job_id": "j", "fields": [{"key": "k", "label": "L"}]})
    assert pa.job_id == "j"
    assert len(pa.fields) == 1
    assert pa.fields[0].key == "k"
    assert pa.fields[0].confidence == 0.0
