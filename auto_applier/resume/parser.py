"""Extract text from resume documents (PDF/DOCX)."""
from pathlib import Path


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
