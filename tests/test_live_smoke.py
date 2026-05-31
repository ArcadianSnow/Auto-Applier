"""Live ATS smoke tests (spec section 11b Phase 3 (9/M)).

> "Scheduled live smoke tests against real sites catch selector drift early
>  (the #1 v2 bug source)."

Two layers:
  1. **Discovery smoke**: hits the real public APIs (Greenhouse / Lever /
     Ashby) against a small list of known-stable tokens. Catches "the API
     shape changed" — rare but breaks discovery instantly when it happens.
  2. **Form-load smoke**: navigates to one real apply form per ATS via
     ``BrowserSession`` and asserts the standard selectors resolve in the
     LIVE HTML. Catches "the ATS changed the apply form selectors" — the
     #1 v2 bug class.

**This file NEVER submits.** Every test ends at form load or earlier. Per
Rule 2.6 (gather vs. act), live smoke tests are gather work — being wrong
is cheap and doesn't compound. The cron / Task Scheduler that runs them is
*user-installed*; the autonomous build only produces the test target.

Marker:
  * ``@pytest.mark.smoke`` — opt-in only. Skipped from the default suite via
    ``pyproject.toml`` ``addopts``. Run via ``pytest -m smoke`` or via
    ``scripts/run_smoke.py``.

Failure handling:
  * Network flakiness on one URL doesn't kill the whole run — each test
    catches network/HTTP errors and ``pytest.skip``s with the error so cron
    monitoring can distinguish "real selector drift" from "AWS hiccup."
  * **Selector drift** is the loud failure: assertions raise normally so the
    cron log shows exactly which selector regressed and on which ATS.

What to do when this trips:
  1. Read the failure message — it names the missing selector and ATS.
  2. Reproduce locally: ``pytest tests_v3/test_live_smoke.py -m smoke -v``.
  3. If the live HTML really changed: run
     ``scripts/refresh_fixtures.py <ats> <url>`` against a current posting,
     update the per-ATS driver code to match the new selectors, re-run
     ``tests_v3/test_selector_drift.py`` to confirm fixture+driver alignment.
  4. Commit fixture + driver changes together; the next scheduled smoke run
     should pass.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from auto_applier.sources.ashby import AshbySource
from auto_applier.sources.greenhouse import GreenhouseSource, confirm_probe as gh_confirm
from auto_applier.sources.lever import LeverSource


# Curated stable tokens for the smoke run. These are big established companies
# that change board tokens slowly. Refresh this list if any of them go away
# (a smoke failure on token-not-found is a "rotate the list" signal, not a
# selector drift signal — distinguish in the assertion messages).
_GH_STABLE_TOKENS = ["anthropic", "cloudflare", "tripadvisor"]
_LEVER_STABLE_SITES = ["matchgroup", "highspot"]
_ASHBY_STABLE_SLUGS = ["Linear", "Ramp", "Vanta"]


# --------------------------------------------------------------- discovery smoke

@pytest.mark.smoke
def test_smoke_greenhouse_discovery_reachable():
    """At least ONE of the curated GH tokens must return jobs. We don't
    assert per-token because GH token decay is real (some companies migrate
    boards); any-of-N keeps the test stable while still catching "the GH
    API is down" or "the response shape changed.\""""
    src = GreenhouseSource()
    try:
        any_listings = []
        per_token_errors: dict[str, str] = {}
        for token in _GH_STABLE_TOKENS:
            try:
                listings = src.discover(token)
                any_listings.append((token, len(listings)))
            except Exception as exc:  # noqa: BLE001
                per_token_errors[token] = f"{type(exc).__name__}: {exc}"
        # Net assertion: at least one token actually produced jobs.
        productive = [(t, n) for t, n in any_listings if n > 0]
        if not productive:
            pytest.fail(
                "GH discovery: no curated token returned jobs.\n"
                f"  per-token counts: {any_listings}\n"
                f"  per-token errors: {per_token_errors}\n"
                "  -> rotate _GH_STABLE_TOKENS if all are dead, "
                "or investigate the GH discovery code path"
            )
    finally:
        src.close()


@pytest.mark.smoke
def test_smoke_greenhouse_jd_describe_works():
    """describe() must return a non-trivially-sized JD for the first hit on a
    healthy token. Catches "the JD field renamed" / "the API returns empty
    descriptions now" regressions."""
    src = GreenhouseSource()
    try:
        for token in _GH_STABLE_TOKENS:
            try:
                listings = src.discover(token)
            except Exception:
                continue
            if not listings:
                continue
            text = src.describe(listings[0])
            assert len(text) > 200, (
                f"GH describe for {token}/{listings[0].source_job_id}: "
                f"JD only {len(text)} chars - field rename likely"
            )
            return  # one success is enough for the smoke
        pytest.skip("no GH token produced listings to describe")
    finally:
        src.close()


@pytest.mark.smoke
def test_smoke_lever_discovery_reachable():
    """At least one curated Lever site returns jobs. Lever inventory is
    sparser than GH but the stable list above is curated for low turnover."""
    src = LeverSource()
    try:
        any_listings = []
        per_site_errors: dict[str, str] = {}
        for site in _LEVER_STABLE_SITES:
            try:
                listings = src.discover(site)
                any_listings.append((site, len(listings)))
            except Exception as exc:  # noqa: BLE001
                per_site_errors[site] = f"{type(exc).__name__}: {exc}"
        productive = [(s, n) for s, n in any_listings if n > 0]
        if not productive:
            pytest.fail(
                "Lever discovery: no curated site returned jobs.\n"
                f"  per-site counts: {any_listings}\n"
                f"  per-site errors: {per_site_errors}"
            )
    finally:
        src.close()


@pytest.mark.smoke
def test_smoke_ashby_discovery_reachable():
    """At least one Ashby slug returns jobs."""
    src = AshbySource()
    try:
        any_listings = []
        per_slug_errors: dict[str, str] = {}
        for slug in _ASHBY_STABLE_SLUGS:
            try:
                listings = src.discover(slug)
                any_listings.append((slug, len(listings)))
            except Exception as exc:  # noqa: BLE001
                per_slug_errors[slug] = f"{type(exc).__name__}: {exc}"
        productive = [(s, n) for s, n in any_listings if n > 0]
        if not productive:
            pytest.fail(
                "Ashby discovery: no curated slug returned jobs.\n"
                f"  per-slug counts: {any_listings}\n"
                f"  per-slug errors: {per_slug_errors}"
            )
    finally:
        src.close()


# --------------------------------------------------------------- form-load smoke

def _selector_present(html: str, *patterns: str) -> bool:
    """True iff at least one of the regex/substring patterns occurs in the
    live HTML. We use substring (not full HTML-parsing) because the smoke
    test wants to catch "the selector our driver uses is gone" — substring
    match against the raw HTML is robust to ATS reshuffles of the surrounding
    markup."""
    return any(p in html for p in patterns)


def _first_live_listing(src_kind: str) -> tuple[str, str] | None:
    """Find one (token/site/slug, apply_url) from the curated lists. Returns
    None if every curated token failed — caller skips the form-load test in
    that case (network is bad enough that we wouldn't trust the result).
    """
    if src_kind == "greenhouse":
        src = GreenhouseSource()
        try:
            for token in _GH_STABLE_TOKENS:
                try:
                    listings = src.discover(token)
                except Exception:
                    continue
                if listings:
                    return token, listings[0].url
        finally:
            src.close()
    elif src_kind == "lever":
        src = LeverSource()
        try:
            for site in _LEVER_STABLE_SITES:
                try:
                    listings = src.discover(site)
                except Exception:
                    continue
                if listings:
                    return site, listings[0].apply_url
        finally:
            src.close()
    elif src_kind == "ashby":
        src = AshbySource()
        try:
            for slug in _ASHBY_STABLE_SLUGS:
                try:
                    listings = src.discover(slug)
                except Exception:
                    continue
                if listings:
                    return slug, listings[0].apply_url
        finally:
            src.close()
    return None


async def _load_form_html(apply_url: str) -> str:
    """Open the apply URL in headed Chrome via the production session stack,
    let it settle, dump page.content(). NEVER submits. Caller is responsible
    for asserting against the returned HTML."""
    from auto_applier.config import load_settings
    from auto_applier.sources.browser.session import BrowserSession

    settings = load_settings()
    session = BrowserSession(settings.browser_profile_dir)
    await session.start()
    try:
        page = await session.new_page()
        await page.goto(apply_url, wait_until="domcontentloaded")
        await asyncio.sleep(3.0)  # SPA settle + CAPTCHA attach
        return await page.content()
    finally:
        await session.stop()


@pytest.mark.smoke
def test_smoke_greenhouse_form_loads_with_expected_selectors():
    """Load a live GH apply form; assert the standard-field selectors that
    the driver depends on are present in the live HTML. **NEVER submits.**"""
    target = _first_live_listing("greenhouse")
    if target is None:
        pytest.skip("no GH discovery succeeded; can't form-load")
    token, url = target

    html = asyncio.run(_load_form_html(url))
    # Standard fields (the driver's name-keyed + id-keyed handles):
    assert _selector_present(html, 'id="first_name"', "id='first_name'"), (
        f"GH form {url}: #first_name missing in live HTML"
    )
    assert _selector_present(html, 'id="email"', "id='email'"), (
        f"GH form {url}: #email missing"
    )
    assert _selector_present(html, 'id="resume"', "id='resume'"), (
        f"GH form {url}: #resume upload missing - apply path broken"
    )
    # Submit button (any type=submit button on the form).
    assert _selector_present(html, "type=\"submit\"", "type='submit'"), (
        f"GH form {url}: no submit button present"
    )


@pytest.mark.smoke
def test_smoke_lever_form_loads_with_expected_selectors():
    """Load a live Lever apply form; assert standard selectors present. NEVER
    submits."""
    target = _first_live_listing("lever")
    if target is None:
        pytest.skip("no Lever discovery succeeded; can't form-load")
    site, url = target

    html = asyncio.run(_load_form_html(url))
    assert _selector_present(html, "name=\"name\"", "name='name'"), (
        f"Lever form {url}: input[name='name'] missing"
    )
    assert _selector_present(html, "name=\"email\"", "name='email'"), (
        f"Lever form {url}: input[name='email'] missing"
    )
    assert _selector_present(html, "resume-upload-input"), (
        f"Lever form {url}: #resume-upload-input missing"
    )
    assert _selector_present(html, "btn-submit"), (
        f"Lever form {url}: #btn-submit missing"
    )


@pytest.mark.smoke
def test_smoke_ashby_form_loads_with_expected_selectors():
    """Load a live Ashby apply form; assert _systemfield_* selectors present.
    NEVER submits."""
    target = _first_live_listing("ashby")
    if target is None:
        pytest.skip("no Ashby discovery succeeded; can't form-load")
    slug, url = target

    html = asyncio.run(_load_form_html(url))
    assert _selector_present(html, "_systemfield_name"), (
        f"Ashby form {url}: #_systemfield_name missing - SPA render likely "
        "didn't complete OR selector renamed"
    )
    assert _selector_present(html, "_systemfield_email"), (
        f"Ashby form {url}: #_systemfield_email missing"
    )
    assert _selector_present(html, "_systemfield_resume"), (
        f"Ashby form {url}: #_systemfield_resume missing"
    )
