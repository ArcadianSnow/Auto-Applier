"""Human-like behavior simulation and anti-detection measures.

All interactions should go through these helpers to avoid bot-like patterns.
Key principles:
  - Never use fixed intervals — always randomize with wide variance
  - Mouse paths should follow Bezier curves, not straight lines
  - Typing speed should vary per-character with occasional pauses
  - Simulate reading by lingering on pages before acting
  - Mix in organic noise: scroll back up, hover random elements, idle
"""

import asyncio
import math
import random

from playwright.async_api import Page

from auto_applier.config import MIN_DELAY_BETWEEN_ACTIONS, MAX_DELAY_BETWEEN_ACTIONS


# ── Delays ───────────────────────────────────────────────────────

async def random_delay(min_sec: float | None = None, max_sec: float | None = None) -> None:
    """Wait a random amount of time, with occasional longer pauses."""
    low = min_sec or MIN_DELAY_BETWEEN_ACTIONS
    high = max_sec or MAX_DELAY_BETWEEN_ACTIONS

    # 15% chance of a "distraction" pause (2-5x longer)
    if random.random() < 0.15:
        high *= random.uniform(2.0, 5.0)

    await asyncio.sleep(random.uniform(low, high))


async def reading_pause(page: Page, min_sec: float = 3, max_sec: float = 12) -> None:
    """Simulate reading a page — scroll a bit, pause, maybe scroll more."""
    # Initial reading time
    await asyncio.sleep(random.uniform(min_sec, max_sec))

    # Simulate eye-scanning: small random scrolls with pauses
    for _ in range(random.randint(1, 3)):
        await human_scroll(page, small=True)
        await asyncio.sleep(random.uniform(1.0, 4.0))


# ── Mouse Movement (Bezier curves) ──────────────────────────────

def _bezier_point(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> tuple:
    """Calculate a point on a cubic Bezier curve at parameter t."""
    x = (1-t)**3 * p0[0] + 3*(1-t)**2*t * p1[0] + 3*(1-t)*t**2 * p2[0] + t**3 * p3[0]
    y = (1-t)**3 * p0[1] + 3*(1-t)**2*t * p1[1] + 3*(1-t)*t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def _generate_bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 20,
) -> list[tuple[float, float]]:
    """Generate a natural-looking curved mouse path between two points."""
    distance = math.sqrt((end[0] - start[0])**2 + (end[1] - start[1])**2)

    # Control points create the curve — offset perpendicular to the line
    spread = max(distance * 0.3, 30)
    ctrl1 = (
        start[0] + (end[0] - start[0]) * 0.25 + random.uniform(-spread, spread),
        start[1] + (end[1] - start[1]) * 0.25 + random.uniform(-spread, spread),
    )
    ctrl2 = (
        start[0] + (end[0] - start[0]) * 0.75 + random.uniform(-spread, spread),
        start[1] + (end[1] - start[1]) * 0.75 + random.uniform(-spread, spread),
    )

    # Vary the number of steps based on distance
    steps = max(10, int(distance / 15)) + random.randint(-3, 3)

    path = []
    for i in range(steps + 1):
        t = i / steps
        x, y = _bezier_point(t, start, ctrl1, ctrl2, end)
        # Add Perlin-like noise (small jitter)
        x += random.gauss(0, 1.5)
        y += random.gauss(0, 1.5)
        path.append((x, y))

    return path


async def human_move_mouse(page: Page, target_x: float, target_y: float) -> None:
    """Move mouse to target position along a Bezier curve."""
    try:
        # Get current mouse position (approximate from last known)
        current = await page.evaluate(
            "() => ({x: window._lastMouseX || 400, y: window._lastMouseY || 300})"
        )
        start = (current["x"], current["y"])
    except Exception:
        start = (random.randint(200, 600), random.randint(200, 400))

    path = _generate_bezier_path(start, (target_x, target_y))

    for x, y in path:
        await page.mouse.move(x, y)
        # Variable speed: faster in the middle, slower at start/end
        await asyncio.sleep(random.uniform(0.005, 0.025))

    # Track position for next move
    try:
        await page.evaluate(f"() => {{ window._lastMouseX = {target_x}; window._lastMouseY = {target_y}; }}")
    except Exception:
        pass


async def human_click(page: Page, selector: str) -> None:
    """Move to an element with a Bezier curve, then click it."""
    try:
        el = await page.wait_for_selector(selector, timeout=5000)
        if not el:
            await page.click(selector)
            return

        box = await el.bounding_box()
        if not box:
            await page.click(selector)
            return

        # Click at a random point within the element (not dead center)
        target_x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        await human_move_mouse(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.05, 0.2))
        await page.mouse.click(target_x, target_y)
    except Exception:
        # Fallback to direct click
        await page.click(selector)


async def random_mouse_movement(page: Page) -> None:
    """Move the mouse to a random position to simulate idle behavior."""
    x = random.randint(100, 900)
    y = random.randint(100, 600)
    await human_move_mouse(page, x, y)
    await random_delay(0.3, 1.5)


# ── Typing ───────────────────────────────────────────────────────

async def human_type(page: Page, selector: str, text: str) -> None:
    """Type text with variable speed, occasional pauses, and mistakes.

    Simulates ~250 characters per minute with natural variance.
    """
    await human_click(page, selector)
    await asyncio.sleep(random.uniform(0.3, 0.8))

    for i, char in enumerate(text):
        await page.keyboard.type(char)

        # Base delay: 80-250ms per character (variable typing speed)
        base_delay = random.uniform(0.08, 0.25)

        # Slow down on special characters (shift key, numbers, symbols)
        if not char.isalpha():
            base_delay *= random.uniform(1.2, 2.0)

        # Occasional longer pause (thinking/looking at keyboard)
        if random.random() < 0.08:
            base_delay += random.uniform(0.3, 1.5)

        # Brief pause between words
        if char == " ":
            base_delay += random.uniform(0.05, 0.3)

        await asyncio.sleep(base_delay)


# ── Scrolling ────────────────────────────────────────────────────

async def human_scroll(page: Page, small: bool = False) -> None:
    """Scroll down the page in a natural way with variable speed."""
    if small:
        total = random.randint(80, 250)
    else:
        total = random.randint(200, 600)

    # Scroll in multiple small increments (not one big jump)
    scrolled = 0
    while scrolled < total:
        chunk = random.randint(30, min(120, total - scrolled + 1))
        await page.mouse.wheel(0, chunk)
        scrolled += chunk
        await asyncio.sleep(random.uniform(0.05, 0.15))

    await random_delay(0.5, 2.0)

    # 20% chance to scroll back up a bit (re-reading)
    if random.random() < 0.20:
        back = random.randint(50, 150)
        await page.mouse.wheel(0, -back)
        await random_delay(0.5, 1.5)


# ── Organic Noise ────────────────────────────────────────────────

async def simulate_organic_behavior(page: Page) -> None:
    """Perform random organic actions to break up bot-like patterns.

    Call this between major actions (searching, applying) to simulate
    natural browsing behavior.
    """
    action = random.choice(["idle", "scroll", "mouse", "hover"])

    if action == "idle":
        # Just sit there like a human reading something
        await asyncio.sleep(random.uniform(2, 8))

    elif action == "scroll":
        await human_scroll(page)
        await asyncio.sleep(random.uniform(1, 3))

    elif action == "mouse":
        await random_mouse_movement(page)

    elif action == "hover":
        # Hover over a random link or button
        try:
            links = await page.query_selector_all("a, button")
            if links:
                target = random.choice(links[:10])  # Only visible ones
                box = await target.bounding_box()
                if box:
                    await human_move_mouse(
                        page,
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await asyncio.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass
