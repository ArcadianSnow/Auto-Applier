"""Per-job cover-letter upload model + the shared attach helper (BUILD 1.1, spec §6b/§8c).

Covers:
  * ``assign_cover_letter`` — copies a hand-authored letter into the job folder under the
    GENERIC basename ``Cover Letter<ext>`` (anti-detection: the ATS only ever sees a generic
    name, never the per-posting source filename), preserving content + extension, replacing
    any prior assignment.
  * ``existing_job_cover`` — the per-job lookup the apply worker uses (file existence =
    "assigned"); ``None`` when nothing is assigned.
  * ``archive_cover_letter`` — moves a confirmed-used letter to ``uploads/_archive`` with the
    job id appended; ``None`` when there's nothing to archive.
  * ``attach_cover_letter`` — defensive native file upload (absent input / error → False).
"""

from __future__ import annotations

import asyncio

from auto_applier.resume.generate import (
    archive_cover_letter,
    archive_resume,
    assign_cover_letter,
    assign_resume,
    existing_job_cover,
    existing_job_resume,
    job_cover_upload_path,
    job_resume_upload_path,
)
from auto_applier.sources.browser.apply_base import attach_cover_letter


# --------------------------------------------------------------- per-job assign / lookup

def _src(tmp_path, name="CoverLetter_Tailscale_SE_Commercial.docx", body="Dear Tailscale"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_none_when_unassigned(settings):
    assert existing_job_cover(settings, "job-1") is None


def test_assign_copies_under_generic_name(settings, tmp_path):
    src = _src(tmp_path)
    dest = assign_cover_letter(settings, "job-1", src)

    # Uploaded basename is generic (NOT the per-posting source name) — the anti-detection point.
    assert dest.name == "Cover Letter.docx"
    assert dest == job_cover_upload_path(settings, "job-1", ".docx")
    assert dest.parent == settings.uploads_dir / "job-1"
    # Content preserved.
    assert dest.read_text(encoding="utf-8") == "Dear Tailscale"
    # And the lookup the worker uses finds it.
    assert existing_job_cover(settings, "job-1") == dest


def test_assign_preserves_extension(settings, tmp_path):
    src = _src(tmp_path, name="letter.pdf", body="pdf-bytes")
    dest = assign_cover_letter(settings, "job-2", src)
    assert dest.name == "Cover Letter.pdf"
    assert existing_job_cover(settings, "job-2") == dest


def test_reassign_replaces_prior_even_across_extension(settings, tmp_path):
    first = assign_cover_letter(settings, "job-3", _src(tmp_path, name="a.docx", body="first"))
    assert first.exists()
    # Re-assign with a DIFFERENT extension — the old one must be gone (exactly one cover).
    second = assign_cover_letter(settings, "job-3", _src(tmp_path, name="b.pdf", body="second"))
    assert second.name == "Cover Letter.pdf"
    assert not first.exists()
    folder = settings.uploads_dir / "job-3"
    covers = [p for p in folder.iterdir() if p.stem == "Cover Letter"]
    assert len(covers) == 1 and covers[0] == second


def test_assign_missing_source_raises(settings, tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        assign_cover_letter(settings, "job-4", tmp_path / "nope.docx")


def test_per_job_isolation(settings, tmp_path):
    a = assign_cover_letter(settings, "job-A", _src(tmp_path, name="a.docx", body="A"))
    b = assign_cover_letter(settings, "job-B", _src(tmp_path, name="b.docx", body="B"))
    assert a != b
    assert existing_job_cover(settings, "job-A").read_text(encoding="utf-8") == "A"
    assert existing_job_cover(settings, "job-B").read_text(encoding="utf-8") == "B"


# --------------------------------------------------------------- archive

def test_archive_moves_and_appends_job_id(settings, tmp_path):
    assign_cover_letter(settings, "job-5", _src(tmp_path, body="keep me"))
    dest = archive_cover_letter(settings, "job-5")

    assert dest is not None
    assert dest.parent == settings.uploads_dir / "_archive"
    assert dest.name == "Cover Letter - job-5.docx"
    assert dest.read_text(encoding="utf-8") == "keep me"
    # Live copy is gone (moved, not copied).
    assert existing_job_cover(settings, "job-5") is None


def test_archive_none_when_nothing_assigned(settings):
    assert archive_cover_letter(settings, "job-6") is None


# --------------------------------------------------------------- résumé (same mechanism)

def test_resume_assign_uses_generic_basename(settings, tmp_path):
    src = tmp_path / "Joseph_Lira_Resume_Solutions_Engineer.docx"
    src.write_text("RESUME", encoding="utf-8")
    dest = assign_resume(settings, "job-r1", src)

    assert dest.name == "Resume.docx"  # NOT the per-posting source name
    assert dest == job_resume_upload_path(settings, "job-r1", ".docx")
    assert existing_job_resume(settings, "job-r1") == dest
    assert dest.read_text(encoding="utf-8") == "RESUME"


def test_resume_prefers_pdf_on_lookup(settings, tmp_path):
    # Assigned a .pdf; lookup returns it (.pdf is first in the résumé ext order).
    src = tmp_path / "r.pdf"
    src.write_text("pdf", encoding="utf-8")
    dest = assign_resume(settings, "job-r2", src)
    assert dest.name == "Resume.pdf"
    assert existing_job_resume(settings, "job-r2") == dest


def test_resume_archive_moves_and_appends_job_id(settings, tmp_path):
    src = tmp_path / "r.pdf"
    src.write_text("keep", encoding="utf-8")
    assign_resume(settings, "job-r3", src)
    dest = archive_resume(settings, "job-r3")

    assert dest is not None
    assert dest.parent == settings.uploads_dir / "_archive"
    assert dest.name == "Resume - job-r3.pdf"
    assert existing_job_resume(settings, "job-r3") is None


def test_resume_and_cover_coexist_in_same_job_folder(settings, tmp_path):
    cov = tmp_path / "c.docx"; cov.write_text("C", encoding="utf-8")
    res = tmp_path / "r.pdf"; res.write_text("R", encoding="utf-8")
    assign_cover_letter(settings, "job-rc", cov)
    assign_resume(settings, "job-rc", res)

    # Distinct stems → both resolve independently, no collision.
    assert existing_job_cover(settings, "job-rc").name == "Cover Letter.docx"
    assert existing_job_resume(settings, "job-rc").name == "Resume.pdf"


# --------------------------------------------------------------- name-prefixed basenames

def test_name_prefix_on_cover_and_resume(settings, tmp_path):
    cov = tmp_path / "CoverLetter_Tailscale_SE_Commercial.docx"; cov.write_text("C", encoding="utf-8")
    res = tmp_path / "Joseph_Lira_Resume_Solutions_Engineer.pdf"; res.write_text("R", encoding="utf-8")

    cdest = assign_cover_letter(settings, "job-n", cov, name="Joseph Lira")
    rdest = assign_resume(settings, "job-n", res, name="Joseph Lira")

    assert cdest.name == "Joseph Lira Cover Letter.docx"
    assert rdest.name == "Joseph Lira Resume.pdf"
    # Lookup finds name-prefixed files (glob on the stem, decoupled from the prefix).
    assert existing_job_cover(settings, "job-n") == cdest
    assert existing_job_resume(settings, "job-n") == rdest


def test_name_prefixed_replaced_on_reassign(settings, tmp_path):
    src1 = tmp_path / "a.pdf"; src1.write_text("1", encoding="utf-8")
    first = assign_resume(settings, "job-n2", src1, name="Joseph Lira")
    assert first.name == "Joseph Lira Resume.pdf"
    # Re-assign (even with no name) clears the name-prefixed prior → exactly one résumé.
    src2 = tmp_path / "b.docx"; src2.write_text("2", encoding="utf-8")
    second = assign_resume(settings, "job-n2", src2)
    assert not first.exists()
    folder = settings.uploads_dir / "job-n2"
    resumes = list(folder.glob("*Resume.*"))
    assert len(resumes) == 1 and resumes[0] == second


def test_name_prefixed_archived_keeps_name(settings, tmp_path):
    src = tmp_path / "r.pdf"; src.write_text("R", encoding="utf-8")
    assign_resume(settings, "job-n3", src, name="Joseph Lira")
    dest = archive_resume(settings, "job-n3")
    assert dest is not None
    assert dest.name == "Joseph Lira Resume - job-n3.pdf"


def test_safe_name_strips_illegal_chars(settings, tmp_path):
    src = tmp_path / "r.pdf"; src.write_text("R", encoding="utf-8")
    # Illegal Windows filename chars in the name must not break the basename.
    dest = assign_resume(settings, "job-n4", src, name='Joseph "JL" Lira/Jr')
    assert dest.name == "Joseph JL LiraJr Resume.pdf"
    assert dest.exists()


# --------------------------------------------------------------- attach_cover_letter

class _FileEl:
    def __init__(self, *, raises: bool = False):
        self.files = None
        self._raises = raises

    async def set_input_files(self, path):
        if self._raises:
            raise RuntimeError("upload failed")
        self.files = path


class _Page:
    def __init__(self, el):
        self._el = el
        self.queried = None

    async def query_selector(self, selector):
        self.queried = selector
        return self._el


def test_attach_cover_letter_uploads_when_present():
    el = _FileEl()
    page = _Page(el)
    ok = asyncio.run(attach_cover_letter(page, "#cover_letter", "/tmp/letter.docx"))
    assert ok is True
    assert el.files == "/tmp/letter.docx"
    assert page.queried == "#cover_letter"


def test_attach_cover_letter_false_when_input_absent():
    page = _Page(None)
    assert asyncio.run(attach_cover_letter(page, "#cover_letter", "/tmp/letter.docx")) is False


def test_attach_cover_letter_empty_path_short_circuits():
    page = _Page(_FileEl())
    assert asyncio.run(attach_cover_letter(page, "#cover_letter", "")) is False
    assert page.queried is None  # never even queried the DOM


def test_attach_cover_letter_swallows_upload_error():
    page = _Page(_FileEl(raises=True))
    assert asyncio.run(attach_cover_letter(page, "#cover_letter", "/tmp/x.docx")) is False
