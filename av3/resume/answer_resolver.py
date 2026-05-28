"""Two-tier answer resolver for ATS custom questions (spec §8b, §8d).

The driver discovers ~20 custom questions per Greenhouse form on average; until those
are answered, even ``BROWSER_ASSISTED`` makes the human re-type everything. This module
is the answer pipeline:

  ┌───────────────────────────────────────────────────────────────────────────┐
  │  resolve(question, fact_bank) ->                                          │
  │                                                                           │
  │  Tier 0 — Sensitive-field policy (deterministic, §8d):                    │
  │    EEO / demographics  -> user self-ID OR "prefer not to answer"          │
  │    Work auth / sponsor -> explicit fact-bank field; no silent default     │
  │    Salary              -> v3.0 simple fill from user config range         │
  │  Tier 1 — Semantic match against AnswerRepo (embedding, cosine ≥ τ):     │
  │    high-similarity hit -> stored answer (source=user|inferred)            │
  │  Tier 2 — Confidence-gated LLM backup (only when bank misses):           │
  │    confidence ≥ θ -> propose answer, flag as 'inferred'                   │
  │    confidence <  θ -> bail to REVIEW                                      │
  └───────────────────────────────────────────────────────────────────────────┘

Reliability invariants this honors:
  * Fail closed. Any ambiguous / unanswerable required question bails to REVIEW; the
    apply driver downgrades the outcome to ``ASSISTED_PENDING`` (never auto-submits a
    form with a wrong required answer).
  * No silent defaulting on sensitive fields. v2's "authorized = Yes" assumption is
    explicitly retired here ([[project_us_default_assumption]]); work-auth answers come
    from the fact bank or REVIEW.
  * EEO values stay local. They flow into the answer, but ``Resolution.flag`` carries
    the sensitive-class marker so telemetry can refuse to mirror them (spec §9).
  * Inferred answers are flagged (``source='inferred'``) so the §8e feedback loop can
    promote frequently-correct inferences to canonical bank entries.

Why a Resolution dataclass instead of just ``str | None``: the driver needs to know
*which questions need a human* to decide whether to submit, *which were inferred* (worth
flagging for review-after-submit), and *which were sensitive* (telemetry mirror policy).
A bare string discards all three signals.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

from av3.domain.models import Answer, utcnow_iso
from av3.llm.embed import EmbeddingClient, bytes_to_vec, cosine, vec_to_bytes
from av3.resume.factbank import FactBank
from av3.sources.browser.apply_base import CustomQuestion

__all__ = [
    "AnswerResolver",
    "Resolution",
    "ResolutionSource",
    "SensitiveClass",
    "classify_sensitive",
]


# ----------------------------------------------------------------- result types

class ResolutionSource(str, Enum):
    """Where the answer came from. Drives the §8e feedback loop + telemetry policy."""

    BANK = "bank"               # exact or semantic match against the answer repo
    INFERRED = "inferred"       # LLM tier-3 answer, confidence-gated, flagged for review
    SENSITIVE_DEFAULT = "sensitive_default"  # EEO blank -> "prefer not to answer"
    FACT_BANK = "fact_bank"     # work-auth pulled from the fact bank directly
    USER_CONFIG = "user_config"  # e.g. salary expectation from user_config.json
    REVIEW = "review"           # bailed; human must answer


class SensitiveClass(str, Enum):
    """Sensitive-field classifications drive policy (spec §8d) AND telemetry scrubbing."""

    NONE = "none"
    EEO = "eeo"                   # race / gender / veteran / disability / orientation
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    SALARY = "salary"


@dataclass
class Resolution:
    """The output of :meth:`AnswerResolver.resolve`. ``value=None`` ⇔ REVIEW bail."""

    question: CustomQuestion
    value: str | None
    source: ResolutionSource
    confidence: float = 1.0    # 1.0 = bank/policy; <1 only for inferred
    sensitive: SensitiveClass = SensitiveClass.NONE
    needs_review: bool = False
    note: str = ""

    @property
    def fills(self) -> bool:
        return self.value is not None and not self.needs_review


# ------------------------------------------------------ sensitive-field classifier

# These patterns are deliberate broad-strokes; the cost of a false positive (treating
# a non-EEO question as EEO and answering "prefer not to answer") is low — the driver
# still REVIEWs anything required and unsupported. The cost of a false negative on a
# sensitive field is the v2 US-yes bug, which we're correcting here.

_EEO_PATTERNS = [
    r"\b(gender|sex)\b",
    r"\brace\b",
    r"\bethnic(ity)?\b",
    r"\bhispanic|latin[xao]\b",
    r"\bveteran\b",
    r"\bdisab(led|ility|ilities)\b",
    r"\bsexual orientation\b",
    r"\bLGBT[Q]?[I]?[A]?\+?\b",
    r"\btransgender\b",
    r"\bpronouns?\b",
]
_WORK_AUTH_PATTERNS = [
    r"\bauthor(ized|ization|ised|isation)\b.*\b(work|employment)\b",
    r"\b(work|employment).*\bauthor(ized|ization|ised|isation)\b",
    r"\bright to work\b",
    r"\blegally\b.*\b(work|employ)\b",
    r"\b(work|employment)\s+permit\b",
    r"\bcitizen(ship)?\s+status\b",
]
_SPONSORSHIP_PATTERNS = [
    r"\bsponsor(ship)?\b",
    r"\bvisa\b",
    r"\bH-?1B\b",
    r"\bgreen\s+card\b",
    r"\bimmigration\b",
]
_SALARY_PATTERNS = [
    r"\bsalary\b",
    r"\bcompensation\b",
    r"\bdesired pay\b",
    r"\b(expected|expectation).*\b(pay|salary|compensation)\b",
    r"\bpay range\b",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def classify_sensitive(label: str) -> SensitiveClass:
    """Classify a question label by sensitivity (spec §8d).

    Order matters: SPONSORSHIP wins over WORK_AUTHORIZATION because "do you require
    sponsorship?" matches both patterns but the answer comes from a different
    fact-bank field. Salary is checked before EEO because compensation language
    occasionally overlaps with demographic surveys.
    """
    s = label or ""
    if _matches_any(s, _SPONSORSHIP_PATTERNS):
        return SensitiveClass.SPONSORSHIP
    if _matches_any(s, _WORK_AUTH_PATTERNS):
        return SensitiveClass.WORK_AUTHORIZATION
    if _matches_any(s, _SALARY_PATTERNS):
        return SensitiveClass.SALARY
    if _matches_any(s, _EEO_PATTERNS):
        return SensitiveClass.EEO
    return SensitiveClass.NONE


# --------------------------------------------------------------------- resolver

_LLM_SYSTEM = (
    "You are answering one question on a job application form on the candidate's "
    "behalf. The candidate's facts are below. Reply with JSON only: "
    '{"answer": "<string>", "confidence": <0..1>}. '
    "Use ONLY the facts provided. If the facts do not support a confident answer, "
    "return confidence below 0.5."
)


@dataclass
class _ResolverConfig:
    """Tuning knobs — surfaced so a settings change can re-tune without code edits."""

    semantic_match_threshold: float = 0.78
    llm_confidence_threshold: float = 0.7


class AnswerResolver:
    """Two-tier resolver. Construct once per apply run; call :meth:`resolve` per Q.

    The class is async because the embedding + LLM calls are; sensitive-field policy
    paths are sync internally but the public surface stays async-uniform.
    """

    def __init__(
        self,
        fact_bank: FactBank,
        answer_repo,
        embed_client: EmbeddingClient | None = None,
        llm_client=None,
        salary_expectation: str = "",
        config: _ResolverConfig | None = None,
    ):
        self.fact_bank = fact_bank
        self.answer_repo = answer_repo
        self.embed_client = embed_client
        self.llm_client = llm_client
        self.salary_expectation = salary_expectation
        self.config = config or _ResolverConfig()

    # ---- public ----------------------------------------------------------

    async def resolve(self, question: CustomQuestion) -> Resolution:
        sensitivity = classify_sensitive(question.label)
        if sensitivity is not SensitiveClass.NONE:
            return self._resolve_sensitive(question, sensitivity)
        bank_hit = await self._resolve_from_bank(question)
        if bank_hit is not None:
            return bank_hit
        if self.llm_client is not None:
            llm_hit = await self._resolve_via_llm(question)
            if llm_hit is not None:
                return llm_hit
        return self._review(question, note="no bank match and LLM unavailable/low-confidence")

    async def resolve_all(self, questions: list[CustomQuestion]) -> list[Resolution]:
        """Resolve a list, preserving order. Driver-friendly batch entry point."""
        out: list[Resolution] = []
        for q in questions:
            out.append(await self.resolve(q))
        return out

    # ---- sensitive (spec §8d) -------------------------------------------

    def _resolve_sensitive(
        self, question: CustomQuestion, sensitivity: SensitiveClass
    ) -> Resolution:
        bank = self.fact_bank
        if sensitivity is SensitiveClass.EEO:
            value = self._pick_eeo(question.label, bank.eeo)
            return Resolution(
                question=question,
                value=value,
                source=ResolutionSource.SENSITIVE_DEFAULT if value == "Prefer not to answer"
                else ResolutionSource.BANK,
                sensitive=SensitiveClass.EEO,
                note="EEO: user self-ID or prefer-not-to-answer",
            )
        if sensitivity is SensitiveClass.WORK_AUTHORIZATION:
            if not bank.work_authorization:
                return self._review(
                    question,
                    note="work authorization not captured in fact bank — no silent default",
                    sensitivity=SensitiveClass.WORK_AUTHORIZATION,
                )
            return Resolution(
                question=question,
                value=bank.work_authorization,
                source=ResolutionSource.FACT_BANK,
                sensitive=SensitiveClass.WORK_AUTHORIZATION,
                note="work authorization from fact bank (explicit, never defaulted)",
            )
        if sensitivity is SensitiveClass.SPONSORSHIP:
            if bank.requires_sponsorship is None:
                return self._review(
                    question,
                    note="sponsorship status not captured — no silent default",
                    sensitivity=SensitiveClass.SPONSORSHIP,
                )
            return Resolution(
                question=question,
                value="Yes" if bank.requires_sponsorship else "No",
                source=ResolutionSource.FACT_BANK,
                sensitive=SensitiveClass.SPONSORSHIP,
                note="sponsorship from fact bank (explicit)",
            )
        # SALARY — v3.0 minimum: fill from user config; the intelligent comp module is v3.1.
        if not self.salary_expectation:
            return self._review(
                question,
                note="no salary expectation configured — v3.0 fills from user_config",
                sensitivity=SensitiveClass.SALARY,
            )
        return Resolution(
            question=question,
            value=self.salary_expectation,
            source=ResolutionSource.USER_CONFIG,
            sensitive=SensitiveClass.SALARY,
            note="salary expectation from user config (v3.0); intelligence layer is v3.1",
        )

    @staticmethod
    def _pick_eeo(label: str, eeo: dict[str, str]) -> str:
        """Match an EEO label to a self-ID key. Blank or no match -> prefer-not."""
        lowered = label.lower()
        for key, val in eeo.items():
            if key.lower() in lowered:
                return val or "Prefer not to answer"
        return "Prefer not to answer"

    # ---- semantic bank match (Tier 1) -----------------------------------

    async def _resolve_from_bank(self, question: CustomQuestion) -> Resolution | None:
        # Fast path: exact question text match (common when v2's flat answers.json was
        # seeded as-is). Skips the embedding round-trip.
        exact = self.answer_repo.get(question.label)
        if exact is not None and exact.answer:
            return Resolution(
                question=question,
                value=exact.answer,
                source=ResolutionSource.BANK,
                note="exact question-text match",
            )
        if self.embed_client is None:
            return None
        # Semantic match: embed the question, cosine vs each stored vector. Bank is
        # bounded (dozens-low-hundreds) so a Python loop is fine — no ANN index.
        try:
            q_vec = await self.embed_client.embed(question.label)
        except Exception as exc:  # noqa: BLE001 — embed failure -> drop to next tier
            return None
        best_score = 0.0
        best: Answer | None = None
        for stored in self.answer_repo.all():
            v = bytes_to_vec(stored.embedding)
            score = cosine(q_vec, v)
            if score > best_score:
                best_score = score
                best = stored
        if best is None or best_score < self.config.semantic_match_threshold:
            return None
        return Resolution(
            question=question,
            value=best.answer,
            source=ResolutionSource.BANK,
            confidence=best_score,
            note=f"semantic match: '{best.question}' (cosine={best_score:.2f})",
        )

    # ---- LLM backup (Tier 2) --------------------------------------------

    async def _resolve_via_llm(self, question: CustomQuestion) -> Resolution | None:
        prompt = self._build_llm_prompt(question)
        try:
            reply = await self.llm_client.complete_json(prompt, system=_LLM_SYSTEM)
        except Exception:  # noqa: BLE001 — LLM unreachable -> caller bails to REVIEW
            return None
        answer = reply.get("answer")
        try:
            confidence = float(reply.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        if not isinstance(answer, (str, int, float)) or confidence < self.config.llm_confidence_threshold:
            return None
        return Resolution(
            question=question,
            value=str(answer),
            source=ResolutionSource.INFERRED,
            confidence=confidence,
            note=f"LLM-inferred (conf={confidence:.2f}); flag for §8e feedback loop",
        )

    def _build_llm_prompt(self, question: CustomQuestion) -> str:
        bank = self.fact_bank
        facts = {
            "name": bank.contact.name,
            "location": bank.contact.location,
            "work_authorization": bank.work_authorization,
            "requires_sponsorship": bank.requires_sponsorship,
            "skills": bank.skills,
            "work_history": [
                {"company": w.company, "title": w.title, "start": w.start, "end": w.end}
                for w in bank.work_history
            ],
            "education": [
                {"institution": e.institution, "degree": e.degree} for e in bank.education
            ],
            "certifications": bank.certifications,
        }
        return (
            "Question label on form: " + (question.label or "(no label)") + "\n"
            "Required: " + ("yes" if question.required else "no") + "\n"
            "Candidate facts (JSON):\n" + json.dumps(facts, default=str)
        )

    # ---- review bail ----------------------------------------------------

    @staticmethod
    def _review(
        question: CustomQuestion,
        *,
        note: str,
        sensitivity: SensitiveClass = SensitiveClass.NONE,
    ) -> Resolution:
        return Resolution(
            question=question,
            value=None,
            source=ResolutionSource.REVIEW,
            confidence=0.0,
            sensitive=sensitivity,
            needs_review=True,
            note=note,
        )


# ---- helper used by the answer-bank seeder + the resolve-time learn loop ----

async def store_answer(
    answer_repo,
    embed_client: EmbeddingClient | None,
    question: str,
    answer: str,
    source: str = "user",
) -> None:
    """UPSERT an answer with its embedding (if a client is available).

    Used at:
      * onboarding seed of v2's flat answers.json (source='user')
      * resolver feedback loop for inferred answers the user later confirms (source='inferred')
    """
    embedding: bytes | None = None
    if embed_client is not None:
        try:
            vec = await embed_client.embed(question)
            embedding = vec_to_bytes(vec)
        except Exception:  # noqa: BLE001 — store without vector; resolver still hits via exact match
            embedding = None
    answer_repo.upsert(
        Answer(
            question=question,
            answer=answer,
            source=source,
            embedding=embedding,
            updated_at=utcnow_iso(),
        )
    )
