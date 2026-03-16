"""Human-like behavior simulation and anti-detection measures."""

import asyncio
import random

from playwright.async_api import Page

from auto_applier.config import MIN_DELAY_BETWEEN_ACTIONS, MAX_DELAY_BETWEEN_ACTIONS


async def random_delay(min_sec: float | None = None, max_sec: float | None = None) -> None:
    """Wait a random amount of time to simulate human behavior."""
    low = min_sec or MIN_DELAY_BETWEEN_ACTIONS
    high = max_sec or MAX_DELAY_BETWEEN_ACTIONS
    await asyncio.sleep(random.uniform(low, high))


async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text with per-character jitter like a real person."""
    await page.click(selector)
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))


async def human_scroll(page: Page) -> None:
    """Scroll down the page a bit, like reading a job description."""
    scroll_amount = random.randint(200, 600)
    await page.mouse.wheel(0, scroll_amount)
    await random_delay(1, 3)


async def random_mouse_movement(page: Page) -> None:
    """Move the mouse to a random position on the page."""
    x = random.randint(100, 800)
    y = random.randint(100, 600)
    await page.mouse.move(x, y)
    await random_delay(0.5, 1.5)
