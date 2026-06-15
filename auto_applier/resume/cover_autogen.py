"""Cover-letter autogen for strong matches — BUILD 5 (spec §6b, "ready just in case").

The optimize→apply pipeline already writes a per-job cover letter (the ``.txt`` the apply
driver pastes into a textarea). This module serves the **discovery+scoring-only** operating
mode, where the user applies externally and just wants a tailored letter sitting ready for
every strong job. It:

  1. generates the same guarded cover-letter body (:class:`CoverLetterGenerator` + the
     ``gen-cover-v2`` no-AI-tells prompt + the :func:`vet_cover_letter` fabrication guard), and
  2. renders it to a real Word **.docx** at the per-job upload path
     (``uploads/<job_id>/[<Name> ]Cover Letter.docx``) — ``.docx`` because the user pastes /
     uploads into web fields where markdown wouldn't render ([[feedback_paste_docs_as_docx]]),
     and because that path is exactly where the apply worker / ``av3 cover`` already look.

Invariants (mirror the apply path's reliability floor):

  * **NEVER clobber an existing letter.** A hand-authored ``av3 cover`` (or a prior autogen)
    always wins; autogen only fills the gap. A single deliberate regenerate may pass
    ``force=True`` (the ``av3 cover --generate <id> --force`` path); the batch backfill never
    forces.
  * **Guard fail-closed.** If :func:`vet_cover_letter` flags an unsupported tech claim the
    letter is NOT written and the job is left letterless with a note — a letter "ready just in
    case" must never be a fabrication the user would have to walk back live.
  * **No pipeline coupling.** This is CLI / daily-refresh driven (``av3 cover --generate`` /
    ``--generate-all``); it never advances job state and the apply worker never calls it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from auto_applier.config.settings import Settings
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job
from auto_applier.domain.state import JobState
from auto_applier.llm.complete import CompletionClient
from auto_applier.resume.factbank import Contact, FactBank
from auto_applier.resume.generate import (
    CoverLetterGenerator,
    existing_job_cover,
    job_cover_upload_path,
)
from auto_applier.resume.guard import vet_cover_letter

__all__ = [
    "CoverAutogenResult",
    "GENERATED",
    "SKIPPED_EXISTING",
    "SKIPPED_GUARD",
    "SKIPPED_NO_DESCRIPTION",
    "SKIPPED_DEGENERATE",
    "ERROR",
    "backfill",
    "generate_one",
    "render_cover_letter_docx",
]

# --- result statuses --------------------------------------------------------
GENERATED = "generated"                      # wrote a fresh .docx
SKIPPED_EXISTING = "skipped_existing"        # a cover already exists (no-clobber); LLM not called
SKIPPED_GUARD = "skipped_guard"              # fabrication guard flagged unsupported claims; not written
SKIPPED_NO_DESCRIPTION = "skipped_no_description"  # no JD text to tailor against; LLM not called
SKIPPED_DEGENERATE = "skipped_degenerate"    # model produced runaway/repetitive output; not written
ERROR = "error"                              # generation or render raised


# Em-dash (U+2014) / en-dash (U+2013) used as a pause, with optional surrounding spaces but
# NOT across a newline (so paragraph breaks survive). The user's #1 AI tell.
_DASH_AS_PAUSE = re.compile(r"[ \t]*[—–][ \t]*")


def _strip_ai_tells(body: str) -> str:
    """Deterministic backstop for the #1 AI tell: em/en dashes.

    The ``gen-cover-v2`` prompt forbids them, but a local model drifts — so we also strip
    them mechanically here, replacing a dash-as-pause with a comma. This GUARANTEES no dash
    ever ships regardless of model behavior (the user is adamant). It only touches dashes;
    the rest of the no-AI-tells voice (excited/buzzwords/rule-of-three) stays prompt-driven,
    since those can't be fixed by a blind substitution without mangling meaning."""
    s = _DASH_AS_PAUSE.sub(", ", body or "")
    s = re.sub(r"[ \t]{2,}", " ", s)            # collapse space runs the substitution may leave
    s = re.sub(r"[ \t]+([,.;:])", r"\1", s)     # no space before punctuation
    s = re.sub(r",\s*,", ",", s)                # collapse a doubled comma
    return s


def _opens_in_third_person(body: str, name: str) -> bool:
    """True if the letter opens by naming the candidate or using 'He/His' as the subject.

    qwen3:8b occasionally drifts into third person on far-from-bank roles (observed on the
    Cockroach 'Value Engineer' JD, 2026-06-15: "Joseph Lira has built..."). A first-person
    letter always opens with 'I' or 'At/When/After <company>, I', so a name/He/His opener is
    an unambiguous, cheaply-detectable defect — :func:`generate_one` regenerates once on it."""
    head = (body or "").lstrip()[:80].lower()
    if not head:
        return False
    candidates = ["he ", "his "]
    n = (name or "").strip().lower()
    if n:
        candidates.append(n)            # full name as the opening subject
        candidates.append(n.split()[0] + " ")  # just the first name
    return any(head.startswith(c) for c in candidates)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _ensure_paragraphs(body: str) -> str:
    """Guarantee the hook / body / close 3-paragraph shape when the model returned one block.

    qwen3 honors the prompt's 'exactly three paragraphs' instruction only ~1/3 of the time
    (observed 2026-06-15). So if the body has no blank-line break and enough sentences,
    regroup deterministically: first sentence = the hook, last = the close, the rest = the
    middle. Content-preserving — it only inserts paragraph breaks at sentence boundaries, and
    leaves a body the model already paragraphed (or one too short to split) untouched."""
    text = (body or "").strip()
    if not text or "\n\n" in text:
        return text  # already paragraphed (or empty) — respect what the model produced
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) < 4:
        return text  # too short for a meaningful hook/body/close
    hook, close = sentences[0], sentences[-1]
    middle = " ".join(sentences[1:-1])
    return f"{hook}\n\n{middle}\n\n{close}"


# The prompt targets 150-250 words; 250 words is ~1700 chars, and a live batch of 458 letters
# put p95 at 1669 and p99 at 3101. So a body over ~2000 chars is over-target — either a qwen3
# repetition loop (the Mistral JDs ran to 20K-208K chars) or padded/doubled output. 2000 cleanly
# separates the 94% normal (<1500) tail from the runaway/padded ones without clipping a real letter.
_MAX_COVER_CHARS = 2000
# The clean samples have 5-7 sentences; a body with 13+ substantial sentences is a list-dump
# ("I built... I designed... I implemented..." ×24), not a letter — qwen3's other failure mode on
# far-from-bank ML/FD roles (unique sentences, so the repeat check misses it). 12 is ~2x the
# largest legitimate letter, so it never clips a real one.
_MAX_COVER_SENTENCES = 12


def _is_degenerate(body: str) -> bool:
    """True if the model produced runaway / padded / repetitive / list-dump output. qwen3 +
    greedy (temp 0) decoding can loop, repeating a sentence many times (Mistral JDs, 2026-06-15:
    20K-208K chars; milder doubled-sentence padding to ~3K chars), OR dump a wall of 24 distinct
    'I've done X' sentences. The fabrication guard passes all of these (every claim is
    bank-supported) and the dash/paragraph backstops don't check length, so this is the only
    thing standing between a degenerate generation and a shipped monstrosity. Caught by
    over-length OR too many sentences OR a substantial sentence repeated."""
    text = (body or "").strip()
    if len(text) > _MAX_COVER_CHARS:
        return True
    sentences = [p.strip() for p in _SENTENCE_SPLIT.split(text) if len(p.strip()) > 20]
    if len(sentences) > _MAX_COVER_SENTENCES:   # a wall of 13+ sentences is a list-dump, not a letter
        return True
    seen: dict[str, int] = {}
    for s in sentences:
        seen[s] = seen.get(s, 0) + 1
        if seen[s] >= 2:               # the same real sentence twice → padded/looping
            return True
    return False


@dataclass
class CoverAutogenResult:
    """One job's autogen outcome — observable, not side-effect-only (mirrors the
    pipeline run-summary style so the CLI can tally without re-querying)."""

    job_id: str
    status: str
    detail: str = ""
    path: str = ""  # the written .docx, set only when status == GENERATED

    @property
    def ok(self) -> bool:
        return self.status == GENERATED


# --- .docx render -----------------------------------------------------------

def render_cover_letter_docx(
    body: str,
    contact: Contact,
    out_path: Path,
    *,
    greeting: str = "Dear Hiring Manager,",
    closing: str = "Sincerely,",
) -> Path:
    """Render a complete, uploadable ``.docx`` cover letter.

    The generator returns a salutation/signature-less body (the apply driver wraps those when
    pasting into a textarea). A standalone document the human uploads needs to be *complete*,
    so we wrap a standard greeting, the body paragraphs, and a closing with the applicant's
    name. A name + contact header makes it read as a real letter.

    Anti-detection nicety: python-docx stamps ``author = "python-docx"`` in the file's core
    properties by default — a tell. We overwrite it with the applicant's own name (or blank).
    """
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    name = (contact.name or "").strip() if contact else ""

    if name:
        head = doc.add_paragraph()
        run = head.add_run(name)
        run.bold = True
        run.font.size = Pt(14)

    contact_bits = [
        b.strip()
        for b in ((contact.email if contact else ""),
                  (contact.phone if contact else ""),
                  (contact.location if contact else ""))
        if b and b.strip()
    ]
    if contact_bits:
        doc.add_paragraph(" | ".join(contact_bits))

    if name or contact_bits:
        doc.add_paragraph("")  # spacer before the salutation

    doc.add_paragraph(greeting)
    doc.add_paragraph("")

    for para in (p.strip() for p in (body or "").split("\n\n")):
        if para:
            doc.add_paragraph(para)

    doc.add_paragraph("")
    doc.add_paragraph(closing)
    if name:
        doc.add_paragraph(name)

    try:  # core-properties author is metadata; never fatal if the backend rejects it
        doc.core_properties.author = name
    except Exception:  # noqa: BLE001
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


# --- single job -------------------------------------------------------------

async def generate_one(
    settings: Settings,
    job: Job,
    *,
    bank: FactBank,
    generator: CoverLetterGenerator,
    name: str = "",
    vocabulary: tuple[str, ...] | None = None,
    force: bool = False,
) -> CoverAutogenResult:
    """Generate + guard + render ONE job's cover letter. Pure of state transitions.

    Order matches the invariants: no-clobber check (unless ``force``) → JD presence →
    LLM generation → fabrication guard (fail-closed) → ``.docx`` render. Any exception is
    caught and returned as an :data:`ERROR` result so a batch never aborts on one job.
    """
    if not force:
        existing = existing_job_cover(settings, job.id)
        if existing is not None:
            return CoverAutogenResult(
                job.id, SKIPPED_EXISTING,
                f"cover already present ({existing.name}); use --force to overwrite",
                str(existing),
            )

    if not (job.description or "").strip():
        return CoverAutogenResult(
            job.id, SKIPPED_NO_DESCRIPTION, "job has no description to tailor against"
        )

    # Generate, with one fail-safe second attempt. The retry serves two cases: (a) the first
    # draft opened in the third person (a clear defect the voice prompt mostly but not always
    # prevents), and (b) the first attempt errored — most often a JD whose qwen3 reasoning blew
    # past the Ollama read timeout. The retry drops qwen3's thinking (`no_think=True`), which
    # both rescues those slow JDs (~2s vs a 180s timeout) and regenerates a fresh first-person
    # draft. The default first attempt keeps thinking on, matching the bulk of letters.
    voice_name = (bank.contact.name if (bank and bank.contact) else "") or name
    body = ""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            raw = await generator.generate(
                bank=bank,
                job_description=job.description,
                company=job.company,
                title=job.title,
                no_think=attempt > 0,
            )
        except Exception as exc:  # noqa: BLE001 — one job's failure must not kill a batch
            last_exc = exc
            continue  # retry once (the no_think attempt is fast and usually succeeds)
        last_exc = None
        body = _strip_ai_tells(raw)  # deterministic em/en-dash backstop before guard + render
        # A clean draft wins. A third-person or runaway/repetitive draft → regenerate (the
        # retry drops qwen3 thinking). Some JDs make qwen3 loop regardless (Mistral FD-ML,
        # 2026-06-15) — if the final draft is still degenerate it's rejected below, never shipped.
        if not _opens_in_third_person(body, voice_name) and not _is_degenerate(body):
            break
    if last_exc is not None:
        return CoverAutogenResult(job.id, ERROR, f"generation failed: {last_exc}")
    if _is_degenerate(body):
        return CoverAutogenResult(
            job.id, SKIPPED_DEGENERATE,
            f"model produced runaway/repetitive output ({len(body)} chars); letter not written",
        )
    body = _ensure_paragraphs(body)  # guarantee hook/body/close shape if the model ran it together
    guard = vet_cover_letter(body, bank, vocabulary)
    if not guard.ok:
        terms = ", ".join(sorted({f.claim for f in guard.findings})) or "unsupported claim"
        return CoverAutogenResult(
            job.id, SKIPPED_GUARD,
            f"fabrication guard flagged: {terms} (letter not written)",
        )

    # force=True: clear every prior cover variant first so there's exactly one assigned file
    # (mirrors assign_cover_letter's replace semantics; uses only the public lookup).
    if force:
        for _ in range(8):  # bounded — at most a handful of ext variants ever exist
            prior = existing_job_cover(settings, job.id)
            if prior is None:
                break
            try:
                prior.unlink()
            except OSError:
                break

    out_path = job_cover_upload_path(settings, job.id, ".docx", name)
    try:
        render_cover_letter_docx(body, bank.contact, out_path)
    except Exception as exc:  # noqa: BLE001
        return CoverAutogenResult(job.id, ERROR, f"docx render failed: {exc}")

    return CoverAutogenResult(job.id, GENERATED, f"{job.company} — {job.title}", str(out_path))


# --- batch backfill ---------------------------------------------------------

async def backfill(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    llm: CompletionClient,
    bank: FactBank,
    min_score: float,
    name: str = "",
    limit: int | None = None,
    states: tuple[JobState, ...] = (JobState.DECIDED,),
    vocabulary: tuple[str, ...] | None = None,
) -> list[CoverAutogenResult]:
    """Write letters for every job scoring ≥ ``min_score`` (in ``states``) that lacks one.

    Reads :meth:`ScoreRepo.list_ranked` (ranked by score desc) with ``min_total=min_score``,
    filters to ``states`` (default ``DECIDED`` — a strong match resting in discovery+scoring-
    only mode), skips jobs that already have a cover (recorded, but they don't consume
    ``limit``), and generates up to ``limit`` *new* letters. The backfill never forces, so a
    hand-authored letter is always preserved. Returns one result per job touched, highest
    score first — the CLI tallies them.
    """
    generator = CoverLetterGenerator(llm)
    job_repo = JobRepo(conn)
    allowed = {s.value for s in states}

    results: list[CoverAutogenResult] = []
    generated = 0  # counts only jobs where the LLM actually ran (caps real work)

    for row in ScoreRepo(conn).list_ranked(min_total=min_score):
        if row.get("state") not in allowed:
            continue
        job = job_repo.get(row["job_id"])
        if job is None:
            continue

        # An existing letter is reported but is free (no LLM) — don't let it eat the limit.
        if existing_job_cover(settings, job.id) is not None:
            results.append(CoverAutogenResult(
                job.id, SKIPPED_EXISTING, f"{job.company} — {job.title}"
            ))
            continue

        if limit is not None and generated >= limit:
            break

        res = await generate_one(
            settings, job, bank=bank, generator=generator, name=name, vocabulary=vocabulary
        )
        results.append(res)
        # GENERATED / SKIPPED_GUARD / SKIPPED_DEGENERATE / ERROR all mean the LLM was invoked →
        # consume a slot. SKIPPED_NO_DESCRIPTION short-circuits before the LLM, so it's free.
        if res.status in (GENERATED, SKIPPED_GUARD, SKIPPED_DEGENERATE, ERROR):
            generated += 1

    return results
