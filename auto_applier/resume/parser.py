"""Extract text from resume documents (PDF/DOCX)."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_text(resume_path: str | Path) -> str:
    """Extract plain text from a resume file.

    Supported formats: PDF (.pdf) and Word (.docx/.doc).

    Raises:
        ValueError: If the file extension is not supported.
        FileNotFoundError: If the file does not exist.
    """
    path = Path(resume_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_from_pdf(path)
    elif suffix in (".docx", ".doc"):
        return _extract_from_docx(path)
    else:
        raise ValueError(f"Unsupported resume format: {suffix}. Use PDF or DOCX.")


def extract_text_via_cache(resume_path: str | Path) -> str:
    """Read resume text, preferring the cached PDF when available.

    Reads from ``data/resumes/.converted/<stem>.pdf`` if it exists
    AND is at least as new as the source file. Falls back to the
    original file if the cache is missing or stale.

    Why prefer the cache:

    1. **No file-lock collisions.** A user editing their .docx in
       Word holds an exclusive lock that breaks the .docx reader
       (`python-docx` / `open(rb)`). The cached PDF is a separate
       file Word never touches, so the scorer can read it during a
       run while the user keeps the source open.
    2. **What the employer reads.** The cached PDF is the same file
       we upload. Scoring against it gives a more honest answer to
       "is this resume strong for that JD?" — it's the bytes the
       employer actually parses.

    Caller can still get original-file behaviour by calling
    ``extract_text`` directly. ``ResumeManager`` switched to this
    helper so the whole pipeline benefits without per-callsite
    awareness.
    """
    from auto_applier.config import CONVERTED_RESUMES_DIR

    path = Path(resume_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume file not found: {path}")

    # The pdf_converter cache key is sanitized stem; mirror that
    # logic without importing the helper to avoid a circular dep.
    import re as _re
    safe_stem = _re.sub(r"[^a-zA-Z0-9._-]", "_", path.stem)
    cached = CONVERTED_RESUMES_DIR / f"{safe_stem}.pdf"
    try:
        if cached.exists() and cached.stat().st_mtime >= path.stat().st_mtime:
            return _extract_from_pdf(cached)
    except OSError:
        pass

    # Cache missing or stale — read the original. If the original
    # is locked (Word/Acrobat holds it), surface a clear error so
    # callers can fall back appropriately.
    try:
        return extract_text(path)
    except PermissionError as exc:
        # Last-ditch: if a stale cached PDF exists, use it rather
        # than failing the run. Better stale text than no text.
        if cached.exists():
            logger.warning(
                "Resume %s is locked (likely open in Word/Acrobat); "
                "reading slightly-stale cached PDF instead.",
                path.name,
            )
            return _extract_from_pdf(cached)
        raise PermissionError(
            f"Resume {path.name} is in use (likely open in Word or "
            "Acrobat). Close the file and retry, or run the wizard "
            "once with the file closed to build a cached copy."
        ) from exc


def _extract_from_pdf(path: Path) -> str:
    """Extract text from a PDF file using pdfplumber."""
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def _extract_from_docx(path: Path) -> str:
    """Extract text from a DOCX file using python-docx.

    Opens the file through an explicit ``open()`` context manager
    instead of letting python-docx hold onto the path, so the OS
    file handle is released as soon as this function returns.
    Without this, Windows can hit WinError 32 ('sharing violation')
    on the next operation that touches the same file.
    """
    from docx import Document

    with open(path, "rb") as f:
        doc = Document(f)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
