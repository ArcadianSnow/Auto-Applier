"""Indeed Smart Apply platform adapter."""

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

LOGIN_URL = "https://secure.indeed.com/auth"
SEARCH_URL = "https://www.indeed.com/jobs"


class IndeedPlatform(JobPlatform):
    name = "Indeed"
    source_id = "indeed"

    def _get_credentials(self) -> tuple[str, str]:
        platforms = self.config.get("platforms", {})
        creds = platforms.get("indeed", {})
        return creds.get("email", ""), creds.get("password", "")

    # ── Auth ─────────────────────────────────────────────────────

    async def ensure_logged_in(self) -> bool:
        page = await self.get_page()

        # Check if already logged in by visiting the profile page
        await page.goto("https://www.indeed.com/account/view", wait_until="domcontentloaded")
        await random_delay(2, 4)

        if "secure.indeed.com/auth" not in page.url and "login" not in page.url:
            click.echo("  Indeed: already logged in.")
            return True

        return await self._login(page)

    async def _login(self, page: Page) -> bool:
        email, password = self._get_credentials()
        if not email or not password:
            click.echo("  Indeed credentials not configured. Skipping.")
            return False

        click.echo("  Logging into Indeed...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await random_delay(2, 4)

        # Indeed login is two-step: email first, then password
        email_input = await page.query_selector(
            'input[type="email"], input[name="__email"], #ifl-InputFormField-3'
        )
        if email_input:
            await email_input.fill("")
            await human_type(page, 'input[type="email"]', email)
            await random_delay(1, 2)

            # Click continue / submit email
            submit = await page.query_selector(
                'button[type="submit"], button[data-tn-element="auth-page-email-submit"]'
            )
            if submit:
                await submit.click()
                await random_delay(2, 4)

        # Password step
        pw_input = await page.query_selector(
            'input[type="password"], input[name="__password"]'
        )
        if pw_input:
            await human_type(page, 'input[type="password"]', password)
            await random_delay(1, 2)
            submit = await page.query_selector('button[type="submit"]')
            if submit:
                await submit.click()
                await random_delay(3, 6)

        # Handle verification prompts
        if "verify" in page.url.lower() or "challenge" in page.url.lower():
            click.echo(
                "\n  Indeed is requesting verification.\n"
                "  Please complete it in the browser window.\n"
                "  Press Enter here once done..."
            )
            input()
            await random_delay(2, 4)

        # Check success
        await page.goto("https://www.indeed.com/account/view", wait_until="domcontentloaded")
        await random_delay(2, 3)
        if "auth" not in page.url and "login" not in page.url:
            click.echo("  Indeed: logged in successfully.")
            return True

        click.echo(f"  Indeed login may have failed. URL: {page.url}")
        return False

    # ── Search ───────────────────────────────────────────────────

    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        page = await self.get_page()
        click.echo(f"  Searching Indeed for: {keyword}")

        # sc=0kf%3Aattr(DSQF7)%3B filters to "Easily apply" jobs
        url = f"{SEARCH_URL}?q={keyword}&sc=0kf%3Aattr(DSQF7)%3B"
        if location:
            url += f"&l={location}"

        await page.goto(url, wait_until="domcontentloaded")
        await random_delay(3, 6)
        await human_scroll(page)

        # Indeed job cards
        job_cards = await page.query_selector_all(
            'div.job_seen_beacon, div[data-jk], li div.cardOutline'
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
        # Get job key from data-jk attribute (on card or child link)
        job_id = await card.get_attribute("data-jk")
        if not job_id:
            link = await card.query_selector("a[data-jk]")
            if link:
                job_id = await link.get_attribute("data-jk")
        if not job_id:
            # Try from href
            link = await card.query_selector("a[href*='jk=']")
            if link:
                href = await link.get_attribute("href") or ""
                if "jk=" in href:
                    job_id = href.split("jk=")[-1].split("&")[0]
        if not job_id:
            return None

        title_el = await card.query_selector(
            "h2.jobTitle a, a[data-jk] span, .jobTitle"
        )
        title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

        company_el = await card.query_selector(
            "[data-testid='company-name'], .companyName, .company_location .companyName"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

        url = f"https://www.indeed.com/viewjob?jk={job_id}"

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
            "#jobDescriptionText",
            ".jobsearch-jobDescriptionText",
            "[data-testid='jobDescription']",
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

        # Find the apply button
        apply_btn = None
        for selector in [
            'button[id*="indeedApplyButton"]',
            'button[aria-label*="Apply now"]',
            '#applyButtonLinkContainer button',
            'button.jobsearch-IndeedApplyButton-newDesign',
        ]:
            apply_btn = await page.query_selector(selector)
            if apply_btn:
                break

        if not apply_btn:
            click.echo("    No Indeed Easy Apply button found.")
            return False, gaps

        # Check it's not an external redirect
        btn_text = (await apply_btn.inner_text()).strip().lower()
        if "company site" in btn_text:
            click.echo("    External application — skipping.")
            return False, gaps

        await random_delay(1, 2)
        await apply_btn.click()
        await random_delay(2, 4)

        # Walk through the Indeed apply modal
        return await self._walk_modal(page, job, dry_run)

    async def _walk_modal(
        self, page: Page, job: Job, dry_run: bool,
    ) -> tuple[bool, list[SkillGap]]:
        gaps: list[SkillGap] = []

        for _ in range(10):
            await random_delay(1, 3)

            # Check for the final submit / continue button
            submit = await page.query_selector(
                'button[data-testid="submit-button"], '
                'button[aria-label*="Submit your application"], '
                'button.ia-continueButton'
            )
            review = await page.query_selector(
                'button[aria-label*="Review"], '
                'button[data-testid="review-button"]'
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

            if review:
                await review.click()
                await random_delay(1, 3)
                continue

            # Try to fill screener questions
            step_gaps = await self._fill_screeners(page, job.job_id)
            gaps.extend(step_gaps)

            # Click continue/next
            cont = await page.query_selector(
                'button.ia-continueButton, '
                'button[data-testid="continue-button"], '
                'button[aria-label*="Continue"]'
            )
            if cont:
                await cont.click()
                await random_delay(1, 3)
            else:
                break

        return False, gaps

    async def _fill_screeners(self, page: Page, job_id: str) -> list[SkillGap]:
        gaps = []
        questions = await page.query_selector_all(
            '.ia-Questions-item, [data-testid*="question"]'
        )

        for q in questions:
            label_el = await q.query_selector("label, .ia-Questions-itemLabel")
            if not label_el:
                continue

            label_text = (await label_el.inner_text()).strip().lower()
            value = self._match_field(label_text)

            if value:
                input_el = await q.query_selector("input, textarea, select")
                if input_el:
                    tag = await input_el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await input_el.select_option(label=value)
                    else:
                        await input_el.fill("")
                        await human_type(page, f"#{await input_el.get_attribute('id')}", value)
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
