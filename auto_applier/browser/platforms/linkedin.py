"""LinkedIn Easy Apply platform adapter.

Login strategy: manual login with persistent session. The automated login
flow is one of LinkedIn's top detection triggers. Instead, we:
1. Check if session cookies are still valid
2. If not, open LinkedIn login page and let the user log in manually
3. Persistent browser profile preserves the session for future runs
"""

import re

import click
from playwright.async_api import Page

from auto_applier.browser.anti_detect import (
    human_click,
    human_scroll,
    human_type,
    random_delay,
    random_mouse_movement,
    reading_pause,
    simulate_organic_behavior,
)
from auto_applier.browser.base_platform import JobPlatform
from auto_applier.storage.models import Job, SkillGap

FEED_URL = "https://www.linkedin.com/feed/"
JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"


class LinkedInPlatform(JobPlatform):
    name = "LinkedIn"
    source_id = "linkedin"

    # ── Auth (manual login with session persistence) ─────────────

    async def ensure_logged_in(self) -> bool:
        page = await self.get_page()
        await page.goto(FEED_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        if "/feed" in page.url:
            click.echo("  LinkedIn: session active (already logged in).")
            # Simulate brief feed browsing before jumping to jobs
            await simulate_organic_behavior(page)
            return True

        # Session expired — ask user to log in manually
        return await self._manual_login(page)

    async def _manual_login(self, page: Page) -> bool:
        """Let the user log in manually — much safer than automated login.

        The persistent browser profile will save the session for next time.
        """
        click.echo(
            "\n  ╔══════════════════════════════════════════════╗\n"
            "  ║  LinkedIn login required.                    ║\n"
            "  ║                                              ║\n"
            "  ║  A browser window is open to LinkedIn.       ║\n"
            "  ║  Please log in manually — this is the safest ║\n"
            "  ║  way to avoid triggering bot detection.      ║\n"
            "  ║                                              ║\n"
            "  ║  Your session will be saved for future runs. ║\n"
            "  ╚══════════════════════════════════════════════╝\n"
        )
        click.echo("  Waiting for you to log in... (press Enter when done)")

        # Navigate to LinkedIn home (which shows login if not authenticated)
        await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded")

        input()  # Wait for user to finish logging in
        await random_delay(2, 4)

        # Verify login succeeded
        await page.goto(FEED_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        if "/feed" in page.url:
            click.echo("  LinkedIn: logged in successfully. Session saved.")
            # Browse the feed briefly to look natural
            await reading_pause(page, 3, 6)
            return True

        click.echo(f"  LinkedIn: login check failed. Current URL: {page.url}")
        return False

    # ── Search ───────────────────────────────────────────────────

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        page = await self.get_page()
        click.echo(f"  Searching LinkedIn for: {keyword}")

        params = f"?keywords={keyword}&f_AL=true"
        if location:
            params += f"&location={location}"

        await page.goto(JOBS_SEARCH_URL + params, wait_until="domcontentloaded")
        await random_delay(3, 6)

        # Simulate reading the search results page
        await reading_pause(page, 2, 5)

        job_cards = await page.query_selector_all(".job-card-container")
        if not job_cards:
            job_cards = await page.query_selector_all("[data-job-id]")

        click.echo(f"  Found {len(job_cards)} listings.")
        jobs = []

        for card in job_cards:
            try:
                job = await self._parse_card(card, keyword)
                if job:
                    jobs.append(job)
            except Exception as e:
                click.echo(f"    Skipping card (parse error): {e}")
            await random_mouse_movement(page)

        return jobs

    async def _parse_card(self, card, search_keyword: str) -> Job | None:
        job_id = await card.get_attribute("data-job-id")
        if not job_id:
            link = await card.query_selector("a[href*='/jobs/view/']")
            if link:
                href = await link.get_attribute("href") or ""
                match = re.search(r"/jobs/view/(\d+)", href)
                job_id = match.group(1) if match else None
        if not job_id:
            return None

        title_el = await card.query_selector(
            ".job-card-list__title, .artdeco-entity-lockup__title"
        )
        title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

        company_el = await card.query_selector(
            ".job-card-container__primary-description, .artdeco-entity-lockup__subtitle"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

        return Job(
            job_id=job_id,
            title=title,
            company=company,
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            search_keyword=search_keyword,
            source=self.source_id,
        )

    # ── Job Description ──────────────────────────────────────────

    async def get_job_description(self, job: Job) -> str:
        page = await self.get_page()
        await page.goto(job.url, wait_until="domcontentloaded")
        await random_delay(2, 4)

        # Simulate actually reading the job description
        await reading_pause(page, 3, 8)

        for selector in [
            ".jobs-description__content",
            ".jobs-box__html-content",
            "#job-details",
            ".description__text",
        ]:
            el = await page.query_selector(selector)
            if el:
                return (await el.inner_text()).strip()
        return ""

    # ── Apply ────────────────────────────────────────────────────

    async def apply_to_job(
        self, job: Job, dry_run: bool = False,
    ) -> tuple[bool, list[SkillGap]]:
        page = await self.get_page()

        # Organic noise before applying — don't just click apply immediately
        await simulate_organic_behavior(page)

        # Click Easy Apply button
        for selector in [
            "button.jobs-apply-button",
            'button[aria-label*="Easy Apply"]',
            ".jobs-apply-button--top-card",
        ]:
            button = await page.query_selector(selector)
            if button:
                await random_delay(1, 3)
                await human_click(page, selector)
                await random_delay(2, 4)
                break
        else:
            click.echo("    No Easy Apply button found.")
            return False, []

        return await self._walk_modal(page, job, dry_run)

    async def _walk_modal(
        self, page: Page, job: Job, dry_run: bool,
    ) -> tuple[bool, list[SkillGap]]:
        gaps: list[SkillGap] = []

        for _ in range(10):
            await random_delay(1, 3)

            submit = await page.query_selector(
                'button[aria-label="Submit application"], '
                'button[aria-label="Review your application"]'
            )

            if submit:
                label = await submit.get_attribute("aria-label") or ""
                if "Review" in label:
                    await human_click(page, 'button[aria-label="Review your application"]')
                    await random_delay(1, 3)
                    continue

                if dry_run:
                    click.echo("    [DRY RUN] Would submit here.")
                    await self._close_modal(page)
                    return True, gaps

                await human_click(page, 'button[aria-label="Submit application"]')
                await random_delay(2, 4)
                click.echo("    Application submitted!")
                return True, gaps

            # Fill fields on current step
            step_gaps = await self._fill_step(page, job.job_id)
            gaps.extend(step_gaps)

            next_btn = await page.query_selector(
                'button[aria-label="Continue to next step"], '
                'button[data-easy-apply-next-button]'
            )
            if next_btn:
                await human_click(page, 'button[aria-label="Continue to next step"]')
                await random_delay(1, 3)
            else:
                click.echo("    No Next or Submit button. Aborting.")
                await self._close_modal(page)
                return False, gaps

        await self._close_modal(page)
        return False, gaps

    async def _fill_step(self, page: Page, job_id: str) -> list[SkillGap]:
        gaps = []
        form_groups = await page.query_selector_all(
            ".jobs-easy-apply-form-section__grouping, .fb-dash-form-element"
        )

        for group in form_groups:
            label_el = await group.query_selector("label, .fb-dash-form-element__label")
            if not label_el:
                continue
            label_text = (await label_el.inner_text()).strip().lower()

            value = self._match_field(label_text)
            if value:
                input_el = await group.query_selector("input, textarea")
                if input_el:
                    input_id = await input_el.get_attribute("id")
                    if input_id:
                        await input_el.fill("")
                        await human_type(page, f"#{input_id}", value)
            else:
                gaps.append(SkillGap(
                    job_id=job_id,
                    field_label=label_text,
                    category=self._categorize_field(label_text),
                ))

        return gaps

    def _match_field(self, label: str) -> str | None:
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
                return self.config.get(key, "")
        return None

    @staticmethod
    def _categorize_field(label: str) -> str:
        label = label.lower()
        if any(w in label for w in ["certif", "license"]):
            return "certification"
        if any(w in label for w in ["experience", "years", "how long"]):
            return "experience"
        if any(w in label for w in ["skill", "proficien", "familiar"]):
            return "skill"
        return "other"

    async def _close_modal(self, page: Page) -> None:
        close = await page.query_selector(
            'button[aria-label="Dismiss"], button[data-test-modal-close-btn]'
        )
        if close:
            await human_click(page, 'button[aria-label="Dismiss"]')
            await random_delay(1, 2)

        discard = await page.query_selector(
            'button[data-test-dialog-primary-btn], '
            'button[data-control-name="discard_application_confirm_btn"]'
        )
        if discard:
            await discard.click()
            await random_delay(1, 2)
