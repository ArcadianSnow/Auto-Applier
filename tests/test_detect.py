"""CAPTCHA + confirmation detectors (spec §8b, risk ④). Fixture-driven, no live browser."""

from __future__ import annotations

from auto_applier.sources.browser.detect import (
    CaptchaType,
    ConfirmationOutcome,
    classify_captcha,
    detect_confirmation,
)


# --------------------------------------------------------------------- CAPTCHA
def test_no_captcha():
    r = classify_captcha("<form><input id='email'></form>", scripts=[])
    assert r.type is CaptchaType.NONE and not r.present


def test_greenhouse_invisible_recaptcha():
    html = '<textarea id="g-recaptcha-response" name="g-recaptcha-response"></textarea>'
    scripts = ["https://www.gstatic.com/recaptcha/releases/abc/recaptcha__en.js"]
    r = classify_captcha(html, scripts)
    assert r.type is CaptchaType.RECAPTCHA_INVISIBLE
    assert r.is_invisible and not r.enterprise


def test_recaptcha_enterprise_detected_from_script():
    html = '<textarea id="g-recaptcha-response"></textarea>'
    scripts = ["https://www.google.com/recaptcha/enterprise.js?render=KEY"]
    r = classify_captcha(html, scripts)
    assert r.type is CaptchaType.RECAPTCHA_ENTERPRISE
    assert r.enterprise and r.is_invisible


def test_visible_recaptcha_checkbox():
    html = '<div class="g-recaptcha" data-size="normal" data-sitekey="x"></div>'
    r = classify_captcha(html, scripts=["https://www.google.com/recaptcha/api.js"])
    assert r.type is CaptchaType.RECAPTCHA_CHECKBOX
    assert not r.is_invisible  # visible → assisted


def test_visible_challenge_frame():
    html = '<iframe title="recaptcha challenge expires in two minutes"></iframe>'
    r = classify_captcha(html)
    assert r.type is CaptchaType.VISIBLE_CHALLENGE
    assert not r.is_invisible


def test_lever_hcaptcha():
    html = '<input type="hidden" name="h-captcha-response" id="hcaptchaResponseInput">'
    r = classify_captcha(html)
    assert r.type is CaptchaType.HCAPTCHA and r.is_invisible


# ---------------------------------------------------------------- CONFIRMATION
def test_greenhouse_confirmation_url():
    r = detect_confirmation(
        "https://job-boards.greenhouse.io/acme/jobs/123/confirmation", "<h1>Thanks!</h1>"
    )
    assert r.outcome is ConfirmationOutcome.CONFIRMED
    assert r.signal == "url:/confirmation"


def test_lever_thanks_url():
    r = detect_confirmation("https://jobs.lever.co/acme/uuid/thanks", "")
    assert r.outcome is ConfirmationOutcome.CONFIRMED


def test_success_text_without_redirect():
    # Ashby SPA case: no URL change, in-place panel
    r = detect_confirmation(
        "https://jobs.ashbyhq.com/acme/uuid/application",
        "<div>Application submitted. We have received your application.</div>",
    )
    assert r.outcome is ConfirmationOutcome.CONFIRMED
    assert r.signal == "success_text"


def test_captcha_error_is_not_success():
    # GH "Please complete the reCAPTCHA and resubmit" → challenge, NOT applied
    r = detect_confirmation(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "<p>Please complete the reCAPTCHA and resubmit your application</p>",
    )
    assert r.outcome is ConfirmationOutcome.CAPTCHA_CHALLENGE


def test_validation_error():
    r = detect_confirmation(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        '<span class="error">This field is required</span>',
    )
    assert r.outcome is ConfirmationOutcome.FAILED_VALIDATION


def test_no_signal_is_unconfirmed_not_applied():
    # the safety-critical case: a click with no positive signal must NEVER be APPLIED
    r = detect_confirmation("https://job-boards.greenhouse.io/acme/jobs/123", "<div></div>")
    assert r.outcome is ConfirmationOutcome.UNCONFIRMED


def test_error_beats_success_text():
    # both a stray "thank you" and a validation error present → must not confirm
    html = "<p>thank you for your interest</p><span class='error'>field is required</span>"
    r = detect_confirmation("https://x/jobs/1", html)
    assert r.outcome is ConfirmationOutcome.FAILED_VALIDATION
