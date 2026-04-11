"""Tailored follow-up email drafter.

Sibling of ``cover_letter.py`` and ``outreach.py``. Takes a stored
job + the user's resume + an attempt number (1, 2, or 3) and
produces a short follow-up email body ready for the user to copy
into their own mail client. The user sends the email themselves —
we never touch SMTP.

Tone varies by attempt:

- **Attempt 1** (day 7 by default): warm, enthusiastic, short
  check-in on timeline.
- **Attempt 2** (day 14): polite but more direct, adds one new
  signal the original application didn't have.
- **Attempt 3** (day 21): respectful 'closing the loop' message
  that pivots to a request to stay in touch for future openings.
"""

from __future__ import annotations

import logging

from auto_applier.llm.prompts import FOLLOWUP_EMAIL
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)

FOLLOWUP_EMAIL_MAX_WORDS = 150


class FollowupEmailWriter:
    """Generates follow-up email drafts via the LLM router."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def generate(
        self,
        resume_text: str,
        job_description: str,
        company_name: str,
        job_title: str,
        attempt: int,
        days_since: int,
    ) -> str:
        """Return an email body string, or empty on LLM failure.

        Clamps ``attempt`` to 1-3. Any value above 3 is treated as
        attempt 3 (closing-the-loop tone) so runaway followup counts
        don't produce increasingly desperate messages.
        """
        attempt = max(1, min(3, int(attempt)))
        prompt = FOLLOWUP_EMAIL.format(
            resume_text=resume_text[:2000],
            job_description=job_description[:1200],
            company_name=company_name,
            job_title=job_title,
            days_since=str(days_since),
            attempt=str(attempt),
        )
        try:
            response = await self.router.complete(
                prompt=prompt,
                system_prompt=FOLLOWUP_EMAIL.system,
                temperature=0.5,
                max_tokens=300,
            )
            text = response.text.strip()
        except Exception as exc:
            logger.warning(
                "Follow-up email generation failed for %s @ %s: %s",
                job_title, company_name, exc,
            )
            return ""

        # Strip leading / trailing quotes and markdown stars the LLM
        # sometimes wraps prose in.
        text = text.strip('"').strip("'").strip("*").strip()

        # Strip an accidental 'Hi Name,' / 'Hello,' opener the LLM
        # may add despite the system prompt saying not to.
        for prefix in ("hi ", "hello ", "hey ", "dear "):
            if text.lower().startswith(prefix):
                newline = text.find("\n")
                if 0 < newline < 80:
                    text = text[newline + 1:].strip()
                    break

        # Soft word-count cap — if the LLM overshoots, trim to the
        # last sentence boundary under the cap so we don't cut
        # mid-word.
        words = text.split()
        if len(words) > FOLLOWUP_EMAIL_MAX_WORDS:
            trimmed = " ".join(words[:FOLLOWUP_EMAIL_MAX_WORDS])
            last_period = max(
                trimmed.rfind("."), trimmed.rfind("?"), trimmed.rfind("!"),
            )
            if last_period > 60:
                text = trimmed[: last_period + 1]
            else:
                text = trimmed + "..."
        return text
