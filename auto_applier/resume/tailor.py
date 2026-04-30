"""Per-JD tailored resume generation.

Given a stored job and a base resume, produce a rewritten version
that emphasizes the material most relevant to that specific listing,
render it through a simple HTML template, and save the result as a
PDF under ``data/profiles/generated/<job_id>.pdf``.

The original resume files in ``data/resumes/`` are **never**
modified — that constraint is load-bearing for the whole project.
Tailored PDFs are generated artifacts, not edits.

PDF rendering uses the same Playwright Chromium we already require
for scraping. No new dependencies. The FormFiller's resume-upload
path should prefer a tailored version when one exists for the
target job_id.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from auto_applier.config import GENERATED_RESUMES_DIR
from auto_applier.llm.prompts import TAILOR_RESUME
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass
class TailoredResume:
    summary: str
    skills: list[str]
    experience: list[dict]  # each: {title, company, dates, bullets}
    education: list[dict]   # each: {school, degree, year}
    job_id: str = ""
    source_resume_label: str = ""


# Minimal ATS-friendly HTML template. No fancy CSS, no web fonts,
# no images — just clean semantic structure that parses well in
# resume trackers and prints to a readable single column.
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
<h2>Summary</h2>
<p>{summary}</p>
<h2>Skills</h2>
<div class="skills">{skills}</div>
<h2>Experience</h2>
{experience}
<h2>Education</h2>
{education}
</body></html>
"""


def render_html(tailored: TailoredResume, name: str = "", contact: str = "") -> str:
    """Render a TailoredResume to self-contained HTML."""
    def _esc(s: str) -> str:
        return html.escape(str(s or ""))

    skills_html = ", ".join(_esc(s) for s in tailored.skills)
    experience_blocks = []
    for job in tailored.experience:
        bullets = "".join(f"<li>{_esc(b)}</li>" for b in job.get("bullets", []))
        experience_blocks.append(
            f'<h3>{_esc(job.get("title", ""))} — {_esc(job.get("company", ""))}</h3>'
            f'<p class="role-meta">{_esc(job.get("dates", ""))}</p>'
            f'<ul>{bullets}</ul>'
        )
    experience_html = "\n".join(experience_blocks) or "<p><em>No experience listed.</em></p>"

    education_blocks = []
    for edu in tailored.education:
        education_blocks.append(
            f'<p><strong>{_esc(edu.get("school", ""))}</strong> — '
            f'{_esc(edu.get("degree", ""))} '
            f'<span class="role-meta">({_esc(edu.get("year", ""))})</span></p>'
        )
    education_html = "\n".join(education_blocks) or "<p><em>No education listed.</em></p>"

    return _HTML_TEMPLATE.format(
        name=_esc(name or "Candidate"),
        contact=_esc(contact),
        summary=_esc(tailored.summary),
        skills=skills_html,
        experience=experience_html,
        education=education_html,
    )


class ResumeTailor:
    """Rewrites a resume for a specific job description via the LLM router."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def tailor(
        self,
        resume_text: str,
        job_description: str,
        company_name: str,
        job_title: str,
        job_id: str = "",
        resume_label: str = "",
    ) -> TailoredResume | None:
        """Produce a TailoredResume, or None on LLM failure.

        Validates that the response has at least a summary and some
        skills — a shell response with empty fields would produce
        a useless PDF and is rejected.
        """
        try:
            result = await self.router.complete_json(
                prompt=TAILOR_RESUME.format(
                    resume_text=resume_text[:4000],
                    job_description=job_description[:2000],
                    company_name=company_name,
                    job_title=job_title,
                ),
                system_prompt=TAILOR_RESUME.system,
            )
        except Exception as e:
            logger.debug("Resume tailor LLM call failed: %s", e)
            return None

        summary = str(result.get("summary", "")).strip()
        skills = result.get("skills", [])
        if not summary or not isinstance(skills, list) or not skills:
            logger.debug("Tailor response too sparse, discarding")
            return None

        experience = result.get("experience", [])
        if not isinstance(experience, list):
            experience = []
        education = result.get("education", [])
        if not isinstance(education, list):
            education = []

        return TailoredResume(
            summary=summary,
            skills=[str(s) for s in skills if str(s).strip()],
            experience=[e for e in experience if isinstance(e, dict)],
            education=[e for e in education if isinstance(e, dict)],
            job_id=job_id,
            source_resume_label=resume_label,
        )


async def render_pdf(html_content: str, out_path: Path) -> bool:
    """Render HTML to a PDF at ``out_path`` via Playwright Chromium.

    Returns True on success, False on any failure. The browser
    context is ephemeral — we don't reuse the main scraping context
    because PDF rendering happens outside a platform flow.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — cannot render PDF")
        return False

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(html_content, wait_until="domcontentloaded")
            out_path.parent.mkdir(parents=True, exist_ok=True)
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
    except Exception as e:
        logger.warning("PDF render failed: %s", e)
        return False


def _user_filename_prefix() -> str:
    """Build a 'First_Last' or 'First' filename prefix from user_config.

    The previous naming pattern was `<job_id>.pdf` (e.g.
    `ind-9934dbc8cae647b8.pdf`) — uploading a file with that name
    is a dead giveaway that the resume was system-generated. Using
    the candidate's actual name reads as if they exported their own
    resume from Word.

    Falls back to "Resume" if user_config.json is missing or has no
    name fields, so the filename is still organic-looking.
    """
    try:
        import json
        from auto_applier.config import USER_CONFIG_FILE
        if USER_CONFIG_FILE.exists():
            data = json.loads(USER_CONFIG_FILE.read_text(encoding="utf-8"))
            personal = data.get("personal_info", {}) or {}
            first = (personal.get("first_name") or "").strip()
            last = (personal.get("last_name") or "").strip()
            if first and last:
                return f"{_clean_name(first)}_{_clean_name(last)}"
            if first:
                return _clean_name(first)
            full = (personal.get("name") or "").strip()
            if full:
                return _clean_name(full).replace(" ", "_")
    except Exception:
        pass
    return ""


def _clean_name(name: str) -> str:
    """Strip filesystem-unfriendly chars from a name."""
    return "".join(c for c in name if c.isalnum() or c in "_- ").strip()


def tailored_pdf_path(job_id: str) -> Path:
    """Return the canonical path for a tailored PDF for a job.

    Layout: ``<GENERATED_RESUMES_DIR>/<job_id>/<First>_<Last>_Resume.pdf``.
    The job_id stays in the path (we need a stable lookup key) but
    sits in the *directory* layer, not the filename — so the basename
    that gets uploaded to a job board is ``Jordan_Testpilot_Resume.pdf``,
    not ``ind-9934dbc8cae647b8.pdf``.
    """
    # Sanitize job_id for filesystem safety
    safe_job = "".join(c if c.isalnum() or c in "-_." else "_" for c in job_id)
    prefix = _user_filename_prefix()
    filename = f"{prefix}_Resume.pdf" if prefix else "Resume.pdf"
    return GENERATED_RESUMES_DIR / safe_job / filename


def save_tailored_json(tailored: TailoredResume) -> Path:
    """Persist the structured TailoredResume next to the PDF."""
    from dataclasses import asdict

    path = tailored_pdf_path(tailored.job_id).with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(tailored), indent=2), encoding="utf-8",
    )
    return path
