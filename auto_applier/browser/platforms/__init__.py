"""Platform registry -- maps source_id strings to platform adapter classes.

Two adapter families:

  - **Browser-driven** (linkedin, indeed, dice, ziprecruiter) — drive
    real Chrome via patchright, walk the apply form. Subject to
    anti-detect, rate limits, selector decay.
  - **ATS public API** (ats_greenhouse, ats_lever, ats_ashby) — hit
    the ATS's documented JSON endpoint, no browser involved. Always
    discovery-only: surfaces matches in 'cli almost' for manual
    apply via the URL. Per Tier 4 research, this category sidesteps
    LinkedIn-style TLS fingerprinting entirely.

ATS adapters are independent of the browser pool — they don't
consume a browser context, they don't trigger CAPTCHA paths, and
they're safe to run alongside the browser-driven platforms in the
same cycle.
"""
from auto_applier.browser.platforms.linkedin import LinkedInPlatform
from auto_applier.browser.platforms.indeed import IndeedPlatform
from auto_applier.browser.platforms.dice import DicePlatform
from auto_applier.browser.platforms.ziprecruiter import ZipRecruiterPlatform
from auto_applier.browser.platforms.ats_greenhouse import ATSGreenhousePlatform
from auto_applier.browser.platforms.ats_lever import ATSLeverPlatform
from auto_applier.browser.platforms.ats_ashby import ATSAshbyPlatform

PLATFORM_REGISTRY: dict[str, type] = {
    # Browser-driven platforms
    "linkedin": LinkedInPlatform,
    "indeed": IndeedPlatform,
    "dice": DicePlatform,
    "ziprecruiter": ZipRecruiterPlatform,
    # ATS public-API discovery (no browser, no anti-detect risk)
    "ats_greenhouse": ATSGreenhousePlatform,
    "ats_lever": ATSLeverPlatform,
    "ats_ashby": ATSAshbyPlatform,
}
