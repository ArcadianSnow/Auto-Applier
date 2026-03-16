"""Dice.com Easy Apply platform adapter."""

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

LOGIN_URL = "https://www.dice.com/dashboard/login"
SEARCH_URL = "https://www.dice.com/jobs"


class DicePlatform(JobPlatform):
    name = "Dice"
    source_id = "dice"

    def _get_credentials(self) -> tuple[str, str]:
        platforms = self.config.get("platforms", {})
        creds = platforms.get("dice", {})
        return creds.get("email", ""), creds.get("password", "")

    # ── Auth ─────────────────────────────────────────────────────

    async def ensure_logged_in(self) -> bool:
        page = await self.get_page()

        await page.goto("https://www.dice.com/dashboard", wait_until="domcontentloaded")
        await random_delay(2, 4)

        if "login" not in page.url and "dashboard" in page.url:
            click.echo("  Dice: already logged in.")
            return True

        return await self._login(page)

    async def _login(self, page: Page) -> bool:
        email, password = self._get_credentials()
        if not email or not password:
            click.echo("  Dice credentials not configured. Skipping.")
            return False

        click.echo("  Logging into Dice...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        # Email
        email_input = await page.query_selector(
            'input[name="email"], input[type="email"], input[data-cy="email-input"]'
        )
        if email_input:
            await email_input.fill("")
            await human_type(page, 'input[name="email"]', email)
            await random_delay(1, 2)

        # Password
        pw_input = await page.query_selector(
            'input[name="password"], input[type="password"]'
        )
        if pw_input:
            await human_type(page, 'input[type="password"]', password)
            await random_delay(1, 2)

        # Submit
        submit = await page.query_selector(
            'button[type="submit"], button[data-cy="login-button"]'
        )
        if submit:
            await submit.click()
            await random_delay(3, 6)

        # Check for verification
        if "verify" in page.url.lower() or "challenge" in page.url.lower():
            click.echo(
                "\n  Dice is requesting verification.\n"
                "  Please complete it in the browser window.\n"
                "  Press Enter here once done..."
            )
            input()
            await random_delay(2, 4)

        # Verify
        await page.goto("https://www.dice.com/dashboard", wait_until="domcontentloaded")
        await random_delay(2, 3)
        if "login" not in page.url:
            click.echo("  Dice: logged in successfully.")
            return True

        click.echo(f"  Dice login may have failed. URL: {page.url}")
        return False

    # ── Search ───────────────────────────────────────────────────

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        page = await self.get_page()
        click.echo(f"  Searching Dice for: {keyword}")

        url = f"{SEARCH_URL}?q={keyword}&filters.easyApply=true"
        if location:
            url += f"&location={location}"

        await page.goto(url, wait_until="domcontentloaded")
        await random_delay(3, 6)
        await human_scroll(page)

        job_cards = await page.query_selector_all(
            '[data-cy="search-card"], .card-title-link, dhi-search-card'
        )

        click.echo(f"  Found {len(job_cards)} listings.")
        jobs = []

        for card in job_cards:
            try:
                job = await self._parse_card(page, card, keyword)
                if job:
                    jobs.append(job)
            except Exception as e:
                click.echo(f"    Skipping card (parse error): {e}")
            await random_mouse_movement(page)

        return jobs

    async def _parse_card(self, page: Page, card, search_keyword: str) -> Job | None:
        # Dice job links contain the job ID in the URL
        link = await card.query_selector(
            'a[data-cy="card-title-link"], a[href*="/job-detail/"]'
        )
        if not link:
            link = card if await card.get_attribute("href") else None
        if not link:
            return None

        href = await link.get_attribute("href") or ""
        # Extract job ID from URL like /job-detail/abc123-def456
        parts = href.split("/job-detail/")
        job_id = parts[-1].split("?")[0] if len(parts) > 1 else None
        if not job_id:
            return None

        title = (await link.inner_text()).strip()

        company_el = await card.query_selector(
            '[data-cy="search-result-company-name"], .card-company a'
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

        url = f"https://www.dice.com/job-detail/{job_id}"

        return Job(
            job_id=job_id,
            title=title,
            company=company,
            url=url,
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
            '[data-cy="jobDescription"]',
            '.job-description',
            '#jobDescription',
            '[data-testid="jobDescriptionHtml"]',
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
        gaps: list[SkillGap] = []

        # Find Easy Apply button
        apply_btn = None
        for selector in [
            'button[data-cy="apply-button-wotc-follow"]',
            'button[aria-label*="Easy Apply"]',
            'apply-button-wotc button',
            'button.btn-primary[data-cy*="apply"]',
        ]:
            apply_btn = await page.query_selector(selector)
            if apply_btn:
                break

        if not apply_btn:
            click.echo("    No Dice Easy Apply button found.")
            return False, gaps

        if dry_run:
            click.echo("    [DRY RUN] Would click Easy Apply here.")
            return True, gaps

        await random_delay(1, 2)
        await apply_btn.click()
        await random_delay(2, 4)

        # Dice Easy Apply is typically a short form:
        # work authorization, contact info, then submit
        return await self._walk_form(page, job, dry_run)

    async def _walk_form(
        self, page: Page, job: Job, dry_run: bool,
    ) -> tuple[bool, list[SkillGap]]:
        gaps: list[SkillGap] = []

        for _ in range(8):
            await random_delay(1, 3)

            # Check if a new tab opened (external ATS redirect)
            if len(page.context.pages) > 2:
                new_page = page.context.pages[-1]
                await new_page.close()
                click.echo("    External ATS redirect detected — skipping.")
                return False, gaps

            # Look for submit
            submit = await page.query_selector(
                'button[data-cy="submit-application"], '
                'button[type="submit"][aria-label*="Submit"], '
                'button.btn-primary:has-text("Submit")'
            )
            if submit:
                btn_text = (await submit.inner_text()).strip().lower()
                if "submit" in btn_text:
                    if dry_run:
                        click.echo("    [DRY RUN] Would submit here.")
                        return True, gaps
                    await submit.click()
                    await random_delay(2, 4)
                    click.echo("    Application submitted!")
                    return True, gaps

            # Fill visible fields
            step_gaps = await self._fill_fields(page, job.job_id)
            gaps.extend(step_gaps)

            # Click next/continue
            next_btn = await page.query_selector(
                'button:has-text("Next"), button:has-text("Continue")'
            )
            if next_btn:
                await next_btn.click()
                await random_delay(1, 3)
            else:
                break

        return False, gaps

    async def _fill_fields(self, page: Page, job_id: str) -> list[SkillGap]:
        gaps = []
        form_groups = await page.query_selector_all(
            '.form-group, [data-cy*="form"], .field-group'
        )

        for group in form_groups:
            label_el = await group.query_selector("label")
            if not label_el:
                continue
            label_text = (await label_el.inner_text()).strip().lower()

            value = self._match_field(label_text)
            if value:
                input_el = await group.query_selector("input, textarea, select")
                if input_el:
                    tag = await input_el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await input_el.select_option(label=value)
                    else:
                        await input_el.fill("")
                        input_id = await input_el.get_attribute("id") or ""
                        if input_id:
                            await human_type(page, f"#{input_id}", value)
            else:
                gaps.append(SkillGap(
                    job_id=job_id,
                    field_label=label_text,
                    category="other",
                ))

        return gaps

    def _match_field(self, label: str) -> str | None:
        mappings = {
            "phone": ["phone", "mobile"],
            "email": ["email"],
            "city": ["city", "location"],
            "first_name": ["first name"],
            "last_name": ["last name"],
        }
        for key, keywords in mappings.items():
            if any(kw in label for kw in keywords):
                return self.config.get(key, "")

        # Dice-specific: work authorization
        if "authorized" in label or "work authorization" in label:
            return self.config.get("work_authorization", "Yes")

        return None
