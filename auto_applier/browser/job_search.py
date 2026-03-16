"""Search LinkedIn for jobs and parse results."""

import re

import click
from playwright.async_api import Page

from auto_applier.browser.anti_detect import human_scroll, random_delay, random_mouse_movement
from auto_applier.storage.models import Job

LINKEDIN_JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"


async def search_jobs(page: Page, keyword: str, location: str = "") -> list[Job]:
    """Search LinkedIn for jobs matching the keyword. Returns a list of Job objects."""
    click.echo(f"Searching LinkedIn for: {keyword}")

    # Build search URL with Easy Apply filter (f_AL=true)
    params = f"?keywords={keyword}&f_AL=true"
    if location:
        params += f"&location={location}"

    await page.goto(LINKEDIN_JOBS_SEARCH_URL + params, wait_until="domcontentloaded")
    await random_delay(3, 6)
    await human_scroll(page)

    jobs = []
    job_cards = await page.query_selector_all(".job-card-container")

    if not job_cards:
        # Fallback selector — LinkedIn changes these frequently
        job_cards = await page.query_selector_all("[data-job-id]")

    click.echo(f"Found {len(job_cards)} job listings.")

    for card in job_cards:
        try:
            job = await _parse_job_card(card, keyword)
            if job:
                jobs.append(job)
        except Exception as e:
            click.echo(f"  Skipping a job card (parse error): {e}")
            continue

        await random_mouse_movement(page)

    return jobs


async def _parse_job_card(card, search_keyword: str) -> Job | None:
    """Extract job details from a LinkedIn job card element."""
    # Get job ID from the card's data attribute
    job_id = await card.get_attribute("data-job-id")
    if not job_id:
        # Try to find it in a child link
        link = await card.query_selector("a[href*='/jobs/view/']")
        if link:
            href = await link.get_attribute("href") or ""
            match = re.search(r"/jobs/view/(\d+)", href)
            job_id = match.group(1) if match else None

    if not job_id:
        return None

    # Extract title
    title_el = await card.query_selector(".job-card-list__title, .artdeco-entity-lockup__title")
    title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

    # Extract company
    company_el = await card.query_selector(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle")
    company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

    url = f"https://www.linkedin.com/jobs/view/{job_id}/"

    return Job(
        job_id=job_id,
        title=title,
        company=company,
        url=url,
        search_keyword=search_keyword,
    )


async def get_job_description(page: Page, job_url: str) -> str:
    """Navigate to a job listing and extract the full description text."""
    await page.goto(job_url, wait_until="domcontentloaded")
    await random_delay(2, 4)
    await human_scroll(page)

    # Try common selectors for job description
    selectors = [
        ".jobs-description__content",
        ".jobs-box__html-content",
        "#job-details",
        ".description__text",
    ]

    for selector in selectors:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()

    # Fallback: grab whatever is in the main content area
    return ""
