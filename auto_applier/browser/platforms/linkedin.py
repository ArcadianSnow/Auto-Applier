"""LinkedIn Easy Apply platform adapter."""

import re

import click
from playwright.async_api import Page

from auto_applier.browser.anti_detect import (
    human_scroll,
    human_type,
    random_delay,
    random_mouse_movement,
)
from auto_applier.browser.base_platform import JobPlatform
from auto_applier.storage.models import Job, SkillGap

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"
JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"


class LinkedInPlatform(JobPlatform):
    name = "LinkedIn"
    source_id = "linkedin"

    def _get_credentials(self) -> tuple[str, str]:
        platforms = self.config.get("platforms", {})
        creds = platforms.get("linkedin", {})
        return creds.get("email", ""), creds.get("password", "")

    # ── Auth ─────────────────────────────────────────────────────

    async def ensure_logged_in(self) -> bool:
        page = await self.get_page()
        await page.goto(FEED_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        if "/feed" in page.url:
            click.echo("  LinkedIn: already logged in.")
            return True

        return await self._login(page)

    async def _login(self, page: Page) -> bool:
        email, password = self._get_credentials()
        if not email or not password:
            click.echo("  LinkedIn credentials not configured. Skipping.")
            return False

        click.echo("  Logging into LinkedIn...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        await human_type(page, "#username", email)
        await random_delay(1, 2)
        await human_type(page, "#password", password)
        await random_delay(1, 2)
        await page.click('[data-litms-control-urn="login-submit"]')
        await random_delay(3, 6)

        if "checkpoint" in page.url or "challenge" in page.url:
            click.echo(
                "\n  LinkedIn is requesting verification.\n"
                "  Please complete it in the browser window.\n"
                "  Press Enter here once done..."
            )
            input()
            await random_delay(2, 4)

        if "/feed" in page.url:
            click.echo("  LinkedIn: logged in successfully.")
            return True

        click.echo(f"  LinkedIn login may have failed. URL: {page.url}")
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
        await human_scroll(page)

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
        await human_scroll(page)

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

        # Click Easy Apply button
        for selector in [
            "button.jobs-apply-button",
            'button[aria-label*="Easy Apply"]',
            ".jobs-apply-button--top-card",
        ]:
            button = await page.query_selector(selector)
            if button:
                await random_delay(1, 2)
                await button.click()
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
                    await submit.click()
                    await random_delay(1, 3)
                    continue

                if dry_run:
                    click.echo("    [DRY RUN] Would submit here.")
                    await self._close_modal(page)
                    return True, gaps

                await submit.click()
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
                await next_btn.click()
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
            await close.click()
            await random_delay(1, 2)

        discard = await page.query_selector(
            'button[data-test-dialog-primary-btn], '
            'button[data-control-name="discard_application_confirm_btn"]'
        )
        if discard:
            await discard.click()
            await random_delay(1, 2)
