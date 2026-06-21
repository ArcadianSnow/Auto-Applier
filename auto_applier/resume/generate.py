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

import json
import re
import shutil
import sqlite3
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
    "archive_resume",
    "assign_cover_letter",
    "assign_resume",
    "build_bank_facts",
    "existing_job_cover",
    "existing_job_resume",
    "format_allowed_metrics",
    "generated_cover_letter_path",
    "generated_resume_path",
    "job_cover_upload_path",
    "job_resume_upload_path",
    "parse_cover_letter",
    "parse_generated_resume",
]

#: File extensions an ATS cover-letter upload accepts (Greenhouse's #cover_letter accept
#: list). A hand-authored letter keeps its extension; .docx is the usual real content.
_COVER_LETTER_EXTS = (".docx", ".pdf", ".doc", ".txt", ".rtf")
#: Résumé upload extensions (prefer .pdf, then the hand-authored .docx/.doc).
_RESUME_EXTS = (".pdf", ".docx", ".doc")

#: Generic basenames the files are uploaded under. Playwright sends a file by its BASENAME, so
#: this is what the ATS sees — a per-posting source name is a mass-apply fingerprint; these are
#: what a normal applicant uploads. Per-job identity lives in the folder path, not the name (§8c).
_COVER_UPLOAD_STEM = "Cover Letter"
_RESUME_UPLOAD_STEM = "Resume"


#: Default cover-letter target length (spec §6b: concise & tailored, ~150-250 words).
#: Knob lives here, not in :class:`Settings`, until §8e data motivates a config knob —
#: configurable-but-unmeasured length would just be a guess. v3.0 ships fixed; the
#: outcome feedback loop (v3.1) revises this from response-rate data.
DEFAULT_COVER_TARGET_WORDS = 200

# A cover letter references only 1-2 JD requirements, and those are front-loaded (role
# summary + responsibilities + top requirements come first; boilerplate/EEO/benefits trail).
# So we send only the JD head: it costs almost nothing in relevance and keeps a very large
# JD under the Ollama read-timeout ceiling. qwen3:8b reasoning on a ~8K-char JD blew past 180s
# (Cockroach Labs, 2026-06-15) and the job fell out letterless; trimming to the head fixes that.
_COVER_JD_MAX_CHARS = 4000


def _trim_jd_for_cover(job_description: str) -> str:
    """Return the JD head, capped at :data:`_COVER_JD_MAX_CHARS`, cut at a whitespace
    boundary so we never split a word. Short JDs pass through unchanged."""
    jd = job_description or ""
    if len(jd) <= _COVER_JD_MAX_CHARS:
        return jd
    head = jd[:_COVER_JD_MAX_CHARS]
    cut = head.rfind("\n")
    if cut < _COVER_JD_MAX_CHARS // 2:        # no late newline → fall back to a space
        cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + "\n\n[...]"


# --------------------------------------------------------------- canonical paths

def _slug(text: str, maxlen: int = 28) -> str:
    """ASCII-safe filename fragment: non-alphanumerics → ``_``, trimmed + length-capped."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return s[:maxlen].strip("_")


def _applicant_name(settings: Settings) -> str:
    """Read the candidate name from the fact bank (master.json). ``""`` on any failure —
    read directly (not via web.onboarding) to keep the resume layer free of a web import."""
    try:
        data = json.loads(
            (settings.data_dir / "profile" / "master.json").read_text(encoding="utf-8")
        )
        return str((data.get("contact") or {}).get("name", "") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _job_company_title(settings: Settings, job_id: str) -> tuple[str, str]:
    """(company, title) for a job, read straight from app.db. ``("", "")`` on any failure
    (missing DB / row / uncommitted job) so the stem degrades to the bare id."""
    try:
        conn = sqlite3.connect(str(settings.app_db_path))
        try:
            row = conn.execute(
                "SELECT company, title FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return str(row[0] or ""), str(row[1] or "")
    except sqlite3.Error:
        pass
    return "", ""


def _artifact_stem(settings: Settings, job_id: str, kind: str) -> str:
    """Human-readable, still-deterministic-from-job_id artifact stem
    (``"{Name}_{kind}_{Company}_{Title}_{id8}"``). Derives the readable bits from the job
    (DB) + applicant (master.json); on ANY missing data falls back to the bare ``job_id`` so
    behaviour degrades safely and callers/tests with no seeded job/bank are unaffected.
    Both the optimize worker (write) and the apply worker (read) call the path helpers with the
    same ``job_id``, so they always derive the identical path — no DB hand-off needed."""
    company, title = _job_company_title(settings, job_id)
    bits = [b for b in (_slug(_applicant_name(settings), 24), kind, _slug(company), _slug(title)) if b]
    if len(bits) <= 1:  # nothing readable beyond the kind label → keep the legacy name exactly
        return f"{job_id}_cover" if kind == "Cover" else job_id
    bits.append(job_id[:8])
    return "_".join(bits)


def generated_resume_path(settings: Settings, job_id: str) -> Path:
    """Where the per-job tailored résumé PDF lives.

    Deterministic from ``job.id`` so the optimize worker writes and the apply worker reads
    without any DB hand-off. The on-disk name is human-readable (``Jane_Doe_Resume_Acme_Data
    _Engineer_48b5b857.pdf``) when the job + fact bank are available, else the bare id.
    ``settings.artifacts_dir / "generated"`` is the parent — created by the renderer on first write.
    """
    return settings.artifacts_dir / "generated" / f"{_artifact_stem(settings, job_id, 'Resume')}.pdf"


def generated_cover_letter_path(settings: Settings, job_id: str) -> Path:
    """Where the per-job tailored cover letter text lives.

    Plain ``.txt`` — apply drivers paste into a textarea, so PDF rendering would be wasted
    work. Same parent dir + readable-name scheme as the résumé (``..._Cover_..._48b5b857.txt``).
    """
    return settings.artifacts_dir / "generated" / f"{_artifact_stem(settings, job_id, 'Cover')}.txt"


def _job_upload_dir(settings: Settings, job_id: str) -> Path:
    """Per-job upload folder: ``artifacts/uploads/<job_id>/``."""
    return settings.uploads_dir / job_id


def _norm_ext(ext: str) -> str:
    return ext if (not ext or ext.startswith(".")) else "." + ext


def _safe_name(name: str) -> str:
    """Filesystem-safe form of the applicant name for an upload basename.

    Strips characters illegal in Windows filenames and collapses whitespace. Empty/blank →
    "" (the caller then uses the bare stem)."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "", (name or "")).strip()
    return re.sub(r"\s+", " ", cleaned)


def _basename(stem: str, name: str) -> str:
    """Upload basename: ``"<Name> <Stem>"`` when a name is given, else the bare ``<Stem>``.

    A real applicant's file is ``Joseph Lira Resume.pdf`` / ``Joseph Lira Cover Letter.docx`` —
    the NAME is not a templating tell (it's the applicant's own), so it's consistent with the
    anti-detection goal while looking like a normal upload. With no name → the bare generic stem."""
    nm = _safe_name(name)
    return f"{nm} {stem}" if nm else stem


# --- generic per-job upload mechanism (shared by cover letter + résumé) ---------
# Each job's upload-ready files live under uploads/<job_id>/ with a normal basename — either the
# bare generic stem (``Cover Letter<ext>`` / ``Resume<ext>``) or name-prefixed
# (``Joseph Lira Resume.pdf``). Playwright uploads a file by its basename, so the per-POSTING
# source name (``CoverLetter_Tailscale_SE_Commercial.docx``) is a mass-apply fingerprint — the
# per-job identity lives in the folder PATH, never the uploaded name (§8c anti-detection). Lookup
# globs ``*<stem><ext>`` so it finds the file regardless of any name prefix. One assigned file per
# (stem) per job; on a confirmed APPLIED it's archived (moved) with the job id appended.

def _upload_path(settings: Settings, job_id: str, stem: str, ext: str, name: str = "") -> Path:
    return _job_upload_dir(settings, job_id) / f"{_basename(stem, name)}{_norm_ext(ext)}"


def _existing_upload(settings: Settings, job_id: str, stem: str, exts) -> Path | None:
    folder = _job_upload_dir(settings, job_id)
    if not folder.exists():
        return None
    # Glob ``*<stem><ext>`` (ext-preference order) so a name-prefixed OR bare file is found.
    for ext in exts:
        matches = sorted(folder.glob(f"*{stem}{ext}"))
        if matches:
            return matches[0]
    return None


def _assign_upload(
    settings: Settings, job_id: str, source: Path | str, stem: str, name: str = ""
) -> Path:
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"file not found: {src}")
    folder = _job_upload_dir(settings, job_id)
    folder.mkdir(parents=True, exist_ok=True)
    # Replace any prior assignment for this stem (name-prefixed or bare, any extension) so
    # there's exactly one. glob ``*<stem>.*`` catches every variant robustly.
    for prior in folder.glob(f"*{stem}.*"):
        prior.unlink()
    dest = _upload_path(settings, job_id, stem, src.suffix, name)
    shutil.copyfile(src, dest)
    return dest


def _archive_upload(settings: Settings, job_id: str, stem: str, exts) -> Path | None:
    existing = _existing_upload(settings, job_id, stem, exts)
    if existing is None:
        return None
    archive_dir = settings.uploads_dir / "_archive"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Keep the (possibly name-prefixed) basename, append the job id for identifiability.
        dest = archive_dir / f"{existing.stem} - {job_id}{existing.suffix}"
        shutil.move(str(existing), str(dest))
        return dest
    except OSError:  # archiving is post-confirmation bookkeeping — never fatal
        return None


# --- cover letter (av3 cover) ---------------------------------------------------

def job_cover_upload_path(
    settings: Settings, job_id: str, ext: str = ".docx", name: str = ""
) -> Path:
    """Where a job's upload-ready cover letter lives —
    ``<uploads>/<job_id>/[<Name> ]Cover Letter<ext>`` (the ATS never sees the per-posting source name)."""
    return _upload_path(settings, job_id, _COVER_UPLOAD_STEM, ext, name)


def existing_job_cover(settings: Settings, job_id: str) -> Path | None:
    """The job's assigned cover letter (``<uploads>/<job_id>/*Cover Letter.*``) or ``None`` — the
    per-job contract the apply worker reads (file existence = "assigned"). Benign no-attach."""
    return _existing_upload(settings, job_id, _COVER_UPLOAD_STEM, _COVER_LETTER_EXTS)


def assign_cover_letter(
    settings: Settings, job_id: str, source: Path | str, name: str = ""
) -> Path:
    """Copy a hand-authored letter into the job folder as ``[<Name> ]Cover Letter<ext>``
    (``av3 cover`` — the per-job "write one per job" step). Content preserved; only the upload
    basename is normalized. Replaces any prior assignment. Raises ``FileNotFoundError`` if missing."""
    return _assign_upload(settings, job_id, source, _COVER_UPLOAD_STEM, name)


def archive_cover_letter(settings: Settings, job_id: str) -> Path | None:
    """On a confirmed APPLIED, move the cover to ``<uploads>/_archive/<basename> - <job_id><ext>``.
    Returns the archive path, or ``None`` if there was nothing to archive (or the move failed)."""
    return _archive_upload(settings, job_id, _COVER_UPLOAD_STEM, _COVER_LETTER_EXTS)


# --- résumé (av3 resume) --------------------------------------------------------

def job_resume_upload_path(
    settings: Settings, job_id: str, ext: str = ".pdf", name: str = ""
) -> Path:
    """Where a job's upload-ready résumé lives — ``<uploads>/<job_id>/[<Name> ]Resume<ext>``
    (the ATS never sees ``Joseph_Lira_Resume_Solutions_Engineer.docx``)."""
    return _upload_path(settings, job_id, _RESUME_UPLOAD_STEM, ext, name)


def existing_job_resume(settings: Settings, job_id: str) -> Path | None:
    """The job's manually-assigned résumé (``<uploads>/<job_id>/*Resume.*``) or ``None``. Takes
    precedence over the optimize-generated PDF in the worker — a hand-crafted résumé per posting."""
    return _existing_upload(settings, job_id, _RESUME_UPLOAD_STEM, _RESUME_EXTS)


def assign_resume(
    settings: Settings, job_id: str, source: Path | str, name: str = ""
) -> Path:
    """Copy a hand-crafted résumé into the job folder as ``[<Name> ]Resume<ext>`` (``av3
    resume``). Same per-job model as the cover letter. Raises ``FileNotFoundError`` if missing."""
    return _assign_upload(settings, job_id, source, _RESUME_UPLOAD_STEM, name)


def archive_resume(settings: Settings, job_id: str) -> Path | None:
    """On a confirmed APPLIED, move the résumé to ``<uploads>/_archive/<basename> - <job_id><ext>``.
    Only acts on a manually-assigned résumé (the optimize PDF / global résumé are left in place)."""
    return _archive_upload(settings, job_id, _RESUME_UPLOAD_STEM, _RESUME_EXTS)


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
        no_think: bool = False,
    ) -> str:
        """Return the cover letter body (no salutation, no signature).

        ``no_think`` appends qwen3's ``/no_think`` switch to disable its chain-of-thought for
        this call. A few reasoning-heavy JDs (Mistral Forward-Deployed-ML, ~4.5K ch) make qwen3
        think past the 180s Ollama read timeout and fail; ``/no_think`` returns the same letter
        in ~2s. Used as the fail-safe second attempt in :func:`cover_autogen.generate_one`, not
        the default (the default keeps thinking on, matching the bulk of generated letters)."""
        prompt = GENERATE_COVER_LETTER.format(
            bank_facts=build_bank_facts(bank),
            target_words=self._target_words,
            company=company or "the company",
            title=title or "the role",
            job_description=_trim_jd_for_cover(job_description),
        )
        if no_think:
            prompt += "\n\n/no_think"
        payload = await self._llm.complete_json(prompt, system=GENERATE_COVER_LETTER.system)
        return parse_cover_letter(payload)
