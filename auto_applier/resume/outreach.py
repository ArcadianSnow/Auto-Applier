"""LinkedIn outreach / connection message generator.

A lightweight sibling of ``cover_letter.py``. Produces a short,
personal connection-request message (under 280 characters, LinkedIn's
hard cap) that references one concrete skill from the candidate's
resume and ties it to the target role. Useful when the user wants
to message a recruiter or hiring manager directly instead of — or
in addition to — a standard application.
"""

from __future__ import annotations

import logging

from auto_applier.llm.prompts import OUTREACH_MESSAGE
from auto_applier.llm.router import LLMRouter

logger = logging.getLogger(__name__)

LINKEDIN_CONNECTION_LIMIT = 280


class OutreachWriter:
    """Generates LinkedIn connection-request messages via the LLM router."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def generate(
        self,
        resume_text: str,
        job_description: str,
        company_name: str,
        job_title: str,
    ) -> str:
        """Return a connection-request message, or empty string on failure.

        Truncates the result to LinkedIn's 280-character limit if the
        LLM overshoots. Never raises — callers typically display the
        result to the user who can edit before sending.
        """
        prompt = OUTREACH_MESSAGE.format(
            resume_text=resume_text[:2000],
            job_description=job_description[:1200],
            company_name=company_name,
            job_title=job_title,
        )
        try:
            response = await self.router.complete(
                prompt=prompt,
                system_prompt=OUTREACH_MESSAGE.system,
                temperature=0.6,
                max_tokens=200,
            )
            text = response.text.strip()
        except Exception as exc:
            logger.warning(
                "Outreach generation failed for %s @ %s: %s",
                job_title, company_name, exc,
            )
            return ""

        # Strip accidental markdown or quotes
        text = text.strip('"').strip("'").strip("*").strip()
        # Drop any leading salutation the LLM may have added despite instructions
        for prefix in ("hi ", "hello ", "hey "):
            if text.lower().startswith(prefix):
                newline = text.find("\n")
                if 0 < newline < 50:
                    text = text[newline + 1:].strip()
                    break
        # Enforce LinkedIn's hard character limit
        if len(text) > LINKEDIN_CONNECTION_LIMIT:
            trimmed = text[:LINKEDIN_CONNECTION_LIMIT]
            last_period = max(
                trimmed.rfind("."), trimmed.rfind("?"), trimmed.rfind("!"),
            )
            if last_period > 100:
                text = trimmed[: last_period + 1]
            else:
                # Leave room for the ellipsis within the cap.
                text = text[: LINKEDIN_CONNECTION_LIMIT - 3].rstrip() + "..."
        return text
