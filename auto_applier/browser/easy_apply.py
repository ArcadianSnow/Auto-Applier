"""Walk through LinkedIn Easy Apply modal and submit applications."""

import click
from playwright.async_api import Page

from auto_applier.browser.anti_detect import human_type, random_delay
from auto_applier.storage.models import SkillGap


async def click_easy_apply(page: Page) -> bool:
    """Find and click the Easy Apply button on a job listing page."""
    # LinkedIn uses several variations of the Easy Apply button
    selectors = [
        "button.jobs-apply-button",
        'button[aria-label*="Easy Apply"]',
        ".jobs-apply-button--top-card",
    ]

    for selector in selectors:
        button = await page.query_selector(selector)
        if button:
            await random_delay(1, 2)
            await button.click()
            await random_delay(2, 4)
            return True

    click.echo("  Could not find Easy Apply button.")
    return False


async def fill_application_modal(
    page: Page,
    user_info: dict,
    dry_run: bool = False,
) -> tuple[bool, list[SkillGap]]:
    """Walk through the Easy Apply modal steps.

    Returns (success, list_of_skill_gaps_found).
    user_info should contain keys like 'phone', 'city', etc. from the user config.
    """
    gaps: list[SkillGap] = []
    max_steps = 10  # Safety limit to avoid infinite loops

    for step in range(max_steps):
        await random_delay(1, 3)

        # Check if we're on the review/submit page
        submit_button = await page.query_selector(
            'button[aria-label="Submit application"], '
            'button[aria-label="Review your application"]'
        )

        if submit_button:
            label = await submit_button.get_attribute("aria-label") or ""
            if "Review" in label:
                await submit_button.click()
                await random_delay(1, 3)
                continue

            # This is the final submit
            if dry_run:
                click.echo("  [DRY RUN] Would submit application here.")
                # Close the modal instead
                await _close_modal(page)
                return True, gaps

            await submit_button.click()
            await random_delay(2, 4)
            click.echo("  Application submitted!")
            return True, gaps

        # Try to fill visible form fields
        step_gaps = await _fill_current_step(page, user_info)
        gaps.extend(step_gaps)

        # Click "Next" to advance
        next_button = await page.query_selector(
            'button[aria-label="Continue to next step"], '
            'button[data-easy-apply-next-button]'
        )
        if next_button:
            await next_button.click()
            await random_delay(1, 3)
        else:
            # No next button and no submit button — something went wrong
            click.echo("  Could not find Next or Submit button. Aborting this application.")
            await _close_modal(page)
            return False, gaps

    click.echo("  Hit max steps limit. Aborting.")
    await _close_modal(page)
    return False, gaps


async def _fill_current_step(page: Page, user_info: dict) -> list[SkillGap]:
    """Attempt to fill form fields on the current modal step.

    Returns any fields we couldn't fill (skill gaps).
    """
    gaps = []

    # Find all visible input fields, textareas, and selects
    form_groups = await page.query_selector_all(
        ".jobs-easy-apply-form-section__grouping, "
        ".fb-dash-form-element"
    )

    for group in form_groups:
        label_el = await group.query_selector("label, .fb-dash-form-element__label")
        if not label_el:
            continue

        label_text = (await label_el.inner_text()).strip().lower()

        # Try to match the field to known user info
        value = _match_field_to_user_info(label_text, user_info)

        if value:
            input_el = await group.query_selector("input, textarea")
            if input_el:
                await input_el.fill("")  # Clear first
                await human_type(page, f"#{await input_el.get_attribute('id')}", value)
        else:
            # We don't know the answer — record it as a skill gap
            gaps.append(
                SkillGap(
                    job_id="",  # Will be filled by the caller
                    field_label=label_text,
                    category=_categorize_field(label_text),
                )
            )

    return gaps


def _match_field_to_user_info(label: str, user_info: dict) -> str | None:
    """Try to match a form field label to a value from user config."""
    mappings = {
        "phone": ["phone", "mobile", "contact number"],
        "email": ["email", "e-mail"],
        "city": ["city", "location"],
        "linkedin": ["linkedin profile", "linkedin url"],
        "website": ["website", "portfolio", "personal site"],
        "first_name": ["first name"],
        "last_name": ["last name"],
    }

    for key, keywords in mappings.items():
        if any(kw in label for kw in keywords):
            return user_info.get(key, None)

    return None


def _categorize_field(label: str) -> str:
    """Guess the category of a form field based on its label."""
    label = label.lower()
    if any(word in label for word in ["certif", "license"]):
        return "certification"
    if any(word in label for word in ["experience", "years", "how long"]):
        return "experience"
    if any(word in label for word in ["skill", "proficien", "familiar"]):
        return "skill"
    return "other"


async def _close_modal(page: Page) -> None:
    """Close the Easy Apply modal."""
    close_button = await page.query_selector(
        'button[aria-label="Dismiss"], '
        'button[data-test-modal-close-btn]'
    )
    if close_button:
        await close_button.click()
        await random_delay(1, 2)

    # Handle "Discard application?" confirmation
    discard_button = await page.query_selector(
        'button[data-test-dialog-primary-btn], '
        'button[data-control-name="discard_application_confirm_btn"]'
    )
    if discard_button:
        await discard_button.click()
        await random_delay(1, 2)
