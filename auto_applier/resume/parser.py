"""Extract text content from PDF and DOCX resume files."""

from pathlib import Path


def extract_text(resume_path: Path) -> str:
    """Extract raw text from a resume file (PDF or DOCX)."""
    suffix = resume_path.suffix.lower()

    if suffix == ".pdf":
        return _extract_from_pdf(resume_path)
    elif suffix in (".docx", ".doc"):
        return _extract_from_docx(resume_path)
    else:
        raise ValueError(f"Unsupported resume format: {suffix}. Use PDF or DOCX.")


def _extract_from_pdf(path: Path) -> str:
    import pdfplumber

    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def _extract_from_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
