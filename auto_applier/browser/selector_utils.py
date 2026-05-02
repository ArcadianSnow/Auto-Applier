"""Platform-agnostic form field detection utilities.

Detects form fields on any job application page using four strategies:
1. ``label[for]`` -> ``input[id]`` pairing
2. Form-group containers (label + input as siblings/children)
3. ``aria-label`` on input elements
4. ``placeholder`` text as label fallback

Deduplicates by label text to avoid filling the same field twice.
"""
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import ElementHandle, Page

logger = logging.getLogger(__name__)


# Labels that look like questions but aren't — they're page chrome,
# section headers, file-upload affordance text, or accessibility helper
# text we should never try to LLM-answer. Matched as a substring (after
# lowercasing the detected label) so minor wording variants still hit.
# User explicitly called out the first three from a dry-run audit; the
# rest were observed in run logs as "filled" with junk answers.
_PHANTOM_LABEL_PATTERNS = (
    "voluntary self identification",
    "voluntary self-identification",
    "self-identification questions",
    "self identification questions",
    "upload a file",
    "drag and drop",
    "drag & drop",
    "drop your file here",
    "current page",
    "page navigation",
    "powered by",
    "click to upload",
    "browse files",
    "choose file",
    "no file chosen",
    "accepted formats",
    "max size",
    "maximum file size",
)


def _clean_compound_label(raw: str) -> str:
    """Reduce a multi-line wrapper-derived label to its actionable line.

    When a single ``<label>`` wraps an entire fieldset (Indeed's
    questions module pattern), ``inner_text()`` returns multiple
    lines stacked together — section heading, helper paragraph,
    actual question, asterisk, options. The actual question is
    almost always the LAST non-empty line.

    Live-run example that motivated this helper::

        "Mobile Number\\n\\nProvide valid phone numbers to allow \\n\\n
         Recruiters to contact you.\\n\\nCountry *"

    The element was a SELECT with country options; the LLM saw
    "Mobile Number" first and answered with a phone number. Cleaning
    the label to just "Country *" gets the right answer the first
    time.

    Conservative — only kicks in when there's an actual newline AND
    multiple non-empty lines. Single-line labels untouched. If the
    last line is bare punctuation chrome (``*``, ``(required)``),
    fall back to the line before it.
    """
    if not raw or "\n" not in raw:
        return raw
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return raw
    last = lines[-1]
    # Skip pure chrome lines (`*`, `(required)`, `required`).
    chrome_pattern = re.compile(
        r"^[\*\s]+$|^\(required\)$|^required$",
        re.IGNORECASE,
    )
    while lines and chrome_pattern.match(last):
        lines.pop()
        if not lines:
            break
        last = lines[-1]
    if not lines:
        return raw
    return last


def _is_phantom_label(label: str) -> bool:
    """Return True for labels that look like form chrome, not a question.

    Conservative — only patterns we've seen produce nonsense answers.
    Real questions occasionally contain phrases like ``upload`` (e.g.
    "Upload your resume — required"), so we only block when the label
    is *dominated* by chrome text. The two-pronged rule:

      1. The label substring-matches a phantom pattern, AND
      2. The label is short (<= 80 chars) — long labels usually wrap a
         real question around any upload-flavoured noise.
    """
    if not label:
        return True
    s = label.strip().lower()
    if not s:
        return True
    if len(s) > 80:
        return False
    # Pure punctuation / digits-only stays out of the form filler.
    if not re.search(r"[a-z]", s):
        return True
    for pat in _PHANTOM_LABEL_PATTERNS:
        if pat in s:
            return True
    return False


# Minimum length for a "real" question. Anything shorter is fragment
# chrome ("X", "Yes", "*") that slipped past the phantom filter.
# "Country *" is 9 chars, so 8 is a safe lower bound.
_MIN_QUESTION_LEN = 8


# Markers that ONLY appear in LLM prompt bodies, not real form-field
# labels. If any of these appear, the "question" is actually a leaked
# prompt context and must NOT be persisted to unanswered.json.
# Live audit 2026-05-02 found a 1900-char "question" containing
# "Resume:" + the candidate's full resume + "Job description:" + part
# of a JD — the form_filler had passed the LLM prompt instead of the
# field label to _record_unanswered. Defensive cap at the storage
# layer so the bug can't pollute data even if the upstream caller
# mis-passes again.
_PROMPT_LEAK_MARKERS = (
    "resume:\n", "resume:\r",
    "job description:\n", "job description:\r",
    "candidate profile:",
    "applicant resume:",
    "you are answering",
    "you are filling",
    "respond only with",
    "respond ONLY with",
    "json shape",
    "available options",
    "system_prompt",
    "candidate's resume",
)


# Question categories the user explicitly rejected from the wizard's
# unanswered queue: referral / "how did you hear about" variants.
# These are per-application context (the form_filler answers them
# at run time via SOURCE_QUESTION_KEYWORDS using the platform
# display name), not gaps the user can usefully fill in advance.
_USER_REJECTED_FRAGMENTS = (
    "referred by an employee",
    "referred by a current",
    "employee referral",
    "how did you hear about",
    "where did you hear about",
    "how did you find this",
    "where did you find this",
    "referral source",
)


def _load_answers_keys(answers_path) -> tuple[set[str], set[str]]:
    """Return (exact_keys_lower, exact_keys_lower) for answers.json.

    Tolerates the three historical shapes (flat dict, list of entry
    dicts, ``{"questions": [...]}`` wrapper) and returns a set of
    lower-cased question keys. Returns an empty set on any failure
    so callers stay defensive — better to record a duplicate than to
    crash the apply path.

    The duplicated return is intentional: callers want one set for
    exact matching and the *same* set for case-insensitive substring
    matching. Kept as a tuple for forward extensibility.
    """
    from pathlib import Path

    p = Path(answers_path)
    if not p.exists():
        return set(), set()
    try:
        import json as _json
        with open(p, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
    except (OSError, ValueError):
        return set(), set()

    keys: set[str] = set()
    if isinstance(data, dict) and "questions" not in data:
        for k in data.keys():
            if isinstance(k, str) and k.strip():
                keys.add(k.strip().lower())
    elif isinstance(data, dict) and "questions" in data:
        items = data.get("questions", [])
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict):
                    q = entry.get("question", "")
                    if isinstance(q, str) and q.strip():
                        keys.add(q.strip().lower())
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                q = entry.get("question", "")
                if isinstance(q, str) and q.strip():
                    keys.add(q.strip().lower())
            elif isinstance(entry, str) and entry.strip():
                keys.add(entry.strip().lower())
    return keys, keys


def should_skip_unanswered(question: str, answers_path=None) -> bool:
    """Return True if ``question`` should NOT be written to unanswered.json.

    Three rejection rules:

    1. Phantom label — page chrome, headings, upload affordance text
       (delegates to :func:`_is_phantom_label`).
    2. Already in answers.json — exact (case-insensitive) match against
       any existing answer key. Substring match against the trimmed
       question against keys also rejects (catches "Are you 18 years
       of age or older?" appearing both in the answers list and in
       the unanswered queue with trailing punctuation/asterisk).
    3. Too short — less than ``_MIN_QUESTION_LEN`` characters after
       stripping. "X" or "Yes" aren't real questions.

    ``answers_path`` defaults to the configured ``ANSWERS_FILE`` if
    omitted. Pass an explicit path in tests so the helper doesn't
    touch the user's real data.
    """
    if not question:
        return True
    trimmed = question.strip()
    if not trimmed:
        return True
    if len(trimmed) < _MIN_QUESTION_LEN:
        return True
    # Cap — real form labels are rarely longer than a sentence.
    # 2026-05-02 user audit found an entire LLM prompt body (with
    # "Resume:" and "Job description:" sections) leaking into
    # unanswered.json as a single "question" via some upstream
    # call site that handed the prompt instead of the field label.
    # Cap at 300 chars catches it regardless of which caller leaked.
    if len(trimmed) > 300:
        return True
    if _is_phantom_label(trimmed):
        return True

    lowered = trimmed.lower()
    # Prompt-leak markers — substrings that only appear in LLM
    # context blobs, never in real form-field labels.
    for marker in _PROMPT_LEAK_MARKERS:
        if marker in lowered:
            return True
    # User-rejected categories (referral, "how did you hear") —
    # per-application context that the form_filler resolves at
    # run time via SOURCE_QUESTION_KEYWORDS, not gaps the user
    # can answer in advance.
    for fragment in _USER_REJECTED_FRAGMENTS:
        if fragment in lowered:
            return True

    if answers_path is None:
        from auto_applier.config import ANSWERS_FILE
        answers_path = ANSWERS_FILE

    keys, _ = _load_answers_keys(answers_path)
    if not keys:
        return False

    q_lower = trimmed.lower()
    if q_lower in keys:
        return True
    # Case-insensitive substring: the unanswered question contains the
    # answers.json key (e.g. unanswered "Are you 18 years of age or
    # older? *" contains "are you 18 years of age or older?"),
    # OR the answers.json key contains the unanswered question (rare,
    # but covers "Email" key vs "email" question).
    for key in keys:
        if key and (key in q_lower or q_lower in key):
            return True
    return False


@dataclass
class FormField:
    """A detected form field with its label and element handle."""

    label: str
    element: ElementHandle
    field_type: str  # "text", "textarea", "select", "radio", "checkbox", "file"
    options: list[str] = field(default_factory=list)  # For select/radio fields


async def find_form_fields(page: Page) -> list[FormField]:
    """Detect form fields on the current page using multiple strategies.

    Returns a deduplicated list of FormField objects sorted by their
    position in the DOM (top to bottom).
    """
    fields: list[FormField] = []
    seen_labels: set[str] = set()
    try:
        url = page.url
    except Exception:
        url = "?"
    logger.debug("find_form_fields: scanning page url=%s", url)

    # Strategy 1: label[for] -> input[id] pairing
    labels = await page.query_selector_all("label[for]")
    for label_el in labels:
        try:
            label_text = (await label_el.inner_text()).strip()
            for_id = await label_el.get_attribute("for")
            if not for_id or not label_text:
                continue
            input_el = await page.query_selector(f"#{for_id}")
            if input_el and label_text.lower() not in seen_labels:
                f = await _classify_element(input_el, label_text, page)
                if f:
                    fields.append(f)
                    seen_labels.add(label_text.lower())
        except Exception:
            continue

    # Strategy 2: Form-group containers (label + input as siblings)
    container_selectors = (
        ".form-group, .field-group, [class*='form-field'], "
        "[class*='field-container'], [data-testid*='form'], .form-row, "
        ".fb-form-element, .jobs-easy-apply-form-element"
    )
    containers = await page.query_selector_all(container_selectors)
    for container in containers:
        try:
            label_el = await container.query_selector(
                "label, .label, [class*='label'], .fb-form-element-label"
            )
            if not label_el:
                continue
            label_text = (await label_el.inner_text()).strip()
            if not label_text or label_text.lower() in seen_labels:
                continue
            input_el = await container.query_selector(
                "input, textarea, select, [role='combobox'], [role='listbox']"
            )
            if input_el:
                f = await _classify_element(input_el, label_text, page)
                if f:
                    fields.append(f)
                    seen_labels.add(label_text.lower())
        except Exception:
            continue

    # Strategy 3: aria-label on inputs
    aria_inputs = await page.query_selector_all(
        "input[aria-label], textarea[aria-label], select[aria-label]"
    )
    for input_el in aria_inputs:
        try:
            label_text = await input_el.get_attribute("aria-label")
            if not label_text or label_text.strip().lower() in seen_labels:
                continue
            label_text = label_text.strip()
            f = await _classify_element(input_el, label_text, page)
            if f:
                fields.append(f)
                seen_labels.add(label_text.lower())
        except Exception:
            continue

    # Strategy 4: placeholder text as label fallback
    placeholder_inputs = await page.query_selector_all(
        "input[placeholder], textarea[placeholder]"
    )
    for input_el in placeholder_inputs:
        try:
            label_text = await input_el.get_attribute("placeholder")
            if not label_text or label_text.strip().lower() in seen_labels:
                continue
            label_text = label_text.strip()
            f = await _classify_element(input_el, label_text, page)
            if f:
                fields.append(f)
                seen_labels.add(label_text.lower())
        except Exception:
            continue

    # Strategy 5: fieldset/legend pairs (Indeed's screener questions).
    # The question text lives in a <legend>, and the answers live in
    # inputs nested inside the <fieldset>. Radio/checkbox groups also
    # commonly use this pattern when there's no single labelled input.
    fieldsets = await page.query_selector_all("fieldset")
    for fs in fieldsets:
        try:
            legend = await fs.query_selector("legend")
            if not legend:
                continue
            label_text = (await legend.inner_text()).strip()
            if not label_text or label_text.lower() in seen_labels:
                continue
            # Prefer a text/textarea/select if one exists; otherwise
            # the first radio/checkbox stands in for the whole group.
            input_el = await fs.query_selector(
                "input:not([type='hidden']), textarea, select"
            )
            if input_el:
                f = await _classify_element(input_el, label_text, page)
                if f:
                    fields.append(f)
                    seen_labels.add(label_text.lower())
        except Exception:
            continue

    # Strategy 6: role='group' / role='radiogroup' with aria-labelledby.
    # Modern React forms (Indeed's questions-module, LinkedIn Easy
    # Apply) render custom question widgets this way instead of
    # semantic <fieldset>. The label text is referenced by
    # aria-labelledby pointing at a separate element.
    groups = await page.query_selector_all(
        "[role='radiogroup'][aria-labelledby], "
        "[role='group'][aria-labelledby]"
    )
    for group in groups:
        try:
            label_id = await group.get_attribute("aria-labelledby")
            if not label_id:
                continue
            # aria-labelledby can contain space-separated IDs; use
            # only the first one for the CSS selector.
            first_id = label_id.split()[0]
            label_el = await page.query_selector(f"#{first_id}")
            if not label_el:
                continue
            label_text = (await label_el.inner_text()).strip()
            if not label_text or label_text.lower() in seen_labels:
                continue
            input_el = await group.query_selector(
                "input:not([type='hidden']), textarea, select"
            )
            if input_el:
                f = await _classify_element(input_el, label_text, page)
                if f:
                    fields.append(f)
                    seen_labels.add(label_text.lower())
        except Exception:
            continue

    logger.debug("Detected %d form fields on page", len(fields))
    # Dump every detected field's label + type so the run log has
    # a full inventory per page navigation. This is the breadcrumb
    # trail we need when the form filler appears to hang — we can
    # see whether the problem is 'label never detected' vs
    # 'detected but fill failed' vs 'filled but Continue not clicked'.
    for i, f in enumerate(fields):
        opts = f" options={f.options}" if f.options else ""
        logger.debug("  field[%d]: label=%r type=%s%s", i, f.label, f.field_type, opts)
    return fields


# Hint phrases (lowercased substrings) that mark a sibling text input
# as numeric-only. Live-run example: a "Years of experience" field with
# "Numbers only" caption underneath. The form was silently rejected
# when we typed "I have 6 years" because the platform validated the
# field client-side and refused submission.
_NUMERIC_HINT_PHRASES = (
    "numbers only",
    "numeric only",
    "numeric value",
    "numeric values",
    "digits only",
    "must be a number",
    "must be numeric",
    "enter a number",
    "enter numbers",
    "whole number",
    "integer only",
)

# Pattern attribute regexes that indicate the input expects digits.
# Servers regularly write `pattern="\\d+"`, `pattern="[0-9]+"`,
# `pattern="^\\d{1,3}$"`, etc. Match conservatively — only flag when
# the start of the pattern is a digit class, not when digits appear
# inside a broader expression like a phone-format mask.
_NUMERIC_PATTERN_RE = re.compile(r"^\^?\\d|^\^?\[0-9\]")


async def _looks_numeric(el: ElementHandle, page: Page) -> bool:
    """Return True if a text-shaped input is actually numeric-only.

    Three signals (any one suffices):

    1. ``inputmode`` attribute contains "numeric" or "decimal".
    2. ``pattern`` attribute starts with a digit class (``\\d`` or
       ``[0-9]``).
    3. A nearby ancestor (within 4 parents) contains a hint text like
       "numbers only" / "digits only" / "must be a number".

    The walk is identical in spirit to ``_clean_compound_label``'s
    ancestor crawl — capped depth, lowercased substring search, fail-
    closed on any DOM exception.
    """
    # Signal 1: inputmode
    try:
        inputmode = (await el.get_attribute("inputmode") or "").lower()
    except Exception:
        inputmode = ""
    if "numeric" in inputmode or "decimal" in inputmode:
        return True

    # Signal 2: pattern attribute
    try:
        pattern = (await el.get_attribute("pattern") or "").strip()
    except Exception:
        pattern = ""
    if pattern and _NUMERIC_PATTERN_RE.search(pattern):
        return True

    # Signal 3: ancestor hint text. Walk up the DOM (cap at 4 parents)
    # and check inner_text for any of the numeric hint phrases. Caps
    # match elsewhere in this file — anything deeper risks dragging
    # in unrelated chrome from the page header.
    try:
        hint_text = await el.evaluate(
            """el => {
                const phrases = [];
                let cur = el.parentElement;
                let depth = 0;
                while (cur && depth < 4) {
                    // Cheap and good enough: grab visible text of the
                    // ancestor. A label hint ("Numbers only") is
                    // almost always within ~200 chars of the input.
                    const t = cur.innerText || cur.textContent || '';
                    phrases.push(t);
                    cur = cur.parentElement;
                    depth += 1;
                }
                return phrases.join(' \\n ').toLowerCase();
            }"""
        )
    except Exception:
        hint_text = ""
    if hint_text:
        for phrase in _NUMERIC_HINT_PHRASES:
            if phrase in hint_text:
                return True

    return False


async def _classify_element(
    el: ElementHandle, label: str, page: Page
) -> FormField | None:
    """Classify an element into a FormField with type and options."""
    # Reduce compound multi-line wrapper labels to the actionable
    # line BEFORE phantom-label and personal-info matching run on
    # them. A label like "Mobile Number\n\n...helper text...\n\n
    # Country *" should be treated as just "Country *" for every
    # downstream consumer (form_filler keyword match, LLM prompt,
    # answers.json fuzzy match, FIELD_RESULT log line).
    label = _clean_compound_label(label)

    try:
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        input_type = (await el.get_attribute("type") or "text").lower()
    except Exception:
        return None

    # Drop phantom labels that aren't actually questions ("Current
    # page", "Voluntary self identification questions", "Upload a
    # file"). The form_filler used to feed these to the LLM as if
    # they were real prompts and got back garbage like "Yes" or the
    # user's phone number, then typed it into a heading-shaped
    # element. File inputs run the phantom check too — every
    # phantom hit there is upload-chrome text ("Upload a file",
    # "Drag and drop"), which the platform-level uploader handles
    # via classify_file_input regardless of the field-detector's
    # opinion.
    if _is_phantom_label(label):
        logger.debug(
            "Skipping phantom-label field: %r (type=%s)",
            label, input_type,
        )
        return None

    if tag == "textarea":
        return FormField(label=label, element=el, field_type="textarea")

    elif tag == "select":
        options: list[str] = []
        try:
            option_els = await el.query_selector_all("option")
            for opt in option_els:
                text = (await opt.inner_text()).strip()
                value = await opt.get_attribute("value")
                if text and value:
                    options.append(text)
        except Exception:
            pass
        return FormField(
            label=label, element=el, field_type="select", options=options
        )

    elif tag == "input":
        if input_type == "file":
            return FormField(label=label, element=el, field_type="file")
        elif input_type in ("radio", "checkbox"):
            return FormField(label=label, element=el, field_type=input_type)
        elif input_type in ("date", "datetime-local", "month", "week"):
            # Preserve native date pickers so the form filler can emit
            # ISO-formatted values instead of typing free text.
            return FormField(label=label, element=el, field_type="date")
        elif input_type == "number":
            return FormField(label=label, element=el, field_type="number")
        else:
            # `<input type="text">` (or unspecified) is the catch-all,
            # but many sites mark a numeric-only field via inputmode,
            # pattern, or a sibling hint label like "numbers only".
            # Promote to field_type="number" so the form_filler treats
            # the answer numerically (live-run bug 2026-05-01: a
            # "Years of experience" text field with a 'numeric only'
            # hint received "I have 6 years..." and silently rejected).
            if await _looks_numeric(el, page):
                return FormField(label=label, element=el, field_type="number")
            return FormField(label=label, element=el, field_type="text")

    else:
        # Catch-all for custom elements (divs with contenteditable, etc.)
        return FormField(label=label, element=el, field_type="text")
