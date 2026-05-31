"""Mocked-source selector-drift CI (spec section 10 + 11b Phase 3 (8/M)).

> "Recorded HTML fixtures drive form-filler + answer-resolver unit tests; CI
>  runs against mocked sources; scheduled live smoke tests against real sites
>  catch selector drift early (the #1 v2 bug source)."

This is the **fast CI gate** for selector drift. Live smoke tests ((9/M)) hit
real ATS sites and catch outright structural drift; the mocked tests here
catch the more specific "the selectors our drivers depend on are still in the
fixture" regression. When an ATS visibly changes its form (rare), the user
runs ``scripts/refresh_fixtures.py`` to capture the new shape and re-pin.

Why fixture-based rather than driver-replay:
  * Driver replay would require a full async ``Page`` stand-in that runs the
    *full* prepare_application flow against the saved HTML — large surface,
    duplicates the per-driver tests in ``test_lever_apply.py`` /
    ``test_greenhouse_apply.py`` / ``test_ashby_apply.py``.
  * Selector-resolution is what actually drifts on real ATSes (an id getting
    renamed, a class shape changing). Asserting "every selector our driver
    depends on resolves at least one element in the current fixture" catches
    that drift directly, without re-testing flow logic.
  * Uses Python stdlib ``html.parser`` — no BeautifulSoup dep just for CI.

What this test catches:
  * Standard-field selector no longer resolves (e.g. ``#first_name`` renamed).
  * Resume input selector drifts.
  * Submit button selector drifts.
  * CAPTCHA carrier present (so the dispatch can classify).
  * At least one custom question is discoverable.

What this test does NOT catch:
  * The actual API/site changed but the fixture is stale (only the live smoke
    test (9/M) catches that). When the fixture goes stale, this test still
    passes — refresh via ``scripts/refresh_fixtures.py``.
  * Behavioral changes (e.g. submit no longer redirects to /thanks). Confirmation
    detection is tested via its own unit tests.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------- parser

class _ElementCollector(HTMLParser):
    """Collect lightweight (tag, attrs) tuples so we can run CSS-like queries
    without a full DOM. Sufficient for "does selector X resolve to any element?"
    """

    def __init__(self):
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        self.elements.append((tag, attr_dict))


def _parse(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = _ElementCollector()
    parser.feed(html)
    return parser.elements


# --------------------------------------------------------------- selector helpers

def _has_id(elements: list[tuple[str, dict[str, str]]], element_id: str) -> bool:
    return any(attrs.get("id") == element_id for _, attrs in elements)


def _has_name(elements: list[tuple[str, dict[str, str]]], name: str) -> bool:
    return any(attrs.get("name") == name for _, attrs in elements)


def _has_name_starting(elements: list[tuple[str, dict[str, str]]], prefix: str) -> bool:
    return any(attrs.get("name", "").startswith(prefix) for _, attrs in elements)


def _has_tag_with_attr(
    elements: list[tuple[str, dict[str, str]]],
    tag: str,
    attr: str,
    value: str,
) -> bool:
    return any(
        t == tag and attrs.get(attr) == value
        for t, attrs in elements
    )


def _load_fixture(ats: str, name: str = "apply_form.html") -> list[tuple[str, dict[str, str]]]:
    path = FIXTURES_DIR / ats / name
    assert path.exists(), f"fixture missing: {path} (run scripts/refresh_fixtures.py)"
    html = path.read_text(encoding="utf-8")
    return _parse(html)


# --------------------------------------------------------------- greenhouse

def test_greenhouse_standard_field_selectors():
    """Greenhouse driver uses ``#first_name``, ``#last_name``, ``#email``,
    ``#phone``, ``#resume``, ``button[type=submit]``. All must resolve.
    Drift in any of these breaks the apply path."""
    els = _load_fixture("greenhouse")
    assert _has_id(els, "first_name"), "GH: #first_name missing - apply driver depends on this"
    assert _has_id(els, "last_name"), "GH: #last_name missing"
    assert _has_id(els, "email"), "GH: #email missing"
    assert _has_id(els, "phone"), "GH: #phone missing"
    assert _has_id(els, "resume"), "GH: #resume missing - resume upload would fail"
    assert _has_tag_with_attr(els, "button", "type", "submit"), (
        "GH: no <button type=submit> - submit would fail"
    )


def test_greenhouse_captcha_carrier_present():
    """Greenhouse uses reCAPTCHA (frequently Enterprise). The ``g-recaptcha-response``
    textarea is the canonical detection signal for the classifier."""
    els = _load_fixture("greenhouse")
    assert _has_name(els, "g-recaptcha-response"), (
        "GH: g-recaptcha-response missing - CAPTCHA classifier won't detect"
    )


def test_greenhouse_custom_question_pattern():
    """Greenhouse custom questions use ``#question_<numeric>`` ids. Drift here
    means the driver's custom-Q discovery returns empty even when questions
    exist."""
    els = _load_fixture("greenhouse")
    custom_q_ids = [
        attrs["id"] for _, attrs in els
        if attrs.get("id", "").startswith("question_")
    ]
    assert len(custom_q_ids) >= 1, (
        "GH: no #question_* ids found - custom-Q discovery would return empty"
    )


# --------------------------------------------------------------- lever

def test_lever_standard_field_selectors():
    """Lever driver uses ``input[name='name']``, ``[name='email']``,
    ``[name='phone']``, ``[name='org']``, ``#resume-upload-input``,
    ``input[name='resumeStorageId']`` (parse-wait signal), and ``#btn-submit``."""
    els = _load_fixture("lever")
    assert _has_name(els, "name"), "Lever: input[name='name'] missing"
    assert _has_name(els, "email"), "Lever: input[name='email'] missing"
    assert _has_name(els, "phone"), "Lever: input[name='phone'] missing"
    assert _has_name(els, "org"), "Lever: input[name='org'] missing"
    assert _has_id(els, "resume-upload-input"), (
        "Lever: #resume-upload-input missing - resume upload would fail"
    )
    assert _has_name(els, "resumeStorageId"), (
        "Lever: resumeStorageId input missing - parse-wait would never settle"
    )
    assert _has_id(els, "btn-submit"), "Lever: #btn-submit missing"


def test_lever_captcha_carrier_present():
    """Lever uses hCaptcha. The ``h-captcha-response`` textarea is the canonical
    detection signal."""
    els = _load_fixture("lever")
    assert _has_name(els, "h-captcha-response"), (
        "Lever: h-captcha-response missing - CAPTCHA classifier won't detect"
    )


def test_lever_custom_question_pattern():
    """Lever custom questions use ``[name='cards[<uuid>][field0]']``. UUIDs vary
    per posting; we check for the ``cards[`` prefix."""
    els = _load_fixture("lever")
    assert _has_name_starting(els, "cards["), (
        "Lever: no [name^='cards['] inputs - custom-Q discovery would return empty"
    )


def test_lever_eeo_section_discoverable():
    """Lever EEO fields are name-keyed (``eeo[gender]``, etc.). The driver
    discovers them via the same walker as cards[*]."""
    els = _load_fixture("lever")
    assert _has_name_starting(els, "eeo["), (
        "Lever: no [name^='eeo['] inputs - EEO discovery would miss them"
    )


# --------------------------------------------------------------- ashby

def test_ashby_standard_field_selectors():
    """Ashby driver uses ``#_systemfield_name``, ``#_systemfield_email``,
    ``#_systemfield_resume``. No `<form>` wrapper - submit is a non-form
    ``<button type=submit>`` (React onClick -> XHR)."""
    els = _load_fixture("ashby")
    assert _has_id(els, "_systemfield_name"), "Ashby: #_systemfield_name missing"
    assert _has_id(els, "_systemfield_email"), "Ashby: #_systemfield_email missing"
    assert _has_id(els, "_systemfield_resume"), (
        "Ashby: #_systemfield_resume missing - resume upload would fail"
    )
    assert _has_tag_with_attr(els, "button", "type", "submit"), (
        "Ashby: no <button type=submit> - submit would fail (SPA dispatch)"
    )


def test_ashby_captcha_carrier_present():
    """Ashby uses invisible reCAPTCHA (sometimes Enterprise on the harder half)."""
    els = _load_fixture("ashby")
    assert _has_name(els, "g-recaptcha-response"), (
        "Ashby: g-recaptcha-response missing - CAPTCHA classifier won't detect"
    )


def test_ashby_custom_questions_are_uuid_named():
    """Ashby custom questions are UUID-named (NOT ``_systemfield_*``). The
    driver discovers them by *excluding* the ``_systemfield_`` prefix +
    CAPTCHA carriers from all input/select/textarea. Drift here means the
    driver either misses custom Qs or mis-classifies system fields as custom."""
    import re

    els = _load_fixture("ashby")
    # Find any id matching the UUID pattern (loose check — hex with dashes).
    uuid_re = re.compile(r"^[0-9a-f]{4,}-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+$", re.I)
    custom_uuids = [
        attrs["id"] for _, attrs in els
        if uuid_re.match(attrs.get("id", ""))
    ]
    assert len(custom_uuids) >= 1, (
        "Ashby: no UUID-named inputs - custom-Q discovery would return empty"
    )

    # And the system-field exclusion must hold: NO _systemfield_* should
    # be in the UUID-matched list (sanity that the discovery exclusion is sound).
    assert not any(u.startswith("_systemfield_") for u in custom_uuids), (
        "Ashby: a _systemfield_* input matched the UUID pattern - "
        "discovery exclusion logic would break"
    )


# --------------------------------------------------------------- meta

def test_all_fixtures_load_and_are_non_empty():
    """Every per-ATS fixture file must exist and parse to non-empty element list.
    Cheap sanity that the refresh script wrote the right files."""
    for ats in ("greenhouse", "lever", "ashby"):
        els = _load_fixture(ats)
        assert len(els) > 0, f"{ats}: fixture parsed to empty element list"
