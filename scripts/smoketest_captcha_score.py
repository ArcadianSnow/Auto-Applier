"""Smoketest: measure our browser stack's reCAPTCHA v3 score (no submits, safe).

Loads a public reCAPTCHA v3 score tester with our stealth BrowserSession and extracts the
score. A score >= 0.5 means invisible reCAPTCHA would likely pass silently (auto viable);
<= 0.3 means it escalates to a visible challenge (assisted). This is a SAFE proxy for the
invisible-CAPTCHA auto-pass rate on reCAPTCHA-gated ATS forms (Greenhouse/Ashby) — it never
submits a job application.

Usage:
    python scripts/smoketest_captcha_score.py                 # patchright + real Chrome
    AV3_DATA_DIR=data/v3 python scripts/smoketest_captcha_score.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av3.config import load_settings  # noqa: E402
from av3.sources.browser.session import BrowserSession  # noqa: E402

_SCORE_RE = re.compile(r"score(?:\s*(?:is|:|=))?\s*(\d(?:\.\d+)?)", re.I)
_FLOAT_RE = re.compile(r"\b(0\.\d|1\.0)\b")

# antcpt computes a v3 score and renders it; cleantalk is a fallback.
_TARGETS = [
    "https://antcpt.com/score_detector/",
    "https://cleantalk.org/recaptcha-v3-score-test",
]


async def measure(url: str, profile_dir: Path) -> None:
    session = BrowserSession(profile_dir)
    await session.start()
    try:
        page = await session.new_page()
        print(f"[{url}] navigating...")
        await page.goto(url, wait_until="domcontentloaded")
        # score is computed client-side a few seconds after load; poll a few times
        score = None
        for attempt in range(6):
            await asyncio.sleep(4)
            try:
                body = await page.inner_text("body")
            except Exception:  # noqa: BLE001
                body = await page.content()
            m = _SCORE_RE.search(body) or _FLOAT_RE.search(body)
            if m:
                score = m.group(1)
                ctx = body[max(0, m.start() - 50): m.end() + 30].replace("\n", " ")
                print(f"  attempt {attempt+1}: score candidate = {score}  | context: ...{ctx}...")
                break
            print(f"  attempt {attempt+1}: no score yet")
        if score is None:
            snippet = (body[:400] if body else "").replace("\n", " ")
            print(f"  NO SCORE FOUND. body snippet: {snippet}")
        else:
            print(f"  ==> reCAPTCHA v3 score (patchright+Chrome) = {score}")
            try:
                val = float(score)
                verdict = (
                    "PASS-likely (auto viable)" if val >= 0.5
                    else "BORDERLINE" if val >= 0.3
                    else "FAIL-likely (escalates to visible -> assisted)"
                )
                print(f"  ==> verdict: {verdict}")
            except ValueError:
                pass
    finally:
        await session.stop()


async def main() -> None:
    settings = load_settings()
    for url in _TARGETS:
        try:
            await measure(url, settings.browser_profile_dir)
            return  # first target that yields a score is enough
        except Exception as exc:  # noqa: BLE001
            print(f"  target failed: {type(exc).__name__}: {exc}")
    print("All targets failed to yield a score.")


if __name__ == "__main__":
    asyncio.run(main())
