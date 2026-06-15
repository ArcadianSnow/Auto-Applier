"""Application copilot — honesty-first screener-question answering (spec §8f).

Distinct from the §8b answer resolver (which fills *known* form fields from stored
answers): the copilot reasons over {fact bank + the specific job + an arbitrary
question} and returns a structured answer — verdict, paste-ready short + long
answer, reasoning, overclaim-risk flag, interview framing, skill gaps.

The design centerpiece is the **evidence audit**, the judgment-call analog of the
fabrication guard. A local model will agreeably overclaim ("Yes" to "have you led
a Debezium implementation?" when the real experience is watermark-based sync),
and the fabrication guard can't catch a wrong "Yes" — it isn't a fabricated noun.
So the prompt demands ``bank_evidence`` (the bank facts the verdict rests on) and
:func:`audit_evidence` deterministically token-matches each item against the bank
corpus. A yes/partial verdict with zero supported evidence **fails closed to
review**; a "no" never needs evidence (the guarded risk is overclaim, not
underclaim).

Sensitive questions (work-auth / sponsorship / EEO / salary) never reach the LLM —
they route through the same ``classify_sensitive`` + deterministic policies as the
resolver. Nothing the copilot produces is ever auto-submitted.

Full design rationale: ``.claude/skills/auto-applier/research/application-copilot.md``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from auto_applier.domain.dedup import normalize
from auto_applier.domain.models import Job
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import COPILOT_ANSWER, COPILOT_DRAFT
from auto_applier.resume.answer_resolver import (
    SensitiveClass,
    classify_sensitive,
    is_open_ended,
)
from auto_applier.resume.cover_autogen import _strip_ai_tells
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.generate import build_bank_facts, format_allowed_metrics
from auto_applier.resume.guard import vet_cover_letter

logger = logging.getLogger(__name__)

__all__ = [
    "Copilot",
    "CopilotAnswer",
    "DRAFT_VERDICT",
    "VERDICTS",
    "audit_evidence",
]

#: Verdicts the LLM may return for a SCREENER; anything else is malformed → review.
VERDICTS = ("yes", "no", "partial")

#: The synthetic verdict for a freeform/essay DRAFT — distinct from the yes/no/partial
#: screener verdicts. A draft ALWAYS needs human review (assisted: the human edits and
#: submits), so it is never set by the model and never flows unattended into a form.
DRAFT_VERDICT = "draft"

#: The highest-signal AI tells for a freeform answer — the "excited" family the model
#: emits even when the prompt forbids it (measured live 2026-06-15 on the Stripe bait),
#: plus the buzzwords that read as machine-written. Used to (a) prefer the cleaner of two
#: drafts and (b) flag a slip for the human. A deliberate SUBSET of the gen-cover-v4 ban
#: list (highest signal, not an exhaustive lint — the human is the final editor).
_VOICE_TELLS = re.compile(
    r"\b(excit\w*|thrill\w*|passionate|delighted|enthusias\w*|leverag\w*|synerg\w*|"
    r"results-driven|proven track record|cutting-edge|game-chang\w*|customer-centric|"
    r"world-class|best-in-class|hit the ground running)\b",
    re.IGNORECASE,
)


def _voice_violations(text: str) -> list[str]:
    """The banned AI-tell words/phrases present in a draft (deduped, lowercased)."""
    return sorted({m.group(0).lower() for m in _VOICE_TELLS.finditer(text or "")})


def _norm_risk(value: object) -> str:
    """Normalize a model-reported overclaim risk; an unparseable self-assessment is itself
    a red flag, so it defaults to ``high`` (mirrors the screener parse)."""
    risk = str(value or "none").strip().lower()
    return risk if risk in ("none", "low", "high") else "high"


def _payload_str_list(payload: dict, key: str) -> list[str]:
    val = payload.get(key, [])
    if not isinstance(val, list):
        return []
    return [str(v).strip() for v in val if str(v).strip()]

#: Minimum share of an evidence item's content tokens that must appear in a single
#: bank-corpus entry for the item to count as supported. Crude but deterministic —
#: false rejections fail SAFE (verdict drops to review; the human reads it anyway).
_SUPPORT_THRESHOLD = 0.6

#: Tokens this short carry no evidence weight ("a", "of", "to", "in", "we"...).
_MIN_TOKEN_LEN = 3

#: How much of the JD travels into the prompt when a job is attached.
_JD_EXCERPT_CHARS = 1500


@dataclass
class CopilotAnswer:
    """One audited answer. ``needs_review=True`` ⇔ the human must decide —
    either the audit failed the verdict closed or the question is sensitive
    with no bank policy to answer it."""

    question: str
    verdict: str                  # yes | no | partial | review
    short_answer: str = ""
    long_answer: str = ""
    reasoning: str = ""
    bank_evidence: list[str] = field(default_factory=list)
    overclaim_risk: str = "none"  # none | low | high
    risk_note: str = ""
    framing: str = ""
    gaps: list[str] = field(default_factory=list)
    needs_review: bool = False
    #: Deterministic-audit trail: what was checked and what failed.
    audit_notes: list[str] = field(default_factory=list)
    unsupported_evidence: list[str] = field(default_factory=list)
    #: Where the answer came from: "llm" | "policy" (deterministic sensitive path).
    source: str = "llm"


# --------------------------------------------------------------- evidence audit

def _content_tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= _MIN_TOKEN_LEN}


def _bank_corpus(bank: FactBank) -> list[str]:
    """Every bank fact an evidence item could legitimately cite, as flat strings."""
    corpus: list[str] = []
    corpus.extend(bank.skills)
    corpus.extend(bank.certifications)
    corpus.extend(bank.allowed_metrics)
    if bank.work_authorization:
        corpus.append(bank.work_authorization)
    for w in bank.work_history:
        if w.company or w.title:
            corpus.append(f"{w.title} at {w.company} {w.start} {w.end}".strip())
        corpus.extend(w.bullets)
    for e in bank.education:
        corpus.append(f"{e.degree} {e.field_of_study} {e.institution}".strip())
    return [c for c in corpus if c and normalize(c)]


def audit_evidence(
    bank: FactBank, evidence: list[str]
) -> tuple[list[str], list[str]]:
    """Split evidence items into (supported, unsupported) against the bank.

    An item is supported when, for SOME single corpus entry, either normalized
    string contains the other, or ≥ ``_SUPPORT_THRESHOLD`` of the item's content
    tokens appear in that entry. Pure + deterministic — this is the gate that
    keeps an agreeable local model from manufacturing grounds for a "yes".
    """
    corpus = _bank_corpus(bank)
    corpus_norm = [(normalize(c), _content_tokens(c)) for c in corpus]
    supported: list[str] = []
    unsupported: list[str] = []
    for item in evidence:
        item_norm = normalize(item)
        item_tokens = _content_tokens(item)
        if not item_norm:
            unsupported.append(item)
            continue
        ok = False
        for entry_norm, entry_tokens in corpus_norm:
            if item_norm in entry_norm or entry_norm in item_norm:
                ok = True
                break
            if item_tokens:
                overlap = len(item_tokens & entry_tokens) / len(item_tokens)
                if overlap >= _SUPPORT_THRESHOLD:
                    ok = True
                    break
        (supported if ok else unsupported).append(item)
    return supported, unsupported


# --------------------------------------------------------------------- copilot

class Copilot:
    """One audited answer per question. Construct with the local LLM client;
    :meth:`answer` never raises (an advisory tool must not crash a session)."""

    def __init__(self, llm: CompletionClient | None) -> None:
        self._llm = llm

    async def answer(
        self,
        question: str,
        bank: FactBank,
        *,
        job: Job | None = None,
        salary_ask: str = "",
    ) -> CopilotAnswer:
        question = (question or "").strip()
        if not question:
            return self._review(question, "empty question")

        # Sensitive questions are policy, never the LLM (spec §8d posture).
        sensitivity = classify_sensitive(question)
        if sensitivity is not SensitiveClass.NONE:
            return self._answer_sensitive(question, bank, sensitivity, salary_ask)

        if self._llm is None:
            return self._review(question, "no LLM client (run with Ollama up, or omit --no-llm)")

        # Open-ended / freeform fields (why-company, "describe a time…", textareas) are NOT
        # yes/no screeners — forcing them through the verdict prompt produced a meaningless
        # verdict and AI-tell prose (measured 2026-06-15: "Why Stripe?" → verdict="no",
        # "I'm excited about the opportunity" ×2). Route them to the freeform DRAFT path: a
        # grounded, voice-clean, fabrication-guarded answer flagged for the human to edit and
        # submit (assisted). The verdict machinery below stays for true binary screeners.
        if is_open_ended(question):
            return await self._draft_freeform(question, bank, job=job)

        prompt = COPILOT_ANSWER.format(
            bank_facts=build_bank_facts(bank),
            allowed_metrics=format_allowed_metrics(bank),
            job_context=self._job_context(job),
            question=question,
        )
        try:
            payload = await self._llm.complete_json(prompt, system=COPILOT_ANSWER.system)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all (CompletionError, HTTP, parse)
            logger.warning("Copilot LLM call failed: %s", exc)
            return self._review(question, f"LLM unavailable/malformed: {exc}")
        if not isinstance(payload, dict):
            return self._review(question, "LLM reply was not a JSON object")

        answer = self._parse(question, payload)
        if answer.needs_review:
            return answer
        return self._audit(bank, answer)

    # ---- freeform draft (open-ended/essay; BUILD 6) ----------------------

    async def _draft_freeform(
        self, question: str, bank: FactBank, *, job: Job | None = None
    ) -> CopilotAnswer:
        """Draft an open-ended/freeform answer (no verdict). ALWAYS flagged
        ``needs_review`` — a freeform draft is the human's to edit and submit (assisted),
        never auto-filled. Grounded in the bank, voice-cleaned, and honesty-flagged
        (evidence audit + fabrication guard + voice check) but NEVER blanked: a flagged
        draft the human can fix beats a blank he must write from scratch (the assisted
        contract — [[feedback_assisted_means_ai_drafts_freeform]])."""
        # Best-of-two, mirroring cover_autogen.generate_one: qwen3 emits banned tells even
        # under the prompt ban (measured), so generate, and if the draft carries a voice
        # tell, regenerate once with thinking off and keep the cleaner of the two drafts.
        best: tuple[int, str, dict] | None = None  # (violation_count, cleaned_answer, payload)
        last_exc: Exception | None = None
        for attempt in range(2):
            prompt = COPILOT_DRAFT.format(
                bank_facts=build_bank_facts(bank),
                allowed_metrics=format_allowed_metrics(bank),
                job_context=self._job_context(job),
                question=question,
            )
            if attempt > 0:
                prompt += "\n\n/no_think"  # qwen3: drop chain-of-thought on retry (cover-gen idiom)
            try:
                payload = await self._llm.complete_json(prompt, system=COPILOT_DRAFT.system)
            except Exception as exc:  # noqa: BLE001 — transport/parse; retry once, else review
                last_exc = exc
                continue
            if not isinstance(payload, dict):
                continue
            text = _strip_ai_tells(str(payload.get("answer", "")).strip())
            if not text:
                continue
            violations = _voice_violations(text)
            if best is None or len(violations) < best[0]:
                best = (len(violations), text, payload)
            if not violations:
                break

        if best is None:
            note = (f"LLM unavailable/malformed: {last_exc}" if last_exc is not None
                    else "model returned no usable draft after 2 attempts")
            return self._review(question, note)

        _, text, payload = best
        draft = CopilotAnswer(
            question=question,
            verdict=DRAFT_VERDICT,
            long_answer=text,
            reasoning=str(payload.get("risk_note", "")).strip(),
            bank_evidence=_payload_str_list(payload, "bank_evidence"),
            overclaim_risk=_norm_risk(payload.get("overclaim_risk")),
            risk_note=str(payload.get("risk_note", "")).strip(),
            gaps=_payload_str_list(payload, "gaps"),
            needs_review=True,   # a freeform draft is ALWAYS the human's to review + submit
            source="llm",
        )
        draft.audit_notes.append(
            "freeform DRAFT — review and edit before submitting; you are the submit gate (assisted)"
        )
        return self._flag_draft(bank, draft, violations=_voice_violations(text))

    def _flag_draft(
        self, bank: FactBank, draft: CopilotAnswer, *, violations: list[str]
    ) -> CopilotAnswer:
        """Attach honesty flags to a freeform draft WITHOUT ever blanking it (a flagged
        draft beats a blank in assisted mode). Three checks: the bank-evidence audit, the
        fabrication guard on the prose, and the deterministic voice-tell check. Any hit
        raises ``overclaim_risk`` to high and notes what to fix; the draft is still
        returned for the human to edit."""
        supported, unsupported = audit_evidence(bank, draft.bank_evidence)
        draft.unsupported_evidence = unsupported
        draft.bank_evidence = supported + unsupported  # keep both, flagged below
        flagged = bool(unsupported)
        if unsupported:
            draft.audit_notes.append(
                f"{len(unsupported)} cited fact(s) not found in your bank: "
                + "; ".join(unsupported[:3])
            )
        # The same tech-claim fabrication guard the cover letter uses. On a freeform draft
        # we FLAG, never skip — the human edits the unsupported claim out.
        try:
            guard = vet_cover_letter(draft.long_answer, bank)
        except Exception as exc:  # noqa: BLE001 — guard must never crash the advisory tool
            logger.warning("Copilot draft guard failed: %s", exc)
            guard = None
        if guard is not None and not guard.ok:
            flagged = True
            terms = ", ".join(sorted({f.claim for f in guard.findings})) or "unsupported claim"
            draft.audit_notes.append(
                f"fabrication guard flagged unsupported claim(s): {terms} — verify or remove"
            )
        if violations:
            flagged = True
            draft.audit_notes.append(
                "AI-tell wording slipped through: " + ", ".join(violations)
                + " — reword before sending"
            )
        if flagged:
            draft.overclaim_risk = "high"
        return draft

    # ---- sensitive (deterministic policy; mirrors the §8b resolver) -------

    def _answer_sensitive(
        self,
        question: str,
        bank: FactBank,
        sensitivity: SensitiveClass,
        salary_ask: str,
    ) -> CopilotAnswer:
        if sensitivity is SensitiveClass.SPONSORSHIP:
            if bank.requires_sponsorship is None:
                return self._review(
                    question,
                    "sponsorship status not captured in the fact bank — no silent default",
                    source="policy",
                )
            value = "Yes" if bank.requires_sponsorship else "No"
            return CopilotAnswer(
                question=question, verdict=value.lower(), short_answer=value,
                long_answer=value,
                reasoning="sponsorship from the fact bank (explicit, never defaulted)",
                source="policy",
            )
        if sensitivity is SensitiveClass.WORK_AUTHORIZATION:
            if not bank.work_authorization:
                return self._review(
                    question,
                    "work authorization not captured in the fact bank — no silent default",
                    source="policy",
                )
            return CopilotAnswer(
                question=question, verdict="yes",
                short_answer=bank.work_authorization,
                long_answer=bank.work_authorization,
                reasoning="work authorization from the fact bank (explicit)",
                source="policy",
            )
        if sensitivity is SensitiveClass.SALARY:
            if not salary_ask:
                return self._review(
                    question,
                    "no salary ask available — configure salary.floor/ceiling "
                    "(the §8d module computes the per-job ask)",
                    source="policy",
                )
            return CopilotAnswer(
                question=question, verdict="yes", short_answer=salary_ask,
                long_answer=salary_ask,
                reasoning="salary ask from the §8d salary module (posted range + your config)",
                source="policy",
            )
        # EEO — user self-ID when the label matches a captured key, else prefer-not.
        value = "Prefer not to answer"
        lowered = question.lower()
        for key, val in bank.eeo.items():
            if key.lower() in lowered and val:
                value = val
                break
        return CopilotAnswer(
            question=question, verdict="yes", short_answer=value, long_answer=value,
            reasoning="EEO: user self-ID or prefer-not-to-answer; never inferred, never mirrored",
            source="policy",
        )

    # ---- parse + audit -----------------------------------------------------

    def _parse(self, question: str, payload: dict) -> CopilotAnswer:
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict not in VERDICTS:
            return self._review(question, f"malformed verdict {verdict!r}")
        risk = str(payload.get("overclaim_risk", "none")).strip().lower()
        if risk not in ("none", "low", "high"):
            risk = "high"  # an unparseable self-assessment is itself a red flag

        def _str_list(key: str) -> list[str]:
            val = payload.get(key, [])
            if not isinstance(val, list):
                return []
            return [str(v).strip() for v in val if str(v).strip()]

        return CopilotAnswer(
            question=question,
            verdict=verdict,
            short_answer=str(payload.get("short_answer", "")).strip(),
            long_answer=str(payload.get("long_answer", "")).strip(),
            reasoning=str(payload.get("reasoning", "")).strip(),
            bank_evidence=_str_list("bank_evidence"),
            overclaim_risk=risk,
            risk_note=str(payload.get("risk_note", "")).strip(),
            framing=str(payload.get("framing", "")).strip(),
            gaps=_str_list("gaps"),
        )

    def _audit(self, bank: FactBank, answer: CopilotAnswer) -> CopilotAnswer:
        """The deterministic honesty gate. A yes/partial with no supported
        evidence fails closed; unsupported items raise the risk flag."""
        supported, unsupported = audit_evidence(bank, answer.bank_evidence)
        answer.unsupported_evidence = unsupported
        if unsupported:
            answer.overclaim_risk = "high"
            answer.audit_notes.append(
                f"{len(unsupported)} evidence item(s) not found in the fact bank: "
                + "; ".join(unsupported[:3])
            )
        if answer.verdict in ("yes", "partial") and not supported:
            answer.audit_notes.append(
                f"verdict '{answer.verdict}' had no bank-supported evidence — "
                "failed closed to review (the model may be overclaiming)"
            )
            answer.verdict = "review"
            answer.needs_review = True
        answer.bank_evidence = supported + unsupported  # keep both, flagged above
        return answer

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _job_context(job: Job | None) -> str:
        if job is None:
            return ""
        jd = (job.description or "").strip()
        if len(jd) > _JD_EXCERPT_CHARS:
            jd = jd[:_JD_EXCERPT_CHARS] + " [...]"
        lines = [
            "Job context:",
            f"  Company: {job.company}",
            f"  Title: {job.title}",
        ]
        if job.location:
            lines.append(f"  Location: {job.location}")
        if jd:
            lines.append(f"  Description excerpt:\n{jd}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _review(question: str, note: str, *, source: str = "llm") -> CopilotAnswer:
        return CopilotAnswer(
            question=question,
            verdict="review",
            needs_review=True,
            audit_notes=[note],
            source=source,
        )
