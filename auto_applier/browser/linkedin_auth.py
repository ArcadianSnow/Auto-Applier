"""LinkedIn login and session management."""

import click
from playwright.async_api import BrowserContext, Page

from auto_applier.browser.anti_detect import human_type, random_delay
from auto_applier.config import get_linkedin_email, get_linkedin_password

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"


async def is_logged_in(page: Page) -> bool:
    """Check if we're currently logged into LinkedIn."""
    await page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")
    await random_delay(2, 4)

    # If we land on the feed, we're logged in. If redirected to login, we're not.
    return "/feed" in page.url


async def login(page: Page) -> bool:
    """Log into LinkedIn with credentials from .env."""
    email = get_linkedin_email()
    password = get_linkedin_password()

    if not email or not password:
        click.echo("LinkedIn credentials not found. Run 'auto-applier configure' first.")
        return False

    click.echo("Logging into LinkedIn...")
    await page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")
    await random_delay(2, 4)

    # Fill email
    await human_type(page, "#username", email)
    await random_delay(1, 2)

    # Fill password
    await human_type(page, "#password", password)
    await random_delay(1, 2)

    # Click sign in
    await page.click('[data-litms-control-urn="login-submit"]')
    await random_delay(3, 6)

    # Check for security challenges (CAPTCHA, verification, etc.)
    if "checkpoint" in page.url or "challenge" in page.url:
        click.echo(
            "\nLinkedIn is requesting additional verification.\n"
            "Please complete the challenge in the browser window.\n"
            "Press Enter here once you've finished..."
        )
        input()  # Wait for user to handle it manually
        await random_delay(2, 4)

    # Verify login succeeded
    if "/feed" in page.url or "linkedin.com/feed" in page.url:
        click.echo("Successfully logged into LinkedIn.")
        return True

    click.echo("Login may have failed. Current URL: " + page.url)
    return False


async def ensure_logged_in(context: BrowserContext) -> Page:
    """Get a page that's logged into LinkedIn, logging in if needed."""
    pages = context.pages
    page = pages[0] if pages else await context.new_page()

    if not await is_logged_in(page):
        success = await login(page)
        if not success:
            raise RuntimeError("Failed to log into LinkedIn.")

    return page
