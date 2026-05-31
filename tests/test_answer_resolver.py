"""Tests for the two-tier answer resolver (spec §8b, §8d).

Covers: sensitive-field classification + policy; exact bank match (no embedding round-
trip); semantic match via injected embedding stub; LLM tier-3 confidence gating;
required-Q REVIEW bail; the v2-answers seeder.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from auto_applier.db import init_app_db
from auto_applier.db.repositories import AnswerRepo
from auto_applier.llm.embed import EmbeddingClient, bytes_to_vec, cosine, vec_to_bytes
from auto_applier.resume.answer_resolver import (
    AnswerResolver,
    ResolutionSource,
    SensitiveClass,
    classify_sensitive,
    store_answer,
)
from auto_applier.resume.factbank import Contact, FactBank
from auto_applier.resume.seed_answers import seed_from_v2_file
from auto_applier.sources.browser.apply_base import CustomQuestion


# ---- stubs ------------------------------------------------------------------

class StubEmbedder:
    """Deterministic embedder: each call returns the vector registered for that text.

    Unregistered text returns a zero vector — keeps tests honest about which lookups
    actually exercise the bank vs. fall through.
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None):
        self.vectors = vectors or {}
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vectors.get(text, [0.0, 0.0, 0.0]))


class StubLLM:
    """Returns a pre-seeded JSON reply (or raises). Captures the prompt for assertions."""

    def __init__(self, reply: dict | None = None, raise_exc: Exception | None = None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.last_prompt: str = ""
        self.last_system: str = ""

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.last_prompt = prompt
        self.last_system = system
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.reply or {}


# ---- fixtures ---------------------------------------------------------------

def _bank(**over) -> FactBank:
    bank = FactBank(
        contact=Contact(name="Pat Doe", email="pat@example.com", location="Seattle, WA"),
        skills=["Python", "SQL"],
    )
    for k, v in over.items():
        setattr(bank, k, v)
    return bank


@pytest.fixture
def answer_repo(tmp_path):
    db = init_app_db(tmp_path / "app.db")
    return AnswerRepo(db)


def _q(label: str, *, kind="input", required=True, field_id="question_42") -> CustomQuestion:
    return CustomQuestion(field_id=field_id, label=label, required=required, kind=kind)


# ---- sensitive-field classification ----------------------------------------

@pytest.mark.parametrize("label, expected", [
    ("What is your gender?", SensitiveClass.EEO),
    ("Race / Ethnicity", SensitiveClass.EEO),
    ("Are you a protected veteran?", SensitiveClass.EEO),
    ("Do you have a disability?", SensitiveClass.EEO),
    ("Preferred pronouns", SensitiveClass.EEO),
    ("Are you legally authorized to work in the United States?", SensitiveClass.WORK_AUTHORIZATION),
    ("Right to work in the UK?", SensitiveClass.WORK_AUTHORIZATION),
    ("Do you require visa sponsorship?", SensitiveClass.SPONSORSHIP),
    ("Will you now or in the future require sponsorship?", SensitiveClass.SPONSORSHIP),
    ("Salary expectation", SensitiveClass.SALARY),
    ("What are your compensation requirements?", SensitiveClass.SALARY),
    ("Why do you want to work here?", SensitiveClass.NONE),
    ("Tell us about a project you led", SensitiveClass.NONE),
])
def test_classify_sensitive(label, expected):
    assert classify_sensitive(label) is expected


# ---- §8d policy: work auth / sponsorship --------------------------------------

def test_work_auth_uses_factbank_value():
    bank = _bank(work_authorization="US citizen")
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Are you authorized to work in the US?")))
    assert res.value == "US citizen"
    assert res.source is ResolutionSource.FACT_BANK
    assert res.sensitive is SensitiveClass.WORK_AUTHORIZATION
    assert res.needs_review is False


def test_work_auth_missing_factbank_bails_to_review():
    """v2's 'authorized = Yes' default is explicitly retired here (spec §8d, memory
    [[project_us_default_assumption]])."""
    bank = _bank()  # no work_authorization set
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Authorized to work in this country?")))
    assert res.value is None
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW
    assert res.sensitive is SensitiveClass.WORK_AUTHORIZATION


def test_sponsorship_uses_factbank_boolean():
    bank = _bank(requires_sponsorship=False)
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Do you require visa sponsorship?")))
    assert res.value == "No"
    assert res.source is ResolutionSource.FACT_BANK
    assert res.sensitive is SensitiveClass.SPONSORSHIP


def test_sponsorship_unset_bails_to_review():
    bank = _bank()  # requires_sponsorship is None
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Will you require sponsorship in the future?")))
    assert res.needs_review is True
    assert res.sensitive is SensitiveClass.SPONSORSHIP


# ---- §8d policy: EEO -------------------------------------------------------

def test_eeo_uses_user_self_id_value_when_present():
    bank = _bank(eeo={"gender": "Female"})
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Gender", kind="select")))
    assert res.value == "Female"
    assert res.sensitive is SensitiveClass.EEO
    assert res.source is ResolutionSource.BANK


def test_eeo_defaults_to_prefer_not_when_blank():
    bank = _bank(eeo={})
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Race / Ethnicity", kind="select")))
    assert res.value == "Prefer not to answer"
    assert res.source is ResolutionSource.SENSITIVE_DEFAULT
    assert res.sensitive is SensitiveClass.EEO
    # NOT review — answering "prefer not to answer" is a valid submission per §8d.
    assert res.needs_review is False


# ---- §8d policy: salary ----------------------------------------------------

def test_salary_uses_user_config():
    bank = _bank()
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo(), salary_expectation="125000")
    res = asyncio.run(resolver.resolve(_q("What is your salary expectation?")))
    assert res.value == "125000"
    assert res.source is ResolutionSource.USER_CONFIG
    assert res.sensitive is SensitiveClass.SALARY


def test_salary_missing_config_bails():
    resolver = AnswerResolver(_bank(), answer_repo=_make_empty_repo())  # no salary set
    res = asyncio.run(resolver.resolve(_q("Salary expectation")))
    assert res.needs_review is True


# ---- Tier 1: exact bank match -----------------------------------------------

def test_exact_question_text_match_skips_embedding(answer_repo):
    """v2 users' seeded answers.json hits exact-match without firing the embedder."""
    asyncio.run(store_answer(answer_repo, embed_client=None,
                             question="How many years of Python?", answer="6"))
    embedder = StubEmbedder()
    resolver = AnswerResolver(_bank(), answer_repo, embed_client=embedder)
    res = asyncio.run(resolver.resolve(_q("How many years of Python?")))
    assert res.value == "6"
    assert res.source is ResolutionSource.BANK
    assert embedder.calls == []  # never embedded — fast path


# ---- Tier 1: semantic match -------------------------------------------------

def test_semantic_match_uses_cosine_threshold(answer_repo):
    """Differently-worded form question hits the stored Q by embedding cosine."""
    stored_q = "How many years of experience do you have with SQL?"
    asked_q = "Years of SQL experience"
    # Hand-crafted near-duplicate vectors — cosine ~0.99.
    embedder = StubEmbedder({
        stored_q: [1.0, 0.1, 0.0],
        asked_q:  [0.99, 0.12, 0.0],
    })
    asyncio.run(store_answer(answer_repo, embed_client=embedder,
                             question=stored_q, answer="6"))
    resolver = AnswerResolver(_bank(), answer_repo, embed_client=embedder)
    res = asyncio.run(resolver.resolve(_q(asked_q)))
    assert res.value == "6"
    assert res.source is ResolutionSource.BANK
    assert res.confidence > 0.95
    assert "semantic match" in res.note


def test_semantic_match_below_threshold_falls_through(answer_repo):
    """Unrelated stored answer (cosine well below 0.78) -> miss, drops to next tier."""
    embedder = StubEmbedder({
        "Cake flavor?": [0.0, 1.0, 0.0],
        "Spaceship velocity": [1.0, 0.0, 0.0],
    })
    asyncio.run(store_answer(answer_repo, embed_client=embedder,
                             question="Cake flavor?", answer="chocolate"))
    resolver = AnswerResolver(_bank(), answer_repo, embed_client=embedder)
    # No LLM client -> falls all the way to REVIEW.
    res = asyncio.run(resolver.resolve(_q("Spaceship velocity")))
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW


# ---- Tier 2: LLM confidence gating ------------------------------------------

def test_llm_high_confidence_inferred(answer_repo):
    llm = StubLLM(reply={"answer": "5", "confidence": 0.85})
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Years of professional experience?")))
    assert res.value == "5"
    assert res.source is ResolutionSource.INFERRED
    assert res.confidence == pytest.approx(0.85)
    assert res.needs_review is False
    # Prompt carries the fact bank so the LLM can self-judge confidence.
    assert "candidate facts" in llm.last_prompt.lower()


def test_llm_low_confidence_bails_to_review(answer_repo):
    llm = StubLLM(reply={"answer": "maybe", "confidence": 0.3})
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Have you ever piloted a submarine?")))
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW


def test_llm_unavailable_bails(answer_repo):
    llm = StubLLM(raise_exc=RuntimeError("model down"))
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Tell us about yourself")))
    assert res.needs_review is True


def test_llm_malformed_reply_bails(answer_repo):
    llm = StubLLM(reply={"answer": None, "confidence": "high"})
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Why?")))
    assert res.needs_review is True


# ---- batch resolve_all ------------------------------------------------------

def test_resolve_all_preserves_order(answer_repo):
    asyncio.run(store_answer(answer_repo, embed_client=None,
                             question="Highest level of education", answer="Bachelor's"))
    resolver = AnswerResolver(_bank(work_authorization="US citizen"), answer_repo)
    qs = [
        _q("Highest level of education", kind="select", field_id="q1"),
        _q("Authorized to work in the US?", kind="select", field_id="q2"),
        _q("Random unanswerable thing?", kind="textarea", field_id="q3"),
    ]
    out = asyncio.run(resolver.resolve_all(qs))
    assert [r.question.field_id for r in out] == ["q1", "q2", "q3"]
    assert out[0].value == "Bachelor's"
    assert out[1].value == "US citizen"
    assert out[2].needs_review is True


# ---- cosine sanity ---------------------------------------------------------

def test_cosine_identity_and_orthogonal():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0           # mismatched len
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # zero norm


def test_vec_codec_roundtrip():
    v = [0.1, -0.2, 0.3, 0.0, 1.0]
    assert bytes_to_vec(vec_to_bytes(v)) == pytest.approx(v)
    assert bytes_to_vec(None) == []
    assert bytes_to_vec(b"") == []


# ---- v2 seeder --------------------------------------------------------------

def test_seed_from_v2_file_idempotent(answer_repo, tmp_path):
    path = tmp_path / "v2answers.json"
    path.write_text(json.dumps({
        "Are you legally authorized to work in the United States?": "Yes",
        "Years of Python experience": "5",
    }), encoding="utf-8")
    n = asyncio.run(seed_from_v2_file(answer_repo, embed_client=None, v2_answers_path=path))
    assert n == 2
    # Re-run is idempotent (UPSERT) — still 2 rows in the repo, not 4.
    n2 = asyncio.run(seed_from_v2_file(answer_repo, embed_client=None, v2_answers_path=path))
    assert n2 == 2
    assert len(answer_repo.all()) == 2


def test_seed_from_v2_file_missing_is_noop(answer_repo, tmp_path):
    n = asyncio.run(seed_from_v2_file(answer_repo, None, tmp_path / "doesnotexist.json"))
    assert n == 0


# ---- internals --------------------------------------------------------------

def _make_empty_repo():
    """Tiny in-memory stand-in for AnswerRepo used when the bank tier doesn't matter.

    All sensitive-field tests bypass the bank entirely (Tier 0 wins), so this stub
    just needs ``get`` / ``all`` returning empty.
    """

    class _Empty:
        def get(self, _q): return None
        def all(self): return []
        def upsert(self, _a): return None

    return _Empty()
