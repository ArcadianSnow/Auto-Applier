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
from auto_applier.resume.answer_resolver import (
    ProfileField, classify_profile_field, is_open_ended,
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


def _q(label: str, *, kind="input", required=True, field_id="question_42",
       options=None) -> CustomQuestion:
    return CustomQuestion(field_id=field_id, label=label, required=required, kind=kind,
                          options=options or [])


# ---- sensitive-field classification ----------------------------------------

@pytest.mark.parametrize("label, expected", [
    ("What is your gender?", SensitiveClass.EEO),
    ("Race / Ethnicity", SensitiveClass.EEO),
    ("Are you a protected veteran?", SensitiveClass.EEO),
    ("Do you have a disability?", SensitiveClass.EEO),
    ("Preferred pronouns", SensitiveClass.EEO),
    ("Are you legally authorized to work in the United States?", SensitiveClass.WORK_AUTHORIZATION),
    ("Right to work in the UK?", SensitiveClass.WORK_AUTHORIZATION),
    # "eligible to work" phrasing — missed live on Grafana 2026-06-12, now classified.
    ("Are you currently eligible to work in your country of residence?",
     SensitiveClass.WORK_AUTHORIZATION),
    ("Do you require visa sponsorship?", SensitiveClass.SPONSORSHIP),
    ("Will you now or in the future require sponsorship?", SensitiveClass.SPONSORSHIP),
    ("Salary expectation", SensitiveClass.SALARY),
    ("What are your compensation requirements?", SensitiveClass.SALARY),
    ("Why do you want to work here?", SensitiveClass.NONE),
    ("Tell us about a project you led", SensitiveClass.NONE),
])
def test_classify_sensitive(label, expected):
    assert classify_sensitive(label) is expected


# ---- human-attestation gate (blocker A) ------------------------------------

@pytest.mark.parametrize("label", [
    "Are you a human or an automated program?",
    "Please confirm you are a human being",
    "Are you a bot?",
    "Are you a real person?",
    "This application was completed by a human being (not an automated program)",
])
def test_classify_human_attestation_from_label(label):
    assert classify_sensitive(label) is SensitiveClass.HUMAN_ATTESTATION


def test_classify_human_attestation_bare_describes_you_label():
    """The live Grafana gate: a react-select labelled exactly this, whose AI/human
    options are NOT in the DOM at discovery → caught by the label alone (no options)."""
    assert classify_sensitive("Which of the following best describes you?*") \
        is SensitiveClass.HUMAN_ATTESTATION


def test_describes_you_with_demographic_noun_is_eeo_not_attestation():
    """A self-ID question ("…best describes your gender/race") stays EEO (→ prefer-not),
    not the attestation gate — the demographic noun disambiguates."""
    assert classify_sensitive("Which of the following best describes your gender?") \
        is SensitiveClass.EEO
    assert classify_sensitive("Select the option that best describes your race/ethnicity") \
        is SensitiveClass.EEO


def test_classify_human_attestation_from_option_pair():
    """Non-descriptive label ("Which of the following best describes you?") is caught
    by the human-vs-AI shape of its options — the live 2026-06-12 gate."""
    opts = ["I am an AI or automated program", "I am a human being"]
    assert classify_sensitive("Which of the following best describes you?", opts) \
        is SensitiveClass.HUMAN_ATTESTATION


def test_classify_human_attestation_wins_over_other_classes():
    # Even if other sensitive words co-occur, the safety gate takes priority.
    assert classify_sensitive(
        "Are you a human being authorized to work?",
    ) is SensitiveClass.HUMAN_ATTESTATION


def test_normal_select_options_do_not_false_positive():
    # A benign select (country / years) has neither the human nor the AI marker pair.
    assert classify_sensitive("Country of residence",
                              ["United States", "Canada", "Germany"]) is SensitiveClass.NONE
    assert classify_sensitive("Years of experience",
                              ["0-2", "3-5", "6+"]) is SensitiveClass.NONE


@pytest.mark.parametrize("label", [
    'I have read and understand Tailscale\'s "Candidate Privacy Policy" and "AI Guidelines"',
    "I agree to the terms and conditions",
    "I acknowledge the privacy policy",
    "I consent to the processing of my data",
])
def test_classify_consent(label):
    assert classify_sensitive(label) is SensitiveClass.CONSENT


def test_consent_always_bails_to_review():
    """A bot must not knowingly consent (privacy/terms/AI-guidelines) — bail to the human."""
    resolver = AnswerResolver(_bank(), answer_repo=_make_empty_repo(), llm_client=StubLLM(reply=_copilot_reply()))
    res = asyncio.run(resolver.resolve(_q(
        'I have read and understand the "Candidate Privacy Policy" and "AI Guidelines"',
        kind="select", options=["Yes", "No"])))
    assert res.needs_review is True
    assert res.sensitive is SensitiveClass.CONSENT


def test_human_attestation_always_bails_to_review():
    """The bot must NEVER attest to being human. Always REVIEW → driver downgrades to
    assisted, where the human truthfully attests (research/automated-apply-go-live.md)."""
    resolver = AnswerResolver(_bank(), answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q(
        "Which of the following best describes you?", kind="select",
        options=["I am an AI or automated program", "I am a human being"])))
    assert res.value is None
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW
    assert res.sensitive is SensitiveClass.HUMAN_ATTESTATION


def test_human_attestation_never_reaches_llm():
    """A misfiring LLM must not be able to answer the gate — Tier-0 catches it first."""
    llm = StubLLM(reply=_copilot_reply(verdict="yes", short_answer="I am a human being"))
    resolver = AnswerResolver(_bank(), answer_repo=_make_empty_repo(), llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Are you a human or an automated program?")))
    assert res.needs_review is True
    assert res.sensitive is SensitiveClass.HUMAN_ATTESTATION
    assert llm.last_prompt == ""  # never invoked


# ---- profile/contact fields + open-ended bail (2026-06-12 rehearsal #2) ----------

def _contact_bank():
    return FactBank(
        contact=Contact(
            name="Joseph Lira", email="j@x.com",
            location="Dallas, Texas, United States",
            links={"LinkedIn": "https://linkedin.com/in/joseph-lira/"},
        ),
        work_authorization="US Citizen", requires_sponsorship=False,
    )


@pytest.mark.parametrize("label, expected", [
    ("LinkedIn Profile", ProfileField.LINKEDIN),
    ("GitHub", ProfileField.GITHUB),
    ("Website", ProfileField.WEBSITE),
    ("Preferred First Name", ProfileField.PREFERRED_FIRST_NAME),
    ("Location (City)", ProfileField.CITY),
    ("What country and time zone are you based in?", ProfileField.COUNTRY_TIMEZONE),
    ("Where are you located?", ProfileField.LOCATION),
    ("Why do you want this job?", ProfileField.NONE),
])
def test_classify_profile_field(label, expected):
    assert classify_profile_field(label) is expected


def test_profile_linkedin_fills_from_bank():
    resolver = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("LinkedIn Profile")))
    assert res.value == "https://linkedin.com/in/joseph-lira/"
    assert res.source is ResolutionSource.PROFILE
    assert res.needs_review is False


def test_profile_missing_website_bails_not_negates():
    """The KEY fix: 'Website' with no bank link BAILS (blank), never 'No I have not built
    a website.' A blank the human fills beats a confident wrong negation."""
    resolver = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Website")))
    assert res.value is None
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW


def test_profile_website_falls_back_to_github():
    """Website/Portfolio fields ("i.e. website, github, blogs") use the GitHub link when
    there's no dedicated personal site."""
    bank = _contact_bank()
    bank.contact.links["GitHub"] = "https://github.com/ArcadianSnow"
    r = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(r.resolve(_q("Portfolio (i.e. website, github, blogs, etc)")))
    assert res.value == "https://github.com/ArcadianSnow"
    assert res.source is ResolutionSource.PROFILE


def test_profile_preferred_name_and_city_and_timezone():
    r = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo())
    assert asyncio.run(r.resolve(_q("Preferred First Name"))).value == "Joseph"
    assert asyncio.run(r.resolve(_q("Location (City)"))).value == "Dallas"
    tz = asyncio.run(r.resolve(_q("What country and time zone are you based in?"))).value
    assert "United States" in tz and "Central Time" in tz


def test_open_ended_detection():
    assert is_open_ended("Why are you interested in this role?") is True
    assert is_open_ended("Describe your experience with sales") is True
    assert is_open_ended("Tell us about a project you led") is True
    assert is_open_ended("Anything else?", kind="textarea") is True   # textarea = prose
    assert is_open_ended("Are you authorized to work in the US?") is False


def test_open_ended_bails_instead_of_llm_inventing_an_essay():
    """The harm fix: an open-ended motivation prompt with no prepared answer must BAIL to
    assisted — the LLM must NOT free-write (it wrote 'Not interested in a Solutions
    Engineer role' live). The copilot is never even invoked."""
    llm = StubLLM(reply=_copilot_reply(verdict="no", short_answer="Not interested in a Solutions Engineer role"))
    resolver = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo(), llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Why are you interested in a Solutions Engineer role?",
                                         kind="textarea")))
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW
    assert llm.last_prompt == ""  # LLM never invoked for the essay


def test_open_ended_fills_from_bank_when_seeded(answer_repo):
    """When the prepared answer IS seeded, the open-ended prompt fills from the bank (the
    point of seeding) — bailing only happens when there's no prepared answer."""
    asyncio.run(store_answer(answer_repo, embed_client=None,
                             question="Why are you interested in a Solutions Engineer role?",
                             answer="Because it combines deep technical work with customer contact..."))
    resolver = AnswerResolver(_contact_bank(), answer_repo)
    res = asyncio.run(resolver.resolve(_q("Why are you interested in a Solutions Engineer role?",
                                          kind="textarea")))
    assert res.value.startswith("Because it combines")
    assert res.source is ResolutionSource.BANK


def test_work_auth_yes_no_eligibility_maps_to_yes():
    """'Are you eligible to work…?' (yes/no) → 'Yes', not the status string 'US Citizen'
    (which wouldn't match a Yes/No select — live blank 2026-06-12)."""
    resolver = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(
        _q("Are you currently eligible to work in your country of residence?", kind="select")))
    assert res.value == "Yes"
    assert res.sensitive is SensitiveClass.WORK_AUTHORIZATION


def test_work_auth_status_question_returns_status_string():
    resolver = AnswerResolver(_contact_bank(), answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("What is your work authorization status?")))
    assert res.value == "US Citizen"


# ---- §8d policy: work auth / sponsorship --------------------------------------

def test_work_auth_uses_factbank_value():
    # "Are you authorized…?" is a yes/no question → "Yes" (citizen ⇒ authorized), which
    # matches a Yes/No dropdown. The fact-bank value still gates it (no silent default).
    bank = _bank(work_authorization="US citizen")
    resolver = AnswerResolver(bank, answer_repo=_make_empty_repo())
    res = asyncio.run(resolver.resolve(_q("Are you authorized to work in the US?")))
    assert res.value == "Yes"
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


# ---- Tier 2: LLM backup — copilot-audited (spec §8f) -------------------------
#
# Tier-3 routes through the §8f copilot: the reply must be the copilot schema and
# a yes/partial verdict must cite bank facts that pass the deterministic evidence
# audit. Self-reported confidence is gone — it was the overclaim trap (live
# 2026-06-11: qwen3:8b self-reported 0.95 on Kubernetes/Go judgment calls).

def _copilot_reply(**over) -> dict:
    base = dict(
        verdict="yes",
        short_answer="5",
        long_answer="Five years of professional Python and SQL work.",
        reasoning="The bank lists Python and SQL experience.",
        bank_evidence=["Python", "SQL"],   # both in _bank() → audit passes
        overclaim_risk="none",
        risk_note="",
        framing="",
        gaps=[],
    )
    base.update(over)
    return base


def test_llm_audited_grounded_answer_fills_as_inferred(answer_repo):
    llm = StubLLM(reply=_copilot_reply())
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Years of professional experience?")))
    assert res.value == "5"
    assert res.source is ResolutionSource.INFERRED
    assert res.confidence == pytest.approx(0.9)  # structural, not self-reported
    assert res.needs_review is False
    assert "copilot-audited" in res.note
    # Prompt carries the fact bank so the model must ground its evidence.
    assert "Python" in llm.last_prompt


def test_llm_unsupported_yes_fails_the_audit_and_bails(answer_repo):
    # The model says yes citing experience the bank does NOT contain — the
    # deterministic evidence audit voids it and the question goes to REVIEW.
    llm = StubLLM(reply=_copilot_reply(
        bank_evidence=["operated production Kubernetes clusters at scale"]))
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Production Kubernetes experience?")))
    assert res.needs_review is True
    assert res.source is ResolutionSource.REVIEW


def test_llm_high_self_flagged_risk_bails(answer_repo):
    llm = StubLLM(reply=_copilot_reply(overclaim_risk="high",
                                       risk_note="this is a stretch"))
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Have you led large teams?")))
    assert res.needs_review is True


def test_llm_honest_no_fills_without_evidence(answer_repo):
    # "No" needs no evidence — the guarded risk is overclaim, not underclaim.
    llm = StubLLM(reply=_copilot_reply(
        verdict="no", short_answer="No", bank_evidence=[]))
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Have you ever piloted a submarine?")))
    assert res.value == "No"
    assert res.source is ResolutionSource.INFERRED
    assert res.needs_review is False


def test_llm_unavailable_bails(answer_repo):
    llm = StubLLM(raise_exc=RuntimeError("model down"))
    resolver = AnswerResolver(_bank(), answer_repo, llm_client=llm)
    res = asyncio.run(resolver.resolve(_q("Tell us about yourself")))
    assert res.needs_review is True


def test_llm_malformed_reply_bails(answer_repo):
    llm = StubLLM(reply={"answer": None, "confidence": "high"})  # not copilot schema
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
