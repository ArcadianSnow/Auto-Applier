"""Browser apply path + anti-detect (spec §8). Phase 1: Greenhouse hosted form."""

from av3.sources.browser.detect import (
    CaptchaResult,
    CaptchaType,
    ConfirmationOutcome,
    ConfirmationResult,
    classify_captcha,
    detect_confirmation,
)

__all__ = [
    "CaptchaResult",
    "CaptchaType",
    "ConfirmationOutcome",
    "ConfirmationResult",
    "classify_captcha",
    "detect_confirmation",
]
