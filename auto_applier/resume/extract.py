"""Résumé → fact bank extraction (spec §6b; research/future-directions.md Direction 1, Phase A).

Turns an uploaded/pasted résumé into a structured :class:`FactBank` so a new user does not
hand-type their whole work history into onboarding. Two stages:

  1. :func:`extract_text_from_file` — PDF (pdfplumber) / DOCX (python-docx) / TXT → plain text.
     Both parsers are already project deps; no new dependency.
  2. :func:`extract_factbank` — local LLM (``EXTRACT_FACTBANK`` prompt, JSON out) → FactBank.

**Faithfulness is the contract:** the extracted bank becomes the fabrication guard's source of
truth, so the prompt extracts ONLY what the résumé states and the result is COERCED to the known
fact-bank shape (stray keys dropped, lists normalized) before parsing. The candidate-entered
fields a résumé can't supply — work authorization, sponsorship, EEO, relocation — are NEVER
touched here; :func:`merge_extracted` preserves them when refreshing an existing bank.

The user always REVIEWS the result before it's trusted (the onboarding wizard / the ``--save``
gate); this module only produces the draft.
"""

from __future__ import annotations

from pathlib import Path

from auto_applier.llm.prompts import EXTRACT_FACTBANK
from auto_applier.resume.factbank import FactBank

__all__ = [
    "extract_factbank",
    "extract_from_file",
    "extract_text_from_bytes",
    "extract_text_from_file",
    "merge_extracted",
]

#: Guard against a pathological multi-hundred-page PDF; résumés are short (~1-3 pages).
_MAX_RESUME_CHARS = 24000


def extract_text_from_file(path: str | Path) -> str:
    """Plain text from a résumé file on disk. Supports ``.pdf`` / ``.docx`` / ``.txt``; raises
    :class:`ValueError` on an unsupported extension (incl. the legacy binary ``.doc``). Thin
    wrapper over :func:`extract_text_from_bytes` (the web-upload path shares the same logic)."""
    p = Path(path)
    return extract_text_from_bytes(p.read_bytes(), p.name)


def extract_text_from_bytes(data: bytes, filename: str) -> str:
    """Plain text from in-memory résumé bytes (the web-upload path), dispatched by ``filename``'s
    extension: ``.pdf`` (pdfplumber), ``.docx`` (python-docx), ``.txt``/``.md`` (decode). Raises
    :class:`ValueError` on an unsupported extension."""
    import io

    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages).strip()
    if ext == ".docx":
        import docx

        doc = docx.Document(io.BytesIO(data))
        return "\n".join(par.text for par in doc.paragraphs).strip()
    if ext in (".txt", ".md", ".text"):
        return data.decode("utf-8", errors="replace").strip()
    raise ValueError(
        f"unsupported résumé format '{ext}' (use .pdf, .docx, or .txt; legacy .doc is not "
        "supported — re-save it as .docx or .pdf first)"
    )


def _coerce_factbank_dict(raw: dict) -> dict:
    """Whitelist the LLM's JSON into exactly the :meth:`FactBank.from_dict` shape — drop stray
    keys, coerce to ``str``, normalize lists — so a noisy model response can neither blow up
    construction nor smuggle an unknown field into the guard's source of truth. Sensitive /
    user-entered fields (work_authorization, sponsorship, eeo, relocation) are intentionally
    absent here (a résumé doesn't supply them)."""
    c = raw.get("contact") or {}
    contact = {k: str(c.get(k, "") or "") for k in ("name", "email", "phone", "location")}
    contact["links"] = {
        str(k): str(v) for k, v in (c.get("links") or {}).items() if k and v
    }

    def _work(w: dict) -> dict:
        return {
            "company": str(w.get("company", "") or ""),
            "title": str(w.get("title", "") or ""),
            "start": str(w.get("start", "") or ""),
            "end": str(w.get("end", "") or ""),
            "bullets": [
                str(b).strip() for b in (w.get("bullets") or [])
                if b is not None and str(b).strip()
            ],
        }

    def _edu(e: dict) -> dict:
        return {
            k: str(e.get(k, "") or "")
            for k in ("institution", "degree", "field_of_study", "start", "end")
        }

    def _strlist(key: str) -> list[str]:
        return [
            str(x).strip() for x in (raw.get(key) or [])
            if x is not None and str(x).strip()
        ]

    return {
        "contact": contact,
        "work_history": [
            _work(w) for w in (raw.get("work_history") or [])
            if isinstance(w, dict) and (w.get("company") or w.get("title"))
        ],
        "education": [
            _edu(e) for e in (raw.get("education") or [])
            if isinstance(e, dict) and (e.get("institution") or e.get("degree"))
        ],
        "skills": _strlist("skills"),
        "certifications": _strlist("certifications"),
        "allowed_metrics": _strlist("allowed_metrics"),
    }


async def extract_factbank(resume_text: str, llm_client) -> FactBank:
    """Extract a :class:`FactBank` from résumé text via the local LLM. Empty/blank text → an
    empty FactBank (no LLM call). Faithful by construction: only résumé-derived fields are
    populated; work-auth/EEO/relocation are left at their defaults for the user to set."""
    text = (resume_text or "").strip()
    if not text:
        return FactBank()
    # think=False: extraction is a structured copy-out task that needs no chain-of-thought, and
    # leaving thinking ON lets qwen3 run a long (sometimes degenerate) trace that blows the read
    # timeout. The API think param is used (NOT the in-prompt "/no_think" token, which was
    # observed to randomly drop work-history roles, 2026-06-16). num_predict bounds the output so
    # a degenerate loop can't run to the timeout; a full résumé's fact bank fits well under it.
    prompt = EXTRACT_FACTBANK.format(resume_text=text[:_MAX_RESUME_CHARS])
    raw = await llm_client.complete_json(
        prompt, system=EXTRACT_FACTBANK.system, think=False, num_predict=4096,
    )
    return FactBank.from_dict(_coerce_factbank_dict(raw or {}))


async def extract_from_file(path: str | Path, llm_client) -> FactBank:
    """Convenience: :func:`extract_text_from_file` → :func:`extract_factbank`."""
    return await extract_factbank(extract_text_from_file(path), llm_client)


def merge_extracted(existing: FactBank, extracted: FactBank) -> FactBank:
    """Refresh an existing bank with newly extracted résumé fields WITHOUT destroying the
    user-entered fields a résumé can't supply.

    Résumé-derived fields (contact, work_history, education, skills, certifications,
    allowed_metrics) come from ``extracted``; the candidate's explicit work_authorization /
    requires_sponsorship / eeo / relocation are PRESERVED from ``existing`` (the apply path
    fails closed on these, so they must never be silently overwritten with extraction
    defaults). Used by the ``--save`` path against the current bank — an empty
    :class:`FactBank` for a brand-new user, which yields exactly the extracted résumé fields.
    """
    return FactBank(
        contact=extracted.contact,
        work_history=extracted.work_history,
        education=extracted.education,
        skills=extracted.skills,
        certifications=extracted.certifications,
        allowed_metrics=extracted.allowed_metrics,
        work_authorization=existing.work_authorization,
        requires_sponsorship=existing.requires_sponsorship,
        eeo=dict(existing.eeo),
        relocation=dict(existing.relocation),
    )
