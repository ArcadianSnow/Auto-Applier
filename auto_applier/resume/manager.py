"""Multi-resume management with AI-powered best-match selection.

The :class:`ResumeManager` stores multiple resumes (e.g. one tailored for
Data Analyst roles, another for Data Engineer) and selects the best match
for each job by scoring all profiles against the job description via LLM.

Resume files live in ``data/resumes/``.  Per-resume enhanced profiles
(parsed text, extracted skills, confirmed skills) live in
``data/profiles/<label>.json``.
"""

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from auto_applier.config import RESUMES_DIR, PROFILES_DIR
from auto_applier.resume.parser import extract_text
from auto_applier.llm.router import LLMRouter
from auto_applier.llm.prompts import RESUME_SELECT, SKILL_EXTRACT_RESUME

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


@dataclass
class ResumeInfo:
    """Metadata about a loaded resume."""

    label: str
    file_path: Path
    profile_path: Path
    raw_text: str = ""
    skill_count: int = 0


@dataclass
class ResumeScore:
    """Score of a single resume against a job description."""

    resume: ResumeInfo
    score: int  # 1-10
    explanation: str = ""
    matched_skills: list = field(default_factory=list)
    missing_skills: list = field(default_factory=list)


# ------------------------------------------------------------------
# Manager
# ------------------------------------------------------------------


class ResumeManager:
    """Manages multiple resumes and their enhanced profiles.

    Typical workflow::

        mgr = ResumeManager(router)
        await mgr.add_resume("~/Desktop/da_resume.pdf", "data_analyst")
        await mgr.add_resume("~/Desktop/de_resume.pdf", "data_engineer")
        best, score = await mgr.get_best_match(job_description_text)
    """

    def __init__(self, router: LLMRouter) -> None:
        self.router = router
        RESUMES_DIR.mkdir(parents=True, exist_ok=True)
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Add / remove
    # ------------------------------------------------------------------

    async def add_resume(self, source_path: str | Path, label: str) -> ResumeInfo:
        """Add a resume: copy to data/resumes/, parse text, create profile via LLM.

        Args:
            source_path: Path to the original PDF/DOCX file.
            label: Human-readable label (e.g. ``"data_analyst"``).  Used as
                the filename stem for both the copied file and the profile.

        Returns:
            A :class:`ResumeInfo` with metadata about the newly added resume.

        Raises:
            FileNotFoundError: If *source_path* does not exist.
        """
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Resume not found: {source}")

        # Copy to resumes directory with label-based name
        dest = RESUMES_DIR / f"{label}{source.suffix}"
        shutil.copy2(source, dest)
        logger.info("Copied resume to %s", dest)

        # Parse text
        raw_text = extract_text(dest)

        # Extract skills via LLM
        skills_data = await self._extract_skills(raw_text)

        # Build and save profile
        profile = {
            "label": label,
            "source_file": dest.name,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "raw_text": raw_text,
            "summary": "",  # Could be enriched by LLM later
            "skills": skills_data.get("technical_skills", []),
            "tools": skills_data.get("tools", []),
            "certifications": skills_data.get("certifications", []),
            "soft_skills": skills_data.get("soft_skills", []),
            "experience": [],
            "education": [],
            "confirmed_skills": [],
        }

        profile_path = PROFILES_DIR / f"{label}.json"
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        logger.info(
            "Created profile for '%s' (%d skills, %d tools)",
            label,
            len(profile["skills"]),
            len(profile["tools"]),
        )

        return ResumeInfo(
            label=label,
            file_path=dest,
            profile_path=profile_path,
            raw_text=raw_text,
            skill_count=len(profile["skills"]) + len(profile["tools"]),
        )

    def remove_resume(self, label: str) -> None:
        """Remove a resume and its profile from disk.

        Silently succeeds if the resume or profile does not exist.
        """
        profile_path = PROFILES_DIR / f"{label}.json"
        if profile_path.exists():
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
            resume_file = RESUMES_DIR / profile.get("source_file", "")
            if resume_file.exists():
                resume_file.unlink()
                logger.info("Deleted resume file %s", resume_file)
            profile_path.unlink()
            logger.info("Deleted profile %s", profile_path)

    # ------------------------------------------------------------------
    # Listing / querying
    # ------------------------------------------------------------------

    def list_resumes(self) -> list[ResumeInfo]:
        """List all loaded resumes with their metadata.

        Returns a list sorted alphabetically by label.
        """
        resumes: list[ResumeInfo] = []
        for profile_path in sorted(PROFILES_DIR.glob("*.json")):
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    profile = json.load(f)
                resume_file = RESUMES_DIR / profile.get("source_file", "")
                resumes.append(
                    ResumeInfo(
                        label=profile["label"],
                        file_path=resume_file,
                        profile_path=profile_path,
                        raw_text=profile.get("raw_text", ""),
                        skill_count=(
                            len(profile.get("skills", []))
                            + len(profile.get("tools", []))
                        ),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping corrupt profile: %s", profile_path)
                continue
        return resumes

    def get_resume(self, label: str) -> ResumeInfo | None:
        """Get a specific resume by label, or ``None`` if not found."""
        profile_path = PROFILES_DIR / f"{label}.json"
        if not profile_path.exists():
            return None
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        resume_file = RESUMES_DIR / profile.get("source_file", "")
        return ResumeInfo(
            label=profile["label"],
            file_path=resume_file,
            profile_path=profile_path,
            raw_text=profile.get("raw_text", ""),
            skill_count=len(profile.get("skills", [])) + len(profile.get("tools", [])),
        )

    def get_profile(self, label: str) -> dict:
        """Load the full enhanced profile JSON for a resume.

        Returns an empty dict if the profile does not exist.
        """
        profile_path = PROFILES_DIR / f"{label}.json"
        if not profile_path.exists():
            return {}
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_resume_text(self, label: str) -> str:
        """Get the text content to use for LLM prompts.

        Uses the raw parsed text enriched with any confirmed skills that
        were added through the evolution system.  Falls back to raw text
        if no confirmed skills exist.
        """
        profile = self.get_profile(label)
        if not profile:
            return ""

        # Start with original resume text
        parts = [profile.get("raw_text", "")]

        # Append confirmed skills that aren't in the original resume
        confirmed = profile.get("confirmed_skills", [])
        if confirmed:
            parts.append("\n\nAdditional Confirmed Skills:")
            for skill in confirmed:
                name = skill.get("name", "")
                level = skill.get("level", "")
                bullets = skill.get("bullets", [])
                parts.append(f"\n{name} ({level}):")
                for bullet in bullets:
                    parts.append(f"  - {bullet}")

        return "\n".join(parts)

    def save_profile(self, label: str, profile: dict) -> None:
        """Save an updated profile back to disk."""
        profile_path = PROFILES_DIR / f"{label}.json"
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        logger.debug("Saved profile for '%s'", label)

    # ------------------------------------------------------------------
    # Scoring / best-match selection
    # ------------------------------------------------------------------

    async def score_all(self, job_description: str) -> list[ResumeScore]:
        """Score every resume against a job description.

        Returns a list of :class:`ResumeScore` sorted best-first
        (highest score first).
        """
        resumes = self.list_resumes()
        if not resumes:
            return []

        scores: list[ResumeScore] = []
        for resume in resumes:
            score = await self._score_single(resume, job_description)
            scores.append(score)

        # Sort by score descending
        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    async def get_best_match(
        self, job_description: str
    ) -> tuple[ResumeInfo | None, ResumeScore | None]:
        """Get the best-matching resume for a job.

        Returns ``(resume_info, score)`` or ``(None, None)`` if no
        resumes are loaded.
        """
        scores = await self.score_all(job_description)
        if not scores:
            return None, None
        return scores[0].resume, scores[0]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _score_single(
        self, resume: ResumeInfo, job_description: str
    ) -> ResumeScore:
        """Score a single resume against a job description via LLM."""
        resume_text = self.get_resume_text(resume.label)

        try:
            result = await self.router.complete_json(
                prompt=RESUME_SELECT.format(
                    resume_label=resume.label,
                    resume_text=resume_text[:3000],  # Truncate to fit context
                    job_description=job_description[:2000],
                ),
                system_prompt=RESUME_SELECT.system,
            )
            return ResumeScore(
                resume=resume,
                score=min(10, max(1, int(result.get("score", 5)))),
                explanation=result.get("explanation", ""),
                matched_skills=result.get("matched_skills", []),
                missing_skills=result.get("missing_skills", []),
            )
        except Exception as exc:
            # On LLM failure, return neutral score so we can still proceed
            logger.warning("Resume scoring failed for '%s': %s", resume.label, exc)
            return ResumeScore(
                resume=resume, score=5, explanation="Scoring unavailable"
            )

    async def _extract_skills(self, resume_text: str) -> dict:
        """Extract skills from resume text via LLM."""
        try:
            return await self.router.complete_json(
                prompt=SKILL_EXTRACT_RESUME.format(
                    resume_text=resume_text[:4000]
                ),
                system_prompt=SKILL_EXTRACT_RESUME.system,
            )
        except Exception as exc:
            logger.warning("Skill extraction failed: %s", exc)
            return {
                "technical_skills": [],
                "soft_skills": [],
                "certifications": [],
                "tools": [],
            }
