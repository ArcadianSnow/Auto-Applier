"""CAPTCHA-presence survey (Phase 1 safe measurement, spec §11 risk ②).

Loads real Greenhouse application forms in **dry-run** (fill, never submit) and records
what anti-bot challenge each carries. This measures the *ceiling* of the auto-apply
problem — how prevalent invisible reCAPTCHA / Enterprise is across boards — WITHOUT
sending any applications. The complementary *auto-pass rate* (does the invisible challenge
actually clear) needs real submits and is a separate, gated run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from av3.pipeline.stage import new_run_id, stage
from av3.sources.browser.greenhouse_apply import Applicant, prepare_application
from av3.sources.browser.session import BrowserSession
from av3.sources.greenhouse import GreenhouseSource


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
    form_present: bool = True  # False ⇒ canonical URL redirected to a non-GH wrapper


def summarize_survey(rows: list[SurveyRow]) -> dict:
    """Aggregate survey rows into the headline distribution (pure; unit-tested)."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    by_type = Counter(r.captcha_type for r in rows)
    forms = [r for r in rows if r.form_present]
    nf = len(forms) or 1
    return {
        "n": n,
        "forms_present": len(forms),
        "by_captcha_type": dict(by_type),
        # percentages over real GH forms only (a wrapper redirect isn't a form to apply to)
        "pct_invisible_of_forms": round(100 * sum(r.is_invisible for r in forms) / nf, 1),
        "pct_enterprise_of_forms": round(100 * sum(r.enterprise for r in forms) / nf, 1),
        "pct_auto_eligible_of_forms": round(100 * sum(r.auto_eligible for r in forms) / nf, 1),
        "avg_custom_questions_of_forms": round(sum(r.custom_questions for r in forms) / nf, 1),
        "note": (
            "auto_eligible = no visible challenge + standard fields present; NOT the "
            "auto-pass rate (which requires real submits). Percentages are over forms_present."
        ),
    }


async def run_survey(
    tokens: list[str],
    applicant: Applicant,
    resume_path: str,
    profile_dir,
    max_jobs_per_token: int = 1,
) -> list[SurveyRow]:
    """Live dry-run survey across ``tokens``. Returns one row per job inspected."""
    new_run_id()
    rows: list[SurveyRow] = []
    gh = GreenhouseSource()
    session = BrowserSession(profile_dir)
    await session.start()
    try:
        page = await session.new_page()
        for token in tokens:
            try:
                listings = gh.discover(token)
            except Exception:  # noqa: BLE001
                continue
            for listing in listings[:max_jobs_per_token]:
                row = await _survey_one(page=page, listing=listing, applicant=applicant,
                                        resume_path=resume_path, platform="greenhouse")
                if row is not None:
                    rows.append(row)
    finally:
        await session.stop()
        gh.close()
    return rows


@stage("survey")
async def _survey_one(*, page, listing, applicant, resume_path, platform):
    outcome = await prepare_application(page, listing, applicant, resume_path, dry_run=True)
    # if the standard #first_name field is absent, the canonical URL didn't land on a
    # real Greenhouse form (e.g. a company wrapper/redirect — stripe → stripe.com).
    form_present = bool(outcome.filled.get("first_name") or outcome.filled.get("email"))
    return SurveyRow(
        token=listing.board_token,
        job_url=listing.url,
        title=listing.title,
        captcha_type=outcome.captcha.type.value,
        is_invisible=outcome.captcha.is_invisible,
        enterprise=outcome.captcha.enterprise,
        custom_questions=len(outcome.custom_questions),
        auto_eligible=outcome.auto_eligible and form_present,
        form_present=form_present,
    )
