"""Manual cover-letter library + the shared attach helper (BUILD 1, spec §6b).

Covers:
  * ``manual_cover_letter_path`` — index.json match, fuzzy filename match, .docx
    preference on ties, company-suffix/slug normalization, and the no-match → None path
    (the manual-queue apply path maps job.company → a hand-authored letter file).
  * ``attach_cover_letter`` — uploads via a native file input, defensively (absent input
    or upload error → False, never raises; an empty path → False with no query).
"""

from __future__ import annotations

import asyncio

from auto_applier.resume.generate import _cover_slug, manual_cover_letter_path
from auto_applier.sources.browser.apply_base import attach_cover_letter


# --------------------------------------------------------------- manual_cover_letter_path

def _dir(settings):
    d = settings.cover_letters_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_returns_none_when_dir_absent(settings):
    # cover_letters_dir not created → no manual letters configured.
    assert manual_cover_letter_path(settings, "Hightouch") is None


def test_returns_none_for_empty_company(settings):
    _dir(settings)
    assert manual_cover_letter_path(settings, "") is None
    assert manual_cover_letter_path(settings, "   ") is None


def test_fuzzy_filename_match(settings):
    d = _dir(settings)
    (d / "hightouch.docx").write_text("letter", encoding="utf-8")
    (d / "tailscale.docx").write_text("letter", encoding="utf-8")

    got = manual_cover_letter_path(settings, "Hightouch, Inc.")
    assert got == d / "hightouch.docx"


def test_fuzzy_match_strips_cover_letter_prefix(settings):
    d = _dir(settings)
    (d / "cover-letter-grafana.docx").write_text("x", encoding="utf-8")
    assert manual_cover_letter_path(settings, "Grafana Labs") == d / "cover-letter-grafana.docx"


def test_index_json_overrides_fuzzy(settings):
    d = _dir(settings)
    # A file whose stem does NOT match the company; only the index maps it.
    (d / "ht-final-v2.docx").write_text("x", encoding="utf-8")
    (d / "index.json").write_text('{"Hightouch": "ht-final-v2.docx"}', encoding="utf-8")

    assert manual_cover_letter_path(settings, "hightouch") == d / "ht-final-v2.docx"


def test_index_json_missing_file_falls_through_to_fuzzy(settings):
    d = _dir(settings)
    (d / "hightouch.docx").write_text("x", encoding="utf-8")
    # index points at a file that doesn't exist → ignore it, fall back to fuzzy.
    (d / "index.json").write_text('{"hightouch": "does-not-exist.docx"}', encoding="utf-8")

    assert manual_cover_letter_path(settings, "Hightouch") == d / "hightouch.docx"


def test_prefers_docx_over_pdf_on_tie(settings):
    d = _dir(settings)
    (d / "acme.pdf").write_text("x", encoding="utf-8")
    (d / "acme.docx").write_text("x", encoding="utf-8")
    assert manual_cover_letter_path(settings, "Acme") == d / "acme.docx"


def test_no_match_returns_none(settings):
    d = _dir(settings)
    (d / "hightouch.docx").write_text("x", encoding="utf-8")
    assert manual_cover_letter_path(settings, "Snowflake") is None


def test_malformed_index_json_falls_through(settings):
    d = _dir(settings)
    (d / "acme.docx").write_text("x", encoding="utf-8")
    (d / "index.json").write_text("{not valid json", encoding="utf-8")
    assert manual_cover_letter_path(settings, "Acme") == d / "acme.docx"


def test_cover_slug_normalization():
    assert _cover_slug("Hightouch, Inc.") == "hightouch"
    assert _cover_slug("cover-letter-grafana") == "grafana"
    assert _cover_slug("Acme LLC") == "acme"
    assert _cover_slug("") == ""


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
    # A failed upload is observable (False), never fatal.
    assert asyncio.run(attach_cover_letter(page, "#cover_letter", "/tmp/x.docx")) is False
