"""Detection logic for the apply path — pure functions over page state (spec §8b, §8c).

These encode the Phase -1 findings (research/ats-form-automation.md) for risk ④
(confirmation) and the CAPTCHA-gating insight as TESTABLE code, independent of any live
browser. The Playwright driver (``greenhouse_apply.py``) feeds them the page's HTML / URL /
loaded scripts and acts on the verdict.

Two instruments:
  * ``classify_captcha`` — what anti-bot challenge is on this form, and is it the
    *invisible* (behavioral-score) kind or a *visible* challenge? This split is the
    auto-vs-assisted gate; a visible challenge always → assisted (never solved/retried).
  * ``detect_confirmation`` — did the submit positively confirm? APPLIED requires a
    positive on-page signal; anything else is UNCONFIRMED/FAILED → REVIEW (never guess).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


# --------------------------------------------------------------------- CAPTCHA
class CaptchaType(str, Enum):
    NONE = "none"
    RECAPTCHA_INVISIBLE = "recaptcha_invisible"      # behavioral score — the common GH case
    RECAPTCHA_ENTERPRISE = "recaptcha_enterprise"    # AI-scored; "the wall" (Zapply)
    RECAPTCHA_CHECKBOX = "recaptcha_checkbox"        # visible "I'm not a robot"
    HCAPTCHA = "hcaptcha"                            # Lever's default (invisible)
    VISIBLE_CHALLENGE = "visible_challenge"          # an image/grid challenge is showing


#: CAPTCHA types that DON'T block auto-submit if the behavioral score passes silently.
_INVISIBLE = frozenset(
    {CaptchaType.RECAPTCHA_INVISIBLE, CaptchaType.RECAPTCHA_ENTERPRISE, CaptchaType.HCAPTCHA}
)


@dataclass
class CaptchaResult:
    type: CaptchaType
    is_invisible: bool          # True → may auto-pass on a good behavioral score
    enterprise: bool = False    # reCAPTCHA Enterprise (hardest)

    @property
    def present(self) -> bool:
        return self.type is not CaptchaType.NONE


def classify_captcha(html: str, scripts: list[str] | None = None) -> CaptchaResult:
    """Classify the anti-bot challenge from page HTML + loaded script URLs.

    ``scripts`` is the list of <script src> URLs on the page (the most reliable Enterprise
    tell). HTML alone still works via the response-token textareas and widget markup.
    """
    h = html.lower()
    src = " ".join(scripts or []).lower()

    # A visible challenge frame is showing → always assisted, regardless of vendor.
    if "recaptcha challenge expires" in h or 'title="recaptcha challenge' in h or (
        "rc-imageselect" in h
    ):
        return CaptchaResult(CaptchaType.VISIBLE_CHALLENGE, is_invisible=False)

    # hCaptcha (Lever)
    if "h-captcha-response" in h or "hcaptcha" in h or "hcaptcha.com" in src:
        return CaptchaResult(CaptchaType.HCAPTCHA, is_invisible=True)

    # reCAPTCHA family
    recaptcha_present = (
        "g-recaptcha-response" in h
        or "g-recaptcha" in h
        or "recaptcha/api.js" in src
        or "recaptcha/enterprise.js" in src
        or "gstatic.com/recaptcha" in src
    )
    if recaptcha_present:
        enterprise = "recaptcha/enterprise" in src or "enterprise.js" in src or (
            "enterprise" in h and "recaptcha" in h
        )
        # A visible checkbox widget: data-size other than "invisible".
        m = re.search(r'class="[^"]*g-recaptcha[^"]*"[^>]*data-size="([^"]+)"', h)
        if m and m.group(1) != "invisible":
            return CaptchaResult(CaptchaType.RECAPTCHA_CHECKBOX, is_invisible=False, enterprise=enterprise)
        if enterprise:
            return CaptchaResult(CaptchaType.RECAPTCHA_ENTERPRISE, is_invisible=True, enterprise=True)
        return CaptchaResult(CaptchaType.RECAPTCHA_INVISIBLE, is_invisible=True)

    return CaptchaResult(CaptchaType.NONE, is_invisible=False)


# ---------------------------------------------------------------- CONFIRMATION
class ConfirmationOutcome(str, Enum):
    CONFIRMED = "confirmed"              # positive signal → APPLIED
    CAPTCHA_CHALLENGE = "captcha_challenge"  # visible challenge/email-code → assisted, never retry
    FAILED_VALIDATION = "failed_validation"  # inline errors / missing required → REVIEW
    UNCONFIRMED = "unconfirmed"          # no positive AND no error → REVIEW, retry-safe


@dataclass
class ConfirmationResult:
    outcome: ConfirmationOutcome
    signal: str = ""    # which rule fired (for the event log)


_SUCCESS_TEXT_RE = re.compile(
    r"thank(s| you)|application (was )?(submitted|received)|we('| ha)ve received|"
    r"successfully submitted",
    re.I,
)
_CAPTCHA_ERROR_RE = re.compile(
    r"please complete the recaptcha|verify you('| a)re (not a robot|human)|"
    r"enter the (\d-?digit )?code|complete the captcha",
    re.I,
)
_VALIDATION_ERR_RE = re.compile(
    r'aria-invalid="true"|class="[^"]*error|field is required|please (enter|complete|fill)|'
    r"this field is required",
    re.I,
)


def detect_confirmation(
    url: str, html: str, submit_present: bool = True
) -> ConfirmationResult:
    """Layered confirmation detector (research "Confirmation-detection strategy").

    Order matters — error signals are checked BEFORE the (weaker) submit-disappearance
    corroboration so we never mark APPLIED while a CAPTCHA/validation error is on screen.

    Decision rule: APPLIED only on a positive signal (URL ``/confirmation`` for Greenhouse,
    ``/thanks`` for Lever, or success text) AND no error. Visible CAPTCHA/email-code →
    CAPTCHA_CHALLENGE (assisted, never retry). Otherwise UNCONFIRMED (retry-safe).
    """
    u = (url or "").lower().rstrip("/")
    h = html or ""

    # 1. Strong URL transition (Greenhouse /confirmation, Lever /thanks).
    if u.endswith("/confirmation"):
        return ConfirmationResult(ConfirmationOutcome.CONFIRMED, "url:/confirmation")
    if u.endswith("/thanks"):
        return ConfirmationResult(ConfirmationOutcome.CONFIRMED, "url:/thanks")

    # 2. Error signals (checked before success text / submit-gone).
    if _CAPTCHA_ERROR_RE.search(h):
        return ConfirmationResult(ConfirmationOutcome.CAPTCHA_CHALLENGE, "captcha_error_text")
    if _VALIDATION_ERR_RE.search(h):
        return ConfirmationResult(ConfirmationOutcome.FAILED_VALIDATION, "validation_error")

    # 3. Positive success text (Ashby in-place panel, generic thank-you).
    if _SUCCESS_TEXT_RE.search(h):
        return ConfirmationResult(ConfirmationOutcome.CONFIRMED, "success_text")

    # 4. Corroboration only: submit gone + no errors. Weak alone → still UNCONFIRMED
    #    unless paired with a positive signal above. We never mark APPLIED off this alone.
    return ConfirmationResult(ConfirmationOutcome.UNCONFIRMED, "no_positive_signal")


# ------------------------------------------------------------------ AUTH WALL

#: URL substrings that indicate the page redirected to a login flow rather than the
#: requested apply form. Lowercased before comparison; substring match (so
#: ``/account/login?return=…`` matches). These are the patterns we've actually
#: observed across ATS + board redirects; add new ones here as they're seen.
_LOGIN_URL_MARKERS: tuple[str, ...] = (
    "/login", "/signin", "/sign-in", "/sign_in", "/account/login",
    "/users/sign_in", "/auth/login", "/auth/signin", "auth0.com",
)


_AUTH_WALL_INPUT_RE = re.compile(
    r'<input[^>]+(?:type=["\']password["\']|name=["\']password["\']|id=["\']password)',
    re.I,
)
_AUTH_WALL_LABEL_RE = re.compile(
    r'(sign[\s-]?in|log[\s-]?in|please (?:log|sign)\s+in|enter your password)',
    re.I,
)


@dataclass
class AuthWallResult:
    """Pure detector output. ``present`` = navigation landed somewhere the bot can't
    proceed (login form, account-required redirect). ``signal`` records which rule
    fired so the event log and dashboard can show *why*."""

    present: bool
    signal: str = ""


def detect_login_wall(url: str, html: str) -> AuthWallResult:
    """Was the navigation kicked to a login page?

    Pure function over (url, html) so the same detector works against a live
    Playwright page and against saved HTML fixtures. Conservative — both URL
    pattern OR (password input + sign-in label) must fire. Either alone is too
    noisy: ATS forms sometimes embed a "passwordless candidate sign-in" widget
    that has a password field but isn't blocking the apply form.

    Order: URL match wins outright (most reliable when navigation actually
    redirected); HTML-only match requires BOTH a password input AND a
    sign-in/log-in label/heading, which is rare on apply forms.
    """
    u = (url or "").lower()
    for marker in _LOGIN_URL_MARKERS:
        if marker in u:
            return AuthWallResult(True, signal=f"url:{marker}")

    h = html or ""
    if _AUTH_WALL_INPUT_RE.search(h) and _AUTH_WALL_LABEL_RE.search(h):
        return AuthWallResult(True, signal="html:password+signin_label")

    return AuthWallResult(False, signal="")
