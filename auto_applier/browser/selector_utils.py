"""Platform-agnostic form field detection utilities.

Detects form fields on any job application page using four strategies:
1. ``label[for]`` -> ``input[id]`` pairing
2. Form-group containers (label + input as siblings/children)
3. ``aria-label`` on input elements
4. ``placeholder`` text as label fallback

Deduplicates by label text to avoid filling the same field twice.
"""
import logging
from dataclasses import dataclass, field

from playwright.async_api import ElementHandle, Page

logger = logging.getLogger(__name__)


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


async def _classify_element(
    el: ElementHandle, label: str, page: Page
) -> FormField | None:
    """Classify an element into a FormField with type and options."""
    try:
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        input_type = (await el.get_attribute("type") or "text").lower()
    except Exception:
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
            return FormField(label=label, element=el, field_type="text")

    else:
        # Catch-all for custom elements (divs with contenteditable, etc.)
        return FormField(label=label, element=el, field_type="text")
