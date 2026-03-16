"""ZipRecruiter 1-Click Apply platform adapter."""

import click
from playwright.async_api import Page

from auto_applier.browser.anti_detect import (
    human_scroll,
    random_delay,
    random_mouse_movement,
    human_type,
)
from auto_applier.browser.base_platform import JobPlatform
from auto_applier.storage.models import Job, SkillGap

LOGIN_URL = "https://www.ziprecruiter.com/login"
SEARCH_URL = "https://www.ziprecruiter.com/jobs-search"


class ZipRecruiterPlatform(JobPlatform):
    name = "ZipRecruiter"
    source_id = "ziprecruiter"

    def _get_credentials(self) -> tuple[str, str]:
        platforms = self.config.get("platforms", {})
        creds = platforms.get("ziprecruiter", {})
        return creds.get("email", ""), creds.get("password", "")

    # ── Auth ─────────────────────────────────────────────────────

    async def ensure_logged_in(self) -> bool:
        page = await self.get_page()

        await page.goto(
            "https://www.ziprecruiter.com/profile", wait_until="domcontentloaded"
        )
        await random_delay(2, 4)

        if "login" not in page.url:
            click.echo("  ZipRecruiter: already logged in.")
            return True

        return await self._login(page)

    async def _login(self, page: Page) -> bool:
        email, password = self._get_credentials()
        if not email or not password:
            click.echo("  ZipRecruiter credentials not configured. Skipping.")
            return False

        click.echo("  Logging into ZipRecruiter...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        # Email
        email_input = await page.query_selector(
            'input[name="email"], input[type="email"]'
        )
        if email_input:
            await email_input.fill("")
            await human_type(page, 'input[type="email"]', email)
            await random_delay(1, 2)

        # Password
        pw_input = await page.query_selector('input[type="password"]')
        if pw_input:
            await human_type(page, 'input[type="password"]', password)
            await random_delay(1, 2)

        # Submit
        submit = await page.query_selector(
            'button[type="submit"], button[data-testid="login-button"]'
        )
        if submit:
            await submit.click()
            await random_delay(3, 6)

        # Handle verification
        if "verify" in page.url.lower() or "challenge" in page.url.lower():
            click.echo(
                "\n  ZipRecruiter is requesting verification.\n"
                "  Please complete it in the browser window.\n"
                "  Press Enter here once done..."
            )
            input()
            await random_delay(2, 4)

        # Verify success
        await page.goto(
            "https://www.ziprecruiter.com/profile", wait_until="domcontentloaded"
        )
        await random_delay(2, 3)
        if "login" not in page.url:
            click.echo("  ZipRecruiter: logged in successfully.")
            return True

        click.echo(f"  ZipRecruiter login may have failed. URL: {page.url}")
        return False

    # ── Search ───────────────────────────────────────────────────

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        page = await self.get_page()
        click.echo(f"  Searching ZipRecruiter for: {keyword}")

        url = f"{SEARCH_URL}?search={keyword}&is_one_click_apply=true"
        if location:
            url += f"&location={location}"

        await page.goto(url, wait_until="domcontentloaded")
        await random_delay(3, 6)
        await human_scroll(page)

        job_cards = await page.query_selector_all(
            'article.job-listing, [data-testid="job-card"], .jobList article'
        )

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
        link = await card.query_selector(
            'a[href*="/jobs/"], a.job-title, a[data-testid="job-title"]'
        )
        if not link:
            return None

        href = await link.get_attribute("href") or ""
        # ZipRecruiter job URLs look like /jobs/{slug}/{job_id}
        # or /c/{company}/job/{job_id}
        job_id = href.rstrip("/").split("/")[-1].split("?")[0]
        if not job_id:
            return None

        title = (await link.inner_text()).strip()

        company_el = await card.query_selector(
            '[data-testid="company-name"], .company-name, .job-company'
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

        full_url = href if href.startswith("http") else f"https://www.ziprecruiter.com{href}"

        return Job(
            job_id=job_id,
            title=title,
            company=company,
            url=full_url,
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
            '[data-testid="job-description"]',
            '.job-description',
            '.jobDescriptionSection',
            '#job-description',
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

        # ZipRecruiter 1-Click Apply is simpler than other platforms:
        # click button → optional confirm dialog → done
        apply_btn = None
        for selector in [
            'button[data-testid="apply-button"]',
            'button.one_click_apply',
            'button[aria-label*="1-Click Apply"]',
            'button:has-text("1-Click Apply")',
            'button:has-text("Apply")',
        ]:
            apply_btn = await page.query_selector(selector)
            if apply_btn:
                # Make sure it's not "Apply on company site"
                text = (await apply_btn.inner_text()).strip().lower()
                if "company" in text:
                    apply_btn = None
                    continue
                break

        if not apply_btn:
            click.echo("    No ZipRecruiter 1-Click Apply button found.")
            return False, []

        if dry_run:
            click.echo("    [DRY RUN] Would 1-Click Apply here.")
            return True, []

        await random_delay(1, 2)
        await apply_btn.click()
        await random_delay(2, 4)

        # Handle optional confirmation dialog
        confirm = await page.query_selector(
            'button[data-testid="confirm-apply"], '
            'button:has-text("Confirm"), '
            'button:has-text("Submit Application")'
        )
        if confirm:
            await confirm.click()
            await random_delay(2, 4)

        # Check for success indicator
        success = await page.query_selector(
            '[data-testid="application-success"], '
            '.application-success, '
            ':has-text("Application submitted")'
        )
        if success:
            click.echo("    Application submitted!")
            return True, []

        # If there's a form (screener questions), try to fill it
        form = await page.query_selector('form[data-testid*="apply"], .apply-form')
        if form:
            gaps = await self._fill_screeners(page, job.job_id)

            submit = await page.query_selector(
                'button[type="submit"], button:has-text("Submit")'
            )
            if submit:
                if dry_run:
                    click.echo("    [DRY RUN] Would submit here.")
                    return True, gaps
                await submit.click()
                await random_delay(2, 4)
                click.echo("    Application submitted!")
                return True, gaps

            return False, gaps

        # Assume success if the button was clicked and no error appeared
        click.echo("    Application likely submitted (no error detected).")
        return True, []

    async def _fill_screeners(self, page: Page, job_id: str) -> list[SkillGap]:
        gaps = []
        fields = await page.query_selector_all(
            '.form-group, [data-testid*="question"], .screener-question'
        )

        for field_group in fields:
            label_el = await field_group.query_selector("label")
            if not label_el:
                continue

            label_text = (await label_el.inner_text()).strip().lower()
            value = self._match_field(label_text)

            if value:
                input_el = await field_group.query_selector("input, textarea, select")
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
        return None
