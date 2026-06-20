"""Email classification (email-outcome-loop Phase A).

Mirrors tests/test_onboarding_chat.py: deterministic rules, the bounded-LLM path
(stub LLM honored, _coerce_llm projection), and the fail-safe contract (missing LLM
or a raising LLM degrades to method="none", never raises). asyncio.run for the async
classify entry point.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_applier.domain.state import OutcomeKind
from auto_applier.inbox.classify import EmailClass, classify, classify_deterministic
from auto_applier.inbox.parse import parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "inbox"


def _run(coro):
    return asyncio.run(coro)


def _email(name: str):
    return parse_message((FIXTURES / name).read_bytes())


class _DictLLM:
    """complete_json returns a fixed dict (accepts the think/num_predict kwargs)."""

    def __init__(self, payload):
        self.payload = payload

    async def complete_json(self, prompt, *, system="", think=None, num_predict=None):
        return self.payload


class _RaisingLLM:
    async def complete_json(self, *a, **k):
        raise RuntimeError("ollama down")


# --------------------------------------------------------------- deterministic rules


class TestDeterministic:

    @pytest.mark.parametrize("fixture,expected", [
        ("confirmation.eml", OutcomeKind.RESPONSE),
        ("rejection.eml", OutcomeKind.REJECTION),
        ("interview.eml", OutcomeKind.INTERVIEW),
        ("offer.eml", OutcomeKind.OFFER),
        # Real Gusto shape: no "unfortunately", only "we won't be moving forward"
        # with a curly apostrophe — must beat the _RESPONSE keyword to REJECTION.
        ("gusto_rejection.eml", OutcomeKind.REJECTION),
    ])
    def test_kind_projected(self, fixture, expected):
        cls = classify_deterministic(_email(fixture))
        assert cls is not None
        assert cls.kind is expected
        assert cls.method == "deterministic"
        assert cls.confidence >= 0.8

    def test_contraction_rejection_beats_response_keyword(self):
        """Regression for the Gusto miss: a rejection body that ALSO contains a
        response phrase ("thank you for your interest") but no "unfortunately" must
        still classify REJECTION via the "won't be moving forward" contraction
        (rejection is checked before response). Both apostrophe forms work."""
        for apostrophe in ("’", "'"):  # curly (U+2019), straight
            raw = (
                "From: Gusto <careers@gusto.com>\r\n"
                "Subject: Regarding your Application\r\n"
                "Message-ID: <c@gusto.com>\r\n"
                'Content-Type: text/plain; charset="utf-8"\r\n'
                "\r\n"
                "Thank you for your interest in the role. After reviewing your "
                f"application, we won{apostrophe}t be moving forward at this time.\r\n"
            ).encode("utf-8")
            cls = classify_deterministic(parse_message(raw))
            assert cls is not None and cls.kind is OutcomeKind.REJECTION

    def test_thanks_for_applying_next_steps_is_response_not_interview(self):
        """Live FP regression: a "thanks for applying … next steps" confirmation
        (Snowflake / Gusto / Vanta / dbt / Render shape) must classify RESPONSE, not
        INTERVIEW. Bare "next steps" is not an interview invite — verified to produce
        12/13 false INTERVIEW positives over 30 days of live mail, zero true positives.
        """
        raw = (
            "From: Snowflake Hiring Team <careers@snowflake.com>\r\n"
            "Subject: Thank you for applying to Snowflake | Senior Data Platform Architect\r\n"
            "Message-ID: <s@snowflake.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "\r\n"
            "Hi Joseph, Thank you for your interest in pursuing a career with Snowflake. "
            "We have received your application. If it meets the requirements, a recruiter "
            "will reach out about next steps.\r\n"
        ).encode("utf-8")
        cls = classify_deterministic(parse_message(raw))
        assert cls is not None and cls.kind is OutcomeKind.RESPONSE

    def test_confirmation_describing_interview_process_is_not_interview(self):
        """The dbt-Labs leak: a confirmation that DESCRIBES its interview process (so the
        body contains the word "interview") plus "next steps" must still be RESPONSE — the
        word "interview" in prose is not an invitation."""
        raw = (
            "From: dbt Labs <no-reply@us.greenhouse-mail.io>\r\n"
            "Subject: Thank you for applying to dbt Labs, Joseph!\r\n"
            "Message-ID: <d@dbt.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "\r\n"
            "Thank you for applying for the Senior Solutions Engineer role. Our interview "
            "process has three stages; we will be in touch about next steps.\r\n"
        ).encode("utf-8")
        cls = classify_deterministic(parse_message(raw))
        assert cls is not None and cls.kind is OutcomeKind.RESPONSE

    def test_marketing_your_offer_is_not_offer(self):
        """Live FP regression: financial marketing ("your offer" promos — Ally Invest,
        Capital One) must NOT classify OFFER. A real offer names the act. Verified: bare
        "your offer" produced 5 false OFFER positives over 30 days, zero true positives.
        OFFER is the highest-severity outcome, so this guard matters most."""
        raw = (
            "From: Ally Invest <offers@ally-invest.com>\r\n"
            "Subject: A 3.5% Ally Invest IRA contribution match could be yours\r\n"
            "Message-ID: <a@ally.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "\r\n"
            "Don't miss out — your offer is waiting. Open an Ally Invest IRA today.\r\n"
        ).encode("utf-8")
        cls = classify_deterministic(parse_message(raw))
        assert cls is None or cls.kind is not OutcomeKind.OFFER

    def test_payment_received_is_not_response(self):
        """Live FP regression: a utility "payment has been received" (Atmos Energy,
        Paymentus) must NOT classify RESPONSE — only an APPLICATION receipt does."""
        raw = (
            "From: Atmos Energy <no-reply@atmosenergy.com>\r\n"
            "Subject: Your Atmos Energy payment has been received\r\n"
            "Message-ID: <p@atmos.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "\r\n"
            "Your payment has been received. Thank you for being a customer.\r\n"
        ).encode("utf-8")
        cls = classify_deterministic(parse_message(raw))
        assert cls is None or cls.kind is not OutcomeKind.RESPONSE

    def test_passive_application_received_is_response(self):
        """The passive form "your application has been received" must still be RESPONSE
        after the "has been received" → "application has been received" tightening."""
        raw = (
            "From: Acme Careers <no-reply@acme.com>\r\n"
            "Subject: Application update\r\n"
            "Message-ID: <ar@acme.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "\r\n"
            "Your application has been received and is under review.\r\n"
        ).encode("utf-8")
        cls = classify_deterministic(parse_message(raw))
        assert cls is not None and cls.kind is OutcomeKind.RESPONSE

    def test_newsletter_ignored(self):
        cls = classify_deterministic(_email("newsletter.eml"))
        assert cls is not None              # confident enough to not hit the LLM
        assert cls.kind is None
        assert cls.method == "deterministic"

    def test_security_code_flag_set_without_kind(self):
        cls = classify_deterministic(_email("security_code.eml"))
        assert cls is not None
        assert cls.kind is None             # a gate email, not an outcome
        assert cls.security_code_flag is True

    def test_ambiguous_returns_none(self):
        # No status keyword → deterministic bails so the caller can try the LLM.
        raw = (
            b"From: Someone <hi@example.com>\r\n"
            b"Subject: a quick note\r\n"
            b"Message-ID: <amb@example.com>\r\n"
            b"\r\n"
            b"Just checking in about something unrelated.\r\n"
        )
        assert classify_deterministic(parse_message(raw)) is None

    def test_company_hint_from_display_name(self):
        cls = classify_deterministic(_email("confirmation.eml"))
        assert cls is not None
        # "Acme Careers" → "Acme" (the " careers" suffix is trimmed)
        assert cls.company_hint == "Acme"


# --------------------------------------------------------------- async classify


class TestClassify:

    def test_deterministic_used_first(self):
        cls = _run(classify(_email("rejection.eml")))
        assert cls.kind is OutcomeKind.REJECTION
        assert cls.method == "deterministic"

    def test_no_llm_degrades_to_safe_none(self):
        # An ambiguous email with no LLM → fail safe to review (method="none").
        raw = (
            b"From: Someone <hi@example.com>\r\n"
            b"Subject: a quick note\r\n"
            b"Message-ID: <amb2@example.com>\r\n"
            b"\r\n"
            b"Just checking in about something unrelated.\r\n"
        )
        cls = _run(classify(parse_message(raw), llm=None))
        assert cls.kind is None
        assert cls.method == "none"
        assert cls.confidence == 0.0

    def test_llm_path_coerces_kind(self):
        raw = (
            b"From: Vandelay <hr@vandelay.com>\r\n"
            b"Subject: a quick note about your candidacy\r\n"
            b"Message-ID: <llm1@vandelay.com>\r\n"
            b"\r\n"
            b"We wanted to reach out regarding your recent submission.\r\n"
        )
        llm = _DictLLM({
            "kind": "application_received",
            "company": "Vandelay Industries",
            "role": "Latex Salesperson",
            "confidence": 0.77,
        })
        cls = _run(classify(parse_message(raw), llm=llm))
        assert cls.kind is OutcomeKind.RESPONSE      # application_received → RESPONSE
        assert cls.method == "llm"
        assert cls.company_hint == "Vandelay Industries"
        assert cls.role_hint == "Latex Salesperson"
        assert cls.confidence == pytest.approx(0.77)

    def test_llm_other_maps_to_none(self):
        raw = (
            b"From: Someone <hi@example.com>\r\n"
            b"Subject: a quick note\r\n"
            b"Message-ID: <llm2@example.com>\r\n"
            b"\r\n"
            b"Unrelated content here.\r\n"
        )
        llm = _DictLLM({"kind": "other", "company": "", "role": "", "confidence": 0.9})
        cls = _run(classify(parse_message(raw), llm=llm))
        assert cls.kind is None
        assert cls.method == "llm"

    def test_llm_confidence_clamped_and_defaulted(self):
        raw = (
            b"From: Someone <hi@example.com>\r\n"
            b"Subject: a quick note\r\n"
            b"Message-ID: <llm3@example.com>\r\n"
            b"\r\n"
            b"Unrelated content.\r\n"
        )
        # garbage confidence → 0.5 default; out-of-range → clamped
        cls_missing = _run(classify(parse_message(raw), llm=_DictLLM({"kind": "rejection"})))
        assert cls_missing.confidence == 0.5
        cls_high = _run(classify(
            parse_message(raw), llm=_DictLLM({"kind": "rejection", "confidence": 5})
        ))
        assert cls_high.confidence == 1.0

    def test_raising_llm_degrades_without_raising(self):
        raw = (
            b"From: Someone <hi@example.com>\r\n"
            b"Subject: a quick note\r\n"
            b"Message-ID: <llm4@example.com>\r\n"
            b"\r\n"
            b"Unrelated content.\r\n"
        )
        cls = _run(classify(parse_message(raw), llm=_RaisingLLM()))
        assert cls.kind is None
        assert cls.method == "none"

    def test_security_flag_carried_through_llm_path(self):
        # An ambiguous-but-security-code email is caught deterministically (flag set,
        # kind None) before the LLM — but if it somehow reaches the LLM path the flag
        # still rides along. Verify the deterministic short-circuit sets it.
        cls = _run(classify(_email("security_code.eml")))
        assert cls.security_code_flag is True
        assert cls.kind is None
