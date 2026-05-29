"""ATS-safe single-column PDF rendering for generated résumés (spec §6b).

> "Generated résumés render to ATS-safe single-column PDF (no tables/text-boxes/
>  headers-footers, real selectable text) — maximizing parser pass-through is the
>  point; human prettiness is secondary."

This module exists as a tiny seam over Playwright Chromium so the optimize worker
doesn't import Playwright directly — testability + import-cost. Tests pass a stub
renderer that writes a marker file; production wires :func:`render_resume_pdf`
which uses the same Chromium we already require for scraping.

**Why HTML → PDF rather than ReportLab/wkhtmltopdf:**
  * Zero new dependencies (Playwright Chromium is already vendored).
  * The v2 ``tailor.py`` ported HTML → PDF rendering — same approach, less divergence.
  * Real text (not raster) so ATS parsers extract characters, not screenshots.

**ATS rules baked into the template** (research/fabrication-guard.md §3 + spec §6b):
  - One column. No tables, no text boxes, no headers/footers, no images.
  - Standard fonts only (Arial fallback — wide system support, parser-friendly).
  - Plain semantic HTML: ``h1``/``h2``/``h3``/``p``/``ul``/``li``.
  - Reasonable margins (~0.5") so parsers don't get content too close to the edges.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Awaitable, Callable

from av3.resume.factbank import Contact
from av3.resume.guard import GeneratedResume

logger = logging.getLogger(__name__)

__all__ = [
    "PdfRenderer",
    "build_resume_html",
    "render_resume_pdf",
]


# Renderer signature — async callable that writes ``out_path`` and returns True on
# success, False on a recoverable failure. Tests inject a stub matching this shape.
PdfRenderer = Callable[[str, Path], Awaitable[bool]]


# Minimal ATS-friendly template (mirrors v2's ``tailor.py`` shape, kept here so v3
# has no v2 import dependency).
_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Resume</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 11pt;
          color: #111; max-width: 720px; margin: 24px auto; line-height: 1.45; }}
  h1 {{ font-size: 22pt; margin: 0 0 4pt 0; }}
  h2 {{ font-size: 13pt; margin: 18pt 0 4pt 0; border-bottom: 1px solid #888;
        padding-bottom: 2pt; text-transform: uppercase; letter-spacing: 0.5pt; }}
  h3 {{ font-size: 11pt; margin: 10pt 0 2pt 0; }}
  .role-meta {{ color: #555; font-size: 10pt; margin: 0 0 4pt 0; }}
  ul {{ margin: 4pt 0 8pt 0; padding-left: 18pt; }}
  li {{ margin: 2pt 0; }}
  .skills {{ margin: 4pt 0; }}
  .header-contact {{ color: #333; font-size: 10pt; margin: 0 0 12pt 0; }}
</style>
</head>
<body>
<h1>{name}</h1>
<div class="header-contact">{contact}</div>
{summary_block}
{skills_block}
{experience_block}
{education_block}
</body></html>
"""


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _format_contact_line(contact: Contact) -> str:
    """Build the one-line contact header from the fact-bank :class:`Contact`."""
    parts: list[str] = []
    if contact.email:
        parts.append(contact.email)
    if contact.phone:
        parts.append(contact.phone)
    if contact.location:
        parts.append(contact.location)
    for label, url in contact.links.items():
        parts.append(f"{label}: {url}")
    return " | ".join(_esc(p) for p in parts)


def build_resume_html(resume: GeneratedResume, contact: Contact) -> str:
    """Render the structured résumé + contact into self-contained HTML.

    Pure function — no I/O. ``out_path`` writing happens in
    :func:`render_resume_pdf`; this is split out so tests can assert on the HTML
    shape without spinning up a browser.
    """
    name = _esc(contact.name or "Candidate")
    contact_line = _format_contact_line(contact)

    summary_block = (
        f"<h2>Summary</h2><p>{_esc(resume.summary)}</p>" if resume.summary else ""
    )

    skills_block = ""
    if resume.skills:
        skills_html = ", ".join(_esc(s) for s in resume.skills)
        skills_block = f'<h2>Skills</h2><div class="skills">{skills_html}</div>'

    experience_blocks: list[str] = []
    for w in resume.work:
        bullets = "".join(f"<li>{_esc(b)}</li>" for b in w.bullets)
        dates = ""
        if w.start or w.end:
            dates = f"{_esc(w.start)} - {_esc(w.end or 'Present')}"
        experience_blocks.append(
            f"<h3>{_esc(w.title)} - {_esc(w.company)}</h3>"
            f'<p class="role-meta">{dates}</p>'
            f"<ul>{bullets}</ul>"
        )
    experience_block = ""
    if experience_blocks:
        experience_block = "<h2>Experience</h2>" + "".join(experience_blocks)

    education_blocks: list[str] = []
    for e in resume.education:
        education_blocks.append(
            f"<p><strong>{_esc(e.institution)}</strong> - {_esc(e.degree)}</p>"
        )
    education_block = ""
    if education_blocks:
        education_block = "<h2>Education</h2>" + "".join(education_blocks)

    return _HTML_TEMPLATE.format(
        name=name,
        contact=contact_line,
        summary_block=summary_block,
        skills_block=skills_block,
        experience_block=experience_block,
        education_block=education_block,
    )


async def render_resume_pdf(html_content: str, out_path: Path) -> bool:
    """Render ``html_content`` to ``out_path`` via Playwright Chromium.

    Returns ``True`` on success, ``False`` on any failure (caller fail-closes).
    The browser context is ephemeral — we don't reuse the apply session's browser
    because PDF rendering is unrelated to the apply flow and headless is fine.

    Default seam used by the optimize worker. Tests inject a different
    :data:`PdfRenderer` so they don't have to spin up Chromium.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed - cannot render résumé PDF")
        return False

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(html_content, wait_until="domcontentloaded")
            await page.pdf(
                path=str(out_path),
                format="Letter",
                margin={"top": "0.5in", "bottom": "0.5in",
                        "left": "0.5in", "right": "0.5in"},
                print_background=True,
            )
            await context.close()
            await browser.close()
        return True
    except Exception as exc:  # noqa: BLE001 — return-false-on-any-failure is the contract
        logger.warning("Résumé PDF render failed: %s", exc)
        return False
