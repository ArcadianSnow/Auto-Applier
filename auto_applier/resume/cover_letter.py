"""AI-generated cover letters tailored to each job.

Uses the LLM router to produce a concise, job-specific cover letter that
maps the candidate's strengths to the requirements in the job description.
"""

import logging

from auto_applier.llm.router import LLMRouter
from auto_applier.llm.prompts import COVER_LETTER

logger = logging.getLogger(__name__)


class CoverLetterWriter:
    """Generates tailored cover letters using the LLM fallback chain.

    Usage::

        writer = CoverLetterWriter(router)
        text = await writer.generate(
            resume_text=resume_text,
            job_description=jd,
            company_name="Acme Corp",
            job_title="Data Analyst",
        )
    """

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def generate(
        self,
        resume_text: str,
        job_description: str,
        company_name: str,
        job_title: str,
    ) -> str:
        """Generate a cover letter tailored to the specific job.

        Args:
            resume_text: Full or enriched text of the selected resume.
            job_description: The job posting description.
            company_name: Name of the hiring company.
            job_title: Title of the position.

        Returns:
            The cover letter text, or an empty string on failure.
        """
        prompt = COVER_LETTER.format(
            resume_text=resume_text[:3000],
            job_description=job_description[:2000],
            company_name=company_name,
            job_title=job_title,
        )

        try:
            response = await self.router.complete(
                prompt=prompt,
                system_prompt=COVER_LETTER.system,
                temperature=0.4,
                max_tokens=800,
            )
            text = response.text.strip()
            if text:
                logger.info(
                    "Generated cover letter for %s at %s (%d chars)",
                    job_title,
                    company_name,
                    len(text),
                )
            return text
        except Exception as exc:
            logger.warning(
                "Cover letter generation failed for %s at %s: %s",
                job_title,
                company_name,
                exc,
            )
            return ""
