"""Multi-source CAPTCHA-presence survey (Phase 1 → Phase 2, spec §11 risk ②).

Loads real ATS application forms and classifies the anti-bot challenge on each — WITHOUT
submitting. This measures the *ceiling* (how prevalent invisible reCAPTCHA / Enterprise /
hCaptcha is) across Greenhouse, Lever, and Ashby, to answer "is auto-apply viable off
Greenhouse?" The complementary *auto-pass rate* (does the challenge clear) needs real
submits and is a separate, gated run.

Generic by design: a ``ProbeTarget`` is (source, token, apply_url, form_selector, spa); the
probe navigates, waits (SPA-aware), reads the post-load DOM, and runs ``classify_captcha``.
No form-filling — presence detection only needs the loaded page.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

from av3.pipeline.stage import new_run_id, stage
from av3.sources.ashby import AshbySource
from av3.sources.browser.detect import classify_captcha
from av3.sources.browser.session import BrowserSession
from av3.sources.greenhouse import GreenhouseSource
from av3.sources.lever import LeverSource


@dataclass
class ProbeTarget:
    source: str
    token: str
    title: str
    apply_url: str
    form_selector: str
    spa: bool = False


@dataclass
class SurveyRow:
    token: str
    job_url: str
    title: str
    captcha_type: str
    is_invisible: bool
    enterprise: bool
    custom_questions: int
    auto_eligible: bool
    form_present: bool = True   # False ⇒ canonical URL redirected / form didn't render
    source: str = "greenhouse"


def summarize_survey(rows: list[SurveyRow]) -> dict:
    """Aggregate into the headline distribution (pure; unit-tested). Per-source + overall."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    def _block(subset: list[SurveyRow]) -> dict:
        forms = [r for r in subset if r.form_present]
        nf = len(forms) or 1
        return {
            "n": len(subset),
            "forms_present": len(forms),
            "by_captcha_type": dict(Counter(r.captcha_type for r in subset)),
            "pct_invisible_of_forms": round(100 * sum(r.is_invisible for r in forms) / nf, 1),
            "pct_enterprise_of_forms": round(100 * sum(r.enterprise for r in forms) / nf, 1),
            "pct_auto_eligible_of_forms": round(100 * sum(r.auto_eligible for r in forms) / nf, 1),
        }

    by_source = {}
    for src in sorted({r.source for r in rows}):
        by_source[src] = _block([r for r in rows if r.source == src])
    out = _block(rows)
    out["by_source"] = by_source
    out["note"] = (
        "auto_eligible = no visible challenge + form present; NOT the auto-pass rate "
        "(needs real submits). enterprise reCAPTCHA is the hardest to auto-clear."
    )
    return out


async def probe_apply_captcha(page, apply_url: str, form_selector: str, *, spa: bool, settle_s: float = 2.0):
    """Navigate to an apply form and classify its CAPTCHA. Returns (CaptchaResult, form_present)."""
    await page.goto(apply_url, wait_until="domcontentloaded")
    if spa:
        try:
            await page.wait_for_selector(form_selector, timeout=8000)
        except Exception:  # noqa: BLE001 — SPA may render differently / be gated
            pass
    await asyncio.sleep(settle_s)  # let recaptcha/hcaptcha scripts inject + run
    html = await page.content()
    scripts = await page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
    captcha = classify_captcha(html, scripts)
    form_present = (await page.query_selector(form_selector)) is not None
    return captcha, form_present


@stage("survey")
async def _probe_one(*, page, target: ProbeTarget, platform: str) -> SurveyRow:
    captcha, form_present = await probe_apply_captcha(
        page, target.apply_url, target.form_selector, spa=target.spa
    )
    return SurveyRow(
        token=target.token,
        job_url=target.apply_url,
        title=target.title,
        captcha_type=captcha.type.value,
        is_invisible=captcha.is_invisible,
        enterprise=captcha.enterprise,
        custom_questions=0,  # presence survey skips form-walking
        auto_eligible=(captcha.is_invisible or not captcha.present) and form_present,
        form_present=form_present,
        source=target.source,
    )


async def run_multi_survey(targets: list[ProbeTarget], profile_dir, settle_s: float = 2.0) -> list[SurveyRow]:
    """Live dry-run survey across mixed-source targets. One headed browser, reused page."""
    new_run_id()
    rows: list[SurveyRow] = []
    session = BrowserSession(profile_dir)
    await session.start()
    try:
        page = await session.new_page()
        for t in targets:
            row = await _probe_one(page=page, target=t, platform=t.source)
            if row is not None:
                rows.append(row)
    finally:
        await session.stop()
    return rows


# --- target builders (discovery is sync httpx; survey is async browser) -----
def gh_targets(tokens: list[str], max_per: int = 1) -> list[ProbeTarget]:
    gh = GreenhouseSource()
    targets: list[ProbeTarget] = []
    try:
        for tok in tokens:
            try:
                listings = gh.discover(tok)
            except Exception:  # noqa: BLE001
                continue
            for lst in listings[:max_per]:
                # Greenhouse: the job page IS the form; standard id is #first_name.
                targets.append(ProbeTarget("greenhouse", tok, lst.title, lst.url, "#first_name", spa=False))
    finally:
        gh.close()
    return targets


def lever_targets(sites: list[str], max_per: int = 1) -> list[ProbeTarget]:
    lv = LeverSource()
    targets: list[ProbeTarget] = []
    try:
        for site in sites:
            for lst in lv.discover(site)[:max_per]:
                targets.append(ProbeTarget("lever", site, lst.title, lst.apply_url, lv.form_selector, spa=lv.spa))
    finally:
        lv.close()
    return targets


def ashby_targets(slugs: list[str], max_per: int = 1) -> list[ProbeTarget]:
    ash = AshbySource()
    targets: list[ProbeTarget] = []
    try:
        for slug in slugs:
            for lst in ash.discover(slug)[:max_per]:
                targets.append(ProbeTarget("ashby", slug, lst.title, lst.apply_url, ash.form_selector, spa=ash.spa))
    finally:
        ash.close()
    return targets
