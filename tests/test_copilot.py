"""Application copilot tests (spec §8f) — the evidence audit is the load-bearing part."""

from __future__ import annotations

import asyncio

import pytest

from auto_applier.copilot import (
    DRAFT_VERDICT,
    VERDICTS,
    Copilot,
    CopilotAnswer,
    audit_evidence,
)
from auto_applier.domain.models import Job
from auto_applier.resume.factbank import Contact, FactBank, WorkEntry


def _bank(**over) -> FactBank:
    base = dict(
        contact=Contact(name="Ada", email="a@b.c", location="Dallas, Texas"),
        work_history=[WorkEntry(
            company="Acme Health", title="Database Administrator",
            start="2023", end="Present",
            bullets=[
                "Led the design and build of a custom n8n integration suite with "
                "watermark-based incremental change detection between Azure SQL and a CRM",
                "Rebuilt month-end billing for 13 partner labs as one idempotent orchestrator",
            ],
        )],
        skills=["Python", "SQL Server", "Azure SQL", "n8n"],
        allowed_metrics=["13 partner labs", "190+ tables"],
        work_authorization="US citizen",
        requires_sponsorship=False,
        eeo={"veteran": "I am not a veteran"},
    )
    base.update(over)
    return FactBank(**base)


class _StubLLM:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.exc is not None:
            raise self.exc
        return self.payload


def _payload(**over) -> dict:
    base = dict(
        verdict="yes",
        short_answer="Yes",
        long_answer="I built a watermark-based incremental sync between Azure SQL and a CRM.",
        reasoning="The bank shows hands-on incremental sync work.",
        bank_evidence=["custom n8n integration suite with watermark-based incremental change detection"],
        overclaim_risk="low",
        risk_note="",
        framing="Anchor on the n8n suite.",
        gaps=["Debezium"],
    )
    base.update(over)
    return base


def _ask(copilot: Copilot, question: str, **kw) -> CopilotAnswer:
    return asyncio.run(copilot.answer(question, _bank(), **kw))


# ------------------------------------------------------------------ evidence audit

def test_audit_supports_verbatim_bullet_fragment():
    supported, unsupported = audit_evidence(
        _bank(), ["watermark-based incremental change detection"])
    assert supported and not unsupported


def test_audit_supports_skills_and_metrics():
    supported, unsupported = audit_evidence(_bank(), ["Azure SQL", "13 partner labs"])
    assert len(supported) == 2 and not unsupported


def test_audit_rejects_invented_experience():
    supported, unsupported = audit_evidence(
        _bank(), ["led a production Debezium deployment on Kafka Connect"])
    assert not supported and unsupported


def test_audit_mixed():
    supported, unsupported = audit_evidence(
        _bank(), ["n8n", "five years of Kubernetes administration"])
    assert supported == ["n8n"]
    assert unsupported == ["five years of Kubernetes administration"]


def test_audit_empty_and_blank_items():
    supported, unsupported = audit_evidence(_bank(), ["", "   "])
    assert not supported and len(unsupported) == 2


# ------------------------------------------------------------------ happy path

def test_answer_grounded_yes_passes_audit():
    stub = _StubLLM(payload=_payload())
    ans = _ask(Copilot(stub), "Have you built incremental data sync between systems?")
    assert ans.verdict == "yes"
    assert not ans.needs_review
    assert ans.unsupported_evidence == []
    assert ans.long_answer.startswith("I built")


def test_answer_prompt_carries_bank_question_and_job_context():
    stub = _StubLLM(payload=_payload())
    job = Job(source="lever", source_job_id="j1", title="Data Engineer",
              company="Monzo", url="u", location="UK Remote",
              description="JD " * 2000)
    _ask(Copilot(stub), "Have you built sync?", job=job)
    prompt = stub.prompts[0]
    assert "Acme Health" in prompt           # bank facts present
    assert "Have you built sync?" in prompt
    assert "Monzo" in prompt and "UK Remote" in prompt
    assert "[...]" in prompt                 # JD excerpt truncated


# ------------------------------------------------------------------ the honesty gate

def test_unsupported_yes_fails_closed_to_review():
    stub = _StubLLM(payload=_payload(
        bank_evidence=["led a Debezium implementation at scale"]))
    ans = _ask(Copilot(stub), "Have you led a Debezium implementation?")
    assert ans.verdict == "review"
    assert ans.needs_review
    assert ans.overclaim_risk == "high"
    assert ans.unsupported_evidence == ["led a Debezium implementation at scale"]
    assert any("failed closed" in n for n in ans.audit_notes)


def test_yes_with_no_evidence_at_all_fails_closed():
    stub = _StubLLM(payload=_payload(bank_evidence=[]))
    ans = _ask(Copilot(stub), "Have you led a Debezium implementation?")
    assert ans.verdict == "review" and ans.needs_review


def test_partial_without_support_fails_closed():
    stub = _StubLLM(payload=_payload(verdict="partial", bank_evidence=["Kafka Streams work"]))
    ans = _ask(Copilot(stub), "CDC experience?")
    assert ans.verdict == "review" and ans.needs_review


def test_no_verdict_needs_no_evidence():
    stub = _StubLLM(payload=_payload(
        verdict="no", short_answer="No", bank_evidence=[]))
    ans = _ask(Copilot(stub), "Have you led a Debezium implementation?")
    assert ans.verdict == "no"
    assert not ans.needs_review


def test_mixed_evidence_keeps_yes_but_raises_risk():
    stub = _StubLLM(payload=_payload(
        bank_evidence=["n8n", "production Debezium at scale"], overclaim_risk="none"))
    ans = _ask(Copilot(stub), "Integration experience?")
    assert ans.verdict == "yes"            # one supported item keeps the verdict
    assert ans.overclaim_risk == "high"    # but the invented one flags it
    assert ans.unsupported_evidence == ["production Debezium at scale"]


# ------------------------------------------------------------------ malformed / failure

@pytest.mark.parametrize("payload", [
    {"verdict": "maybe"},
    {"verdict": ""},
    {},
    ["not", "a", "dict"],
])
def test_malformed_reply_goes_to_review(payload):
    ans = _ask(Copilot(_StubLLM(payload=payload)), "Anything?")
    assert ans.verdict == "review" and ans.needs_review


def test_llm_failure_goes_to_review_never_raises():
    ans = _ask(Copilot(_StubLLM(exc=RuntimeError("ollama down"))), "Anything?")
    assert ans.verdict == "review" and ans.needs_review


def test_no_llm_client_goes_to_review():
    ans = _ask(Copilot(None), "Anything?")
    assert ans.verdict == "review" and ans.needs_review


def test_empty_question_goes_to_review():
    ans = _ask(Copilot(_StubLLM(payload=_payload())), "   ")
    assert ans.verdict == "review"


def test_unparseable_risk_becomes_high():
    stub = _StubLLM(payload=_payload(overclaim_risk="banana"))
    ans = _ask(Copilot(stub), "Integration experience?")
    assert ans.overclaim_risk == "high"


# ------------------------------------------------------------------ sensitive routing (never the LLM)

def test_sponsorship_is_deterministic_no_llm_call():
    stub = _StubLLM(payload=_payload())
    ans = _ask(Copilot(stub), "Will you now or in the future require visa sponsorship?")
    assert ans.source == "policy"
    assert ans.short_answer == "No"
    assert stub.prompts == []  # the LLM was never consulted


def test_sponsorship_missing_bails_to_review():
    stub = _StubLLM(payload=_payload())
    ans = asyncio.run(Copilot(stub).answer(
        "Do you require sponsorship?", _bank(requires_sponsorship=None)))
    assert ans.needs_review and ans.source != "llm"


def test_work_auth_from_bank():
    ans = _ask(Copilot(_StubLLM()), "Are you legally authorized to work in the US?")
    assert ans.source == "policy"
    assert ans.short_answer == "US citizen"


def test_work_auth_missing_bails():
    ans = asyncio.run(Copilot(_StubLLM()).answer(
        "Are you authorized to work in the US?", _bank(work_authorization="")))
    assert ans.needs_review


def test_salary_uses_injected_ask():
    ans = _ask(Copilot(_StubLLM()), "What are your salary expectations?",
               salary_ask="$120,000")
    assert ans.source == "policy" and ans.short_answer == "$120,000"


def test_salary_without_ask_bails():
    ans = _ask(Copilot(_StubLLM()), "What is your expected compensation?")
    assert ans.needs_review


def test_eeo_self_id_or_prefer_not():
    ans = _ask(Copilot(_StubLLM()), "Are you a protected veteran?")
    assert ans.source == "policy" and ans.short_answer == "I am not a veteran"
    ans2 = _ask(Copilot(_StubLLM()), "What is your gender?")
    assert ans2.short_answer == "Prefer not to answer"


def test_verdicts_constant_is_closed():
    assert set(VERDICTS) == {"yes", "no", "partial"}


# ------------------------------------------------------------------ freeform DRAFT path (BUILD 6)
#
# Open-ended/essay prompts are NOT yes/no screeners — the verdict prompt mis-fits them
# (measured live: "Why Stripe?" → verdict="no" + "I'm excited" ×2). They route to the
# freeform draft path: grounded, voice-clean, fabrication-guarded, ALWAYS needs_review.

class _SeqLLM:
    """Returns a sequence of payloads (or raises an Exception item), one per call —
    lets a test exercise the best-of-two retry distinctly from the single-payload stub."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.calls = 0

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        self.prompts.append(prompt)
        self.systems.append(system)
        p = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        if isinstance(p, Exception):
            raise p
        return p


def _draft_payload(**over) -> dict:
    """A freeform-draft reply shape: {answer, bank_evidence, overclaim_risk, risk_note, gaps}.
    The default answer is bank-grounded and tell-free so the clean path stays clean."""
    base = dict(
        answer=("At Acme Health, the month-end billing for 13 partner labs became one "
                "idempotent orchestrator. That data work is what this role needs."),
        bank_evidence=["Rebuilt month-end billing for 13 partner labs as one idempotent orchestrator"],
        overclaim_risk="low",
        risk_note="",
        gaps=[],
    )
    base.update(over)
    return base


_WHY = "Why do you want to work here?"


def test_open_ended_routes_to_draft_not_verdict():
    stub = _StubLLM(payload=_draft_payload())
    ans = _ask(Copilot(stub), _WHY)
    assert ans.verdict == DRAFT_VERDICT
    assert ans.verdict not in VERDICTS          # never a yes/no/partial on an essay
    assert ans.needs_review                     # a draft is always the human's to submit
    assert ans.long_answer.startswith("At Acme Health")
    assert any("freeform DRAFT" in n for n in ans.audit_notes)


def test_draft_uses_the_freeform_prompt():
    stub = _StubLLM(payload=_draft_payload())
    _ask(Copilot(stub), "Tell us about a challenging project you led.")
    assert "Freeform application question" in stub.prompts[0]
    assert "freeform" in stub.systems[0].lower()


def test_clean_draft_is_not_flagged_high():
    stub = _StubLLM(payload=_draft_payload())
    ans = _ask(Copilot(stub), _WHY)
    assert ans.overclaim_risk in ("none", "low")      # model risk preserved, not forced high
    assert ans.unsupported_evidence == []
    assert not any("fabrication guard" in n for n in ans.audit_notes)
    assert not any("AI-tell" in n for n in ans.audit_notes)
    assert stub.systems and stub.prompts                # the LLM was consulted
    assert len(stub.prompts) == 1                       # clean first draft → no retry


def test_draft_with_invented_tech_is_flagged_but_returned():
    stub = _StubLLM(payload=_draft_payload(
        answer="I deployed and operated production Kubernetes clusters across three regions.",
        bank_evidence=["production Kubernetes administration at scale"],
    ))
    ans = _ask(Copilot(stub), "Describe your infrastructure experience.")
    assert ans.verdict == DRAFT_VERDICT
    assert ans.long_answer                                # NEVER blanked — flagged, not skipped
    assert ans.overclaim_risk == "high"
    assert ans.unsupported_evidence == ["production Kubernetes administration at scale"]
    assert any("fabrication guard" in n for n in ans.audit_notes)


def test_draft_voice_tell_flagged_and_retry_prefers_clean():
    # 1st draft carries "excited" (a banned tell); 2nd is clean → best-of-two keeps the clean one.
    seq = _SeqLLM([
        _draft_payload(answer="I am excited about the opportunity to contribute here."),
        _draft_payload(answer="At Acme Health, I rebuilt month-end billing for 13 partner labs."),
    ])
    ans = asyncio.run(Copilot(seq).answer(_WHY, _bank()))
    assert seq.calls == 2                                 # retried because the first had a tell
    assert "/no_think" in seq.prompts[1]                  # retry drops qwen3 thinking
    assert "excited" not in ans.long_answer.lower()
    assert not any("AI-tell" in n for n in ans.audit_notes)   # the kept draft is clean


def test_draft_voice_tell_flagged_when_both_drafts_dirty():
    seq = _SeqLLM([
        _draft_payload(answer="I am excited and passionate about this role."),
        _draft_payload(answer="I am thrilled and passionate about this role."),
    ])
    ans = asyncio.run(Copilot(seq).answer(_WHY, _bank()))
    assert ans.overclaim_risk == "high"
    assert any("AI-tell" in n for n in ans.audit_notes)


def test_draft_strips_em_dashes():
    stub = _StubLLM(payload=_draft_payload(
        answer="I rebuilt billing for 13 partner labs — it became one idempotent orchestrator."))
    ans = _ask(Copilot(stub), _WHY)
    assert "—" not in ans.long_answer and "–" not in ans.long_answer


def test_empty_draft_goes_to_review():
    ans = _ask(Copilot(_StubLLM(payload=_draft_payload(answer="   "))), _WHY)
    assert ans.verdict == "review" and ans.needs_review


def test_draft_llm_failure_goes_to_review_never_raises():
    ans = _ask(Copilot(_StubLLM(exc=RuntimeError("ollama down"))), _WHY)
    assert ans.verdict == "review" and ans.needs_review


def test_binary_screener_still_uses_verdict_path():
    # Regression: the draft route must not swallow real screeners.
    stub = _StubLLM(payload=_payload())
    ans = _ask(Copilot(stub), "Do you have hands-on experience with incremental data sync?")
    assert ans.verdict == "yes" and not ans.needs_review


def test_sensitive_open_ended_still_routes_to_policy():
    # "Why do you require sponsorship?" is open-ended AND sensitive — sensitive wins (no LLM).
    stub = _StubLLM(payload=_draft_payload())
    ans = _ask(Copilot(stub), "Why would you require visa sponsorship?")
    assert ans.source == "policy"
    assert stub.prompts == []
