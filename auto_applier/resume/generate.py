"""Per-job résumé + cover letter generation from the master fact bank (spec §6b, §7 #6).

This is the LLM-orchestration layer of the optimize+Strict gate. It:

  1. Takes the fact bank + a job's description and produces a *structured*
     :class:`GeneratedResume` (the same shape the deterministic fabrication guard
     in ``auto_applier.resume.guard`` consumes). The structure is what makes the guard's
     allow-list checks possible — guard runs on fields, not free text.
  2. Produces a plain-text cover letter body (no salutation/signature — those are
     wrapped by the apply driver per spec §6b).

Both calls go through the versioned prompts in :mod:`auto_applier.llm.prompts`; the prompt
version is stamped on the persisted artifact filename trail (via
:func:`generated_resume_path` / :func:`generated_cover_letter_path`'s caller in the
worker) so the eval harness ((7/M)) can pin regressions to a specific prompt revision.

**Defensive parsing.** The LLM is told to emit a strict JSON shape, but small local
models drift. :func:`parse_generated_resume` accepts the documented shape and
coerces what it can — missing arrays default to ``[]``, missing strings to ``""``,
non-dict work entries are dropped. The guard is what enforces *fact* correctness;
this layer only enforces *structural* correctness so the guard has something to
read. A truly malformed reply (non-dict top level) raises so the worker fail-closes
that job to REVIEW (the optimize worker's correct response — never trust an
ungrounded résumé into QUEUED_APPLY).

**Path helpers** live here so optimize worker AND apply worker derive the same
canonical paths from a ``job.id``. No DB column added: the file's existence IS
the durable contract that says "this job has been optimized." The apply worker
reads these paths, attaches the files, and writes them onto the ``Application``
row at submit time (where ``cover_letter_path`` / ``generated_resume_path`` already
live in the schema, spec §4).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from auto_applier.config.settings import Settings
from auto_applier.llm.complete import CompletionClient
from auto_applier.llm.prompts import GENERATE_COVER_LETTER, GENERATE_RESUME
from auto_applier.resume.factbank import FactBank
from auto_applier.resume.guard import GeneratedResume, GenEducation, GenWorkEntry

__all__ = [
    "DEFAULT_COVER_TARGET_WORDS",
    "ResumeGenerator",
    "CoverLetterGenerator",
    "archive_cover_letter",
    "assign_cover_letter",
    "build_bank_facts",
    "existing_job_cover",
    "format_allowed_metrics",
    "generated_cover_letter_path",
    "generated_resume_path",
    "job_cover_upload_path",
    "parse_cover_letter",
    "parse_generated_resume",
]

#: File extensions an ATS cover-letter upload accepts (Greenhouse's #cover_letter accept
#: list). A hand-authored letter keeps its extension; .docx is the usual real content.
_COVER_LETTER_EXTS = (".docx", ".pdf", ".doc", ".txt", ".rtf")

#: The generic basename the cover letter is uploaded under. Playwright sends a file by its
#: BASENAME, so this is what the ATS sees — a per-posting filename
#: (``CoverLetter_Tailscale_SE_Commercial.docx``) is a mass-apply fingerprint; this is what a
#: normal applicant uploads. Per-job identity lives in the folder path, not the name (§8c).
_COVER_UPLOAD_STEM = "Cover Letter"


#: Default cover-letter target length (spec §6b: concise & tailored, ~150-250 words).
#: Knob lives here, not in :class:`Settings`, until §8e data motivates a config knob —
#: configurable-but-unmeasured length would just be a guess. v3.0 ships fixed; the
#: outcome feedback loop (v3.1) revises this from response-rate data.
DEFAULT_COVER_TARGET_WORDS = 200


# --------------------------------------------------------------- canonical paths

def generated_resume_path(settings: Settings, job_id: str) -> Path:
    """Where the per-job tailored résumé PDF lives.

    Deterministic from ``job.id`` so the optimize worker writes and the apply
    worker reads without any DB hand-off. ``settings.artifacts_dir / "generated"``
    is the parent — created by the renderer on first write.
    """
    return settings.artifacts_dir / "generated" / f"{job_id}.pdf"


def generated_cover_letter_path(settings: Settings, job_id: str) -> Path:
    """Where the per-job tailored cover letter text lives.

    Plain ``.txt`` — apply drivers paste into a textarea, so PDF rendering would
    be wasted work. Same parent dir as the résumé so a per-job artifacts dir
    listing shows both side-by-side.
    """
    return settings.artifacts_dir / "generated" / f"{job_id}_cover.txt"


def _job_upload_dir(settings: Settings, job_id: str) -> Path:
    """Per-job upload folder: ``artifacts/uploads/<job_id>/``."""
    return settings.uploads_dir / job_id


def job_cover_upload_path(settings: Settings, job_id: str, ext: str = ".docx") -> Path:
    """Where a job's upload-ready cover letter lives — ``<uploads>/<job_id>/Cover Letter<ext>``.

    The basename is the GENERIC ``Cover Letter`` (Playwright uploads by basename, so this is
    what the ATS sees — never the per-posting source name). ``ext`` is normalized to lead with
    a dot; default ``.docx`` (the usual hand-authored format)."""
    if ext and not ext.startswith("."):
        ext = "." + ext
    return _job_upload_dir(settings, job_id) / f"{_COVER_UPLOAD_STEM}{ext}"


def existing_job_cover(settings: Settings, job_id: str) -> Path | None:
    """The job's assigned cover letter if one exists, else ``None``.

    Looks for ``<uploads>/<job_id>/Cover Letter.*`` across the accepted extensions (preferring
    ``.docx``). ``None`` means "no manual cover assigned" — a benign no-attach, never an error.
    This is the per-job contract the apply worker reads (file existence = "assigned")."""
    folder = _job_upload_dir(settings, job_id)
    if not folder.exists():
        return None
    for ext in _COVER_LETTER_EXTS:
        cand = folder / f"{_COVER_UPLOAD_STEM}{ext}"
        if cand.exists():
            return cand
    return None


def assign_cover_letter(settings: Settings, job_id: str, source: Path | str) -> Path:
    """Copy a hand-authored letter into the job folder as the generic ``Cover Letter<ext>``.

    The per-job "write one per job" step (``av3 cover``). ``source`` is the user's real letter
    (e.g. ``CoverLetter_Tailscale_SE_Commercial.docx``); its CONTENT is preserved, only the
    upload basename is normalized. Replaces any prior assignment for this job (incl. one under a
    different extension). Returns the destination path. Raises ``FileNotFoundError`` if ``source``
    doesn't exist (the CLI surfaces it)."""
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"cover letter not found: {src}")
    folder = _job_upload_dir(settings, job_id)
    folder.mkdir(parents=True, exist_ok=True)
    # Clear any prior assignment (possibly a different extension) so there's exactly one.
    for ext in _COVER_LETTER_EXTS:
        prior = folder / f"{_COVER_UPLOAD_STEM}{ext}"
        if prior.exists():
            prior.unlink()
    dest = job_cover_upload_path(settings, job_id, src.suffix)
    shutil.copyfile(src, dest)
    return dest


def archive_cover_letter(settings: Settings, job_id: str) -> Path | None:
    """Move a confirmed-used cover letter to the archive, appending the job id to the name.

    Called after a positive ``APPLIED`` confirmation: the live upload stays generic, but the
    archived copy is identifiable — ``<uploads>/_archive/Cover Letter - <job_id><ext>``. Returns
    the archive path, or ``None`` if there was no cover to archive. Defensive: a move failure
    returns ``None`` (the apply already succeeded; archiving is bookkeeping, never fatal)."""
    cover = existing_job_cover(settings, job_id)
    if cover is None:
        return None
    archive_dir = settings.uploads_dir / "_archive"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f"{_COVER_UPLOAD_STEM} - {job_id}{cover.suffix}"
        shutil.move(str(cover), str(dest))
        return dest
    except OSError:
        return None


# --------------------------------------------------------------- bank → prompt strings

def build_bank_facts(bank: FactBank) -> str:
    """Render the fact bank as a *structured* string the generation prompts read.

    Distinct from :func:`auto_applier.pipeline.filter_worker.build_bank_summary`, which
    flattens for cosine similarity. Here the LLM must be able to copy bank facts
    *verbatim* (company/title/dates) — flattening would lose the structure the
    guard requires. So this writer preserves work-history shape explicitly.

    Sections in order: contact, work history (newest-first as listed), education,
    skills, certifications. Sections with no content are dropped silently — the
    LLM is told to use empty arrays where appropriate, so an empty section in
    the prompt just narrows what the LLM has to choose from.
    """
    parts: list[str] = []

    if bank.contact and bank.contact.name:
        # Just the name + location — the LLM doesn't need email/phone for résumé
        # body generation. Contact info gets injected by the PDF renderer.
        loc = f" ({bank.contact.location})" if bank.contact.location else ""
        parts.append(f"Name: {bank.contact.name}{loc}")

    if bank.work_history:
        parts.append("Work history:")
        for w in bank.work_history:
            dates = _date_range(w.start, w.end)
            header = f"  - {w.title} @ {w.company} [{dates}]" if dates else f"  - {w.title} @ {w.company}"
            parts.append(header)
            for bullet in w.bullets:
                if bullet:
                    parts.append(f"      * {bullet}")

    if bank.education:
        parts.append("Education:")
        for e in bank.education:
            line = e.degree or ""
            if e.field_of_study:
                line = f"{line} in {e.field_of_study}" if line else e.field_of_study
            if e.institution:
                line = f"{line} - {e.institution}" if line else e.institution
            parts.append(f"  - {line.strip()}")

    if bank.skills:
        parts.append(f"Skills: {', '.join(bank.skills)}")

    if bank.certifications:
        parts.append(f"Certifications: {', '.join(bank.certifications)}")

    return "\n".join(parts).strip()


def format_allowed_metrics(bank: FactBank) -> str:
    """One-per-line list of the user's allowed_metrics for the résumé prompt.

    Empty list returns the explicit ``"(none)"`` literal so the LLM doesn't
    interpret a blank tail as "any metric is fine" — every $/% claim has to
    map back to this list or the guard fails closed.
    """
    if not bank.allowed_metrics:
        return "(none)"
    return "\n".join(f"  - {m}" for m in bank.allowed_metrics)


def _date_range(start: str, end: str) -> str:
    s = (start or "").strip()
    e = (end or "").strip() or "Present"
    if not s and e == "Present":
        return ""
    if not s:
        return e
    return f"{s} - {e}"


# --------------------------------------------------------------- payload parsers

def parse_generated_resume(payload: dict) -> GeneratedResume:
    """Coerce a (presumed) LLM JSON reply into a :class:`GeneratedResume`.

    Strict at the wire (must be a dict), lenient at the merge (per-field defaults).
    Mirrors :func:`auto_applier.pipeline.score_worker.parse_dimensions`' philosophy —
    structural defects get repaired, semantic ones (fact correctness) are the
    guard's job. Raises :class:`ValueError` only for a non-dict top-level payload.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"generated résumé reply must be a JSON object (got {type(payload).__name__})"
        )

    summary = _as_str(payload.get("summary", ""))
    skills = _as_str_list(payload.get("skills", []))

    work_raw = payload.get("work", [])
    work: list[GenWorkEntry] = []
    if isinstance(work_raw, list):
        for entry in work_raw:
            if not isinstance(entry, dict):
                continue
            work.append(
                GenWorkEntry(
                    company=_as_str(entry.get("company", "")),
                    title=_as_str(entry.get("title", "")),
                    start=_as_str(entry.get("start", "")),
                    end=_as_str(entry.get("end", "")),
                    bullets=_as_str_list(entry.get("bullets", [])),
                )
            )

    edu_raw = payload.get("education", [])
    education: list[GenEducation] = []
    if isinstance(edu_raw, list):
        for entry in edu_raw:
            if not isinstance(entry, dict):
                continue
            education.append(
                GenEducation(
                    institution=_as_str(entry.get("institution", "")),
                    degree=_as_str(entry.get("degree", "")),
                )
            )

    return GeneratedResume(
        summary=summary,
        skills=skills,
        work=work,
        education=education,
    )


def parse_cover_letter(payload: dict) -> str:
    """Coerce a (presumed) LLM JSON reply into the cover letter body string.

    Raises :class:`ValueError` for a non-dict top-level payload OR an empty body
    string — an empty cover letter is structurally indistinguishable from a
    generation failure, and the optimize gate must fail closed on both. The
    worker catches and routes the job to REVIEW.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"cover letter reply must be a JSON object (got {type(payload).__name__})"
        )
    body = _as_str(payload.get("body", "")).strip()
    if not body:
        raise ValueError("cover letter body is empty")
    return body


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _as_str(item)
        if s:
            out.append(s)
    return out


# --------------------------------------------------------------- generators

class ResumeGenerator:
    """Orchestrate the LLM call → :class:`GeneratedResume`.

    Stateless (the prompt is recomputed per job from the bank + JD). One generator
    is built per worker run and shared across the per-job loop; concurrency is the
    LLM client's problem.
    """

    def __init__(self, llm: CompletionClient):
        self._llm = llm

    async def generate(
        self,
        *,
        bank: FactBank,
        job_description: str,
    ) -> GeneratedResume:
        """Return a structured résumé. Raises on any LLM/parse failure — the
        optimize worker catches and routes the job to REVIEW (fail-closed)."""
        prompt = GENERATE_RESUME.format(
            bank_facts=build_bank_facts(bank),
            allowed_metrics=format_allowed_metrics(bank),
            job_description=job_description,
        )
        payload = await self._llm.complete_json(prompt, system=GENERATE_RESUME.system)
        return parse_generated_resume(payload)


class CoverLetterGenerator:
    """Orchestrate the LLM call → plain-text cover letter body."""

    def __init__(self, llm: CompletionClient, target_words: int = DEFAULT_COVER_TARGET_WORDS):
        self._llm = llm
        self._target_words = target_words

    async def generate(
        self,
        *,
        bank: FactBank,
        job_description: str,
        company: str,
        title: str,
    ) -> str:
        """Return the cover letter body (no salutation, no signature)."""
        prompt = GENERATE_COVER_LETTER.format(
            bank_facts=build_bank_facts(bank),
            target_words=self._target_words,
            company=company or "the company",
            title=title or "the role",
            job_description=job_description,
        )
        payload = await self._llm.complete_json(prompt, system=GENERATE_COVER_LETTER.system)
        return parse_cover_letter(payload)
