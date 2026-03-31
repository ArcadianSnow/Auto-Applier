"""Human-like behavior simulation and anti-detection measures.

This module provides realistic mouse movement (cubic Bezier curves),
per-character typing jitter, organic scrolling, and random noise actions
that make browser automation look like a real human.

Key behaviors:
- Bezier curve mouse paths (not straight lines)
- Per-character typing with 50-250ms jitter
- 15% chance of "distraction" pauses (2-5x longer)
- Random scrolls, hovers, and idle pauses between actions
"""
import asyncio
import logging
import math
import random

from playwright.async_api import Page

from auto_applier.config import MIN_DELAY_BETWEEN_ACTIONS, MAX_DELAY_BETWEEN_ACTIONS

logger = logging.getLogger(__name__)


# ── Delays ───────────────────────────────────────────────────────────


async def random_delay(
    min_sec: float | None = None,
    max_sec: float | None = None,
) -> None:
    """Wait a random amount of time, with occasional longer pauses.

    15% of the time the delay is multiplied by 2-5x to simulate
    the user getting distracted (checking phone, reading something).
    """
    low = min_sec if min_sec is not None else MIN_DELAY_BETWEEN_ACTIONS
    high = max_sec if max_sec is not None else MAX_DELAY_BETWEEN_ACTIONS
    # 15% chance of distraction pause
    if random.random() < 0.15:
        multiplier = random.uniform(2.0, 5.0)
        high *= multiplier
        logger.debug("Distraction pause: %.1fx multiplier", multiplier)
    delay = random.uniform(low, high)
    await asyncio.sleep(delay)


# ── Mouse Movement (Cubic Bezier Curves) ────────────────────────────


async def human_move(page: Page, x: float, y: float, steps: int = 0) -> None:
    """Move mouse along a cubic Bezier curve to target coordinates.

    Uses two random control points offset from the straight-line path
    to create a natural-looking curved trajectory with small jitter.
    """
    # Track last known position on the page object
    current_x = getattr(page, "_last_mouse_x", random.randint(100, 400))
    current_y = getattr(page, "_last_mouse_y", random.randint(100, 400))

    distance = math.hypot(x - current_x, y - current_y)
    if distance < 5:
        return  # Already close enough

    num_steps = steps or max(10, int(distance / 8))
    spread = max(distance * 0.3, 30)

    # Two random control points for cubic Bezier
    cp1x = current_x + (x - current_x) * 0.25 + random.gauss(0, spread)
    cp1y = current_y + (y - current_y) * 0.25 + random.gauss(0, spread)
    cp2x = current_x + (x - current_x) * 0.75 + random.gauss(0, spread * 0.5)
    cp2y = current_y + (y - current_y) * 0.75 + random.gauss(0, spread * 0.5)

    for i in range(1, num_steps + 1):
        t = i / num_steps
        inv = 1 - t
        # Cubic Bezier formula: B(t) = (1-t)^3*P0 + 3(1-t)^2*t*P1 + 3(1-t)*t^2*P2 + t^3*P3
        bx = (
            inv**3 * current_x
            + 3 * inv**2 * t * cp1x
            + 3 * inv * t**2 * cp2x
            + t**3 * x
        )
        by = (
            inv**3 * current_y
            + 3 * inv**2 * t * cp1y
            + 3 * inv * t**2 * cp2y
            + t**3 * y
        )
        # Add small per-step jitter
        bx += random.gauss(0, 1.5)
        by += random.gauss(0, 1.5)
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.uniform(0.005, 0.02))

    # Store final position for next movement
    page._last_mouse_x = x
    page._last_mouse_y = y


async def human_click(page: Page, selector: str, timeout: int = 5000) -> None:
    """Click an element with human-like Bezier mouse movement.

    Moves to a random point within the element's bounding box (avoiding
    edges), pauses briefly, then clicks.
    """
    try:
        el = await page.wait_for_selector(selector, timeout=timeout, state="visible")
        if not el:
            await page.click(selector)
            return
        box = await el.bounding_box()
        if box:
            # Click at a random point within the inner 60% of the element
            target_x = box["x"] + random.uniform(box["width"] * 0.2, box["width"] * 0.8)
            target_y = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)
            await human_move(page, target_x, target_y)
            # Brief pause before clicking (like a human confirming target)
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.click(target_x, target_y)
        else:
            await page.click(selector)
    except Exception:
        # Last resort: use Playwright's built-in click
        await page.click(selector)


# ── Typing ───────────────────────────────────────────────────────────


async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text with per-character jitter simulating ~250 chars/min.

    Letters are typed faster (50-180ms), numbers and symbols slower
    (100-250ms). 5% chance of a brief thinking pause per character.
    """
    el = await page.wait_for_selector(selector, timeout=5000, state="visible")
    if el:
        await el.click()

    for char in text:
        if char.isalpha():
            delay = random.uniform(0.05, 0.18)
        else:
            # Numbers, symbols, and punctuation are typed slower
            delay = random.uniform(0.10, 0.25)
        # 5% chance of a thinking pause mid-typing
        if random.random() < 0.05:
            delay += random.uniform(0.3, 0.8)
        await page.keyboard.type(char, delay=0)
        await asyncio.sleep(delay)


async def human_fill(page: Page, selector: str, text: str) -> None:
    """Clear a field and type text with human-like behavior.

    Selects all existing content, deletes it, then types the new text.
    """
    el = await page.wait_for_selector(selector, timeout=5000, state="visible")
    if el:
        await el.click()
        await page.keyboard.press("Control+A")
        await asyncio.sleep(random.uniform(0.05, 0.1))
        await page.keyboard.press("Backspace")
        await asyncio.sleep(random.uniform(0.1, 0.3))
    await human_type(page, selector, text)


# ── Scrolling ────────────────────────────────────────────────────────


async def human_scroll(
    page: Page, direction: str = "down", amount: int = 0
) -> None:
    """Scroll in small chunks with variable speed, like a real human.

    Scrolls in 3-6 random-sized chunks rather than one smooth movement.
    """
    pixels = amount or random.randint(200, 600)
    if direction == "up":
        pixels = -pixels

    chunks = random.randint(3, 6)
    for _ in range(chunks):
        chunk = pixels / chunks + random.gauss(0, 20)
        await page.mouse.wheel(0, chunk)
        await asyncio.sleep(random.uniform(0.05, 0.15))


# ── Organic Noise ────────────────────────────────────────────────────


async def reading_pause(page: Page) -> None:
    """Simulate reading a page before acting.

    Waits 2-5 seconds, then optionally scrolls around (0-2 scrolls)
    to mimic a user scanning the content.
    """
    await asyncio.sleep(random.uniform(2.0, 5.0))
    for _ in range(random.randint(0, 2)):
        await human_scroll(page, "down", random.randint(100, 300))
        await asyncio.sleep(random.uniform(1.0, 3.0))


async def simulate_organic_behavior(page: Page) -> None:
    """Add a random organic action between major operations.

    Randomly chooses one of: idle pause, scroll, mouse wander, or
    hover over a random link/button.
    """
    action = random.choice(["idle", "scroll", "mouse", "hover"])

    if action == "idle":
        await asyncio.sleep(random.uniform(1.0, 3.0))

    elif action == "scroll":
        direction = random.choice(["down", "up"])
        await human_scroll(page, direction)
        await asyncio.sleep(random.uniform(0.5, 1.5))

    elif action == "mouse":
        # Move mouse to a random viewport position
        x = random.randint(100, 900)
        y = random.randint(100, 600)
        await human_move(page, x, y)

    elif action == "hover":
        # Hover over a random visible link or button
        try:
            links = await page.query_selector_all("a, button")
            if links:
                el = random.choice(links[:10])
                box = await el.bounding_box()
                if box:
                    await human_move(
                        page,
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass
