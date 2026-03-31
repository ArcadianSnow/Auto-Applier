"""AI-powered form filling engine shared by all platform adapters.

Fills application form fields using a priority chain:
1. Personal info match (name, email, phone, etc.)
2. answers.json match (exact then fuzzy)
3. LLM generation (via the fallback router)
4. Record as skill gap (unfilled field)

Also handles cover letter generation and file upload detection.
"""
import difflib
import json
import logging
from pathlib import Path

from playwright.async_api import Page

from auto_applier.browser.anti_detect import human_fill, human_type, random_delay
from auto_applier.browser.selector_utils import FormField
from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.llm.prompts import FORM_FILL
from auto_applier.llm.router import LLMRouter
from auto_applier.resume.cover_letter import CoverLetterWriter
from auto_applier.storage.models import SkillGap

logger = logging.getLogger(__name__)


# Keywords that identify specific personal info fields
PERSONAL_INFO_KEYS: dict[str, str] = {
    "first name": "first_name",
    "last name": "last_name",
    "full name": "full_name",
    "email": "email",
    "phone": "phone",
    "mobile": "phone",
    "city": "city",
    "location": "city",
    "linkedin": "linkedin_url",
    "website": "website",
    "portfolio": "website",
    "address": "address",
}

COVER_LETTER_KEYWORDS = [
    "cover letter",
    "letter of interest",
    "cover note",
    "motivation letter",
]

RESUME_UPLOAD_KEYWORDS = [
    "resume",
    "cv",
    "curriculum vitae",
    "upload your",
]


class FormFiller:
    """Fills application forms using a priority chain.

    Priority order:
    1. Personal info (name, email, phone, etc.)
    2. answers.json (exact match, then fuzzy at 60% threshold)
    3. LLM generation (resume + JD context)
    4. Record as skill gap (for resume improvement)
    """

    def __init__(
        self,
        router: LLMRouter,
        personal_info: dict,
        resume_text: str = "",
        job_description: str = "",
        company_name: str = "",
        job_title: str = "",
        resume_label: str = "",
    ) -> None:
        self.router = router
        self.personal_info = personal_info
        self.resume_text = resume_text
        self.job_description = job_description
        self.company_name = company_name
        self.job_title = job_title
        self.resume_label = resume_label
        self.cover_letter_writer = CoverLetterWriter(router)
        self.gaps: list[SkillGap] = []
        self.fields_filled: int = 0
        self.fields_total: int = 0
        self.used_llm: bool = False
        self.cover_letter_generated: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fill_field(
        self, page: Page, field: FormField, job_id: str = ""
    ) -> bool:
        """Fill a single form field using the priority chain.

        Returns True if the field was successfully filled.
        """
        self.fields_total += 1
        label_lower = field.label.lower()

        # File upload fields are handled by the platform adapter
        if field.field_type == "file" and any(
            kw in label_lower for kw in RESUME_UPLOAD_KEYWORDS
        ):
            return False

        # Cover letter fields get special treatment
        if any(kw in label_lower for kw in COVER_LETTER_KEYWORDS):
            return await self._fill_cover_letter(page, field)

        # Priority 1: Personal info match
        answer = self._match_personal_info(label_lower)

        # Priority 2: answers.json match
        if not answer:
            answer = self._match_answers(field.label)

        # Priority 3: LLM generation
        if not answer:
            answer = await self._generate_answer(field)
            if answer:
                self.used_llm = True

        # Priority 4: Record as gap
        if not answer:
            self._record_gap(field, job_id)
            self._record_unanswered(field.label)
            return False

        return await self._apply_answer(page, field, answer)

    # ------------------------------------------------------------------
    # Priority 1: Personal Info
    # ------------------------------------------------------------------

    def _match_personal_info(self, label_lower: str) -> str:
        """Match field label to personal info from config."""
        for keyword, config_key in PERSONAL_INFO_KEYS.items():
            if keyword in label_lower:
                value = self.personal_info.get(config_key, "")
                if value:
                    logger.debug(
                        "Personal info match: '%s' -> %s", keyword, config_key
                    )
                return value
        return ""

    # ------------------------------------------------------------------
    # Priority 2: Answers.json
    # ------------------------------------------------------------------

    def _match_answers(self, label: str) -> str:
        """Match field label against answers.json (exact then fuzzy)."""
        answers = self._load_answers()

        # Exact match (case-insensitive)
        for entry in answers:
            if entry.get("question", "").lower() == label.lower():
                logger.debug("Exact answers.json match for: '%s'", label)
                return entry.get("answer", "")

        # Fuzzy match (60% threshold)
        best_match = ""
        best_ratio = 0.0
        for entry in answers:
            ratio = difflib.SequenceMatcher(
                None, label.lower(), entry.get("question", "").lower()
            ).ratio()
            if ratio > best_ratio and ratio >= 0.6:
                best_ratio = ratio
                best_match = entry.get("answer", "")

        if best_match:
            logger.debug(
                "Fuzzy answers.json match for '%s' (%.0f%%)", label, best_ratio * 100
            )
        return best_match

    # ------------------------------------------------------------------
    # Priority 3: LLM Generation
    # ------------------------------------------------------------------

    async def _generate_answer(self, field: FormField) -> str:
        """Generate an answer via LLM using resume and JD context."""
        if not self.resume_text and not self.job_description:
            return ""

        prompt_text = FORM_FILL.format(
            resume_text=self.resume_text[:2000],
            job_description=self.job_description[:1500],
            question=field.label,
        )

        # For select fields, include the available options
        if field.field_type == "select" and field.options:
            prompt_text += (
                f"\n\nAvailable options (choose exactly one): "
                f"{', '.join(field.options)}"
            )

        try:
            response = await self.router.complete(
                prompt=prompt_text,
                system_prompt=FORM_FILL.system,
                temperature=0.2,
                max_tokens=200,
            )
            answer = response.text.strip()
            # Reject empty or uncertain answers
            if not answer or answer.lower() in ("n/a", "none", "unknown", ""):
                return ""
            logger.debug("LLM generated answer for '%s': %.50s...", field.label, answer)
            return answer
        except Exception as exc:
            logger.warning("LLM answer generation failed for '%s': %s", field.label, exc)
            return ""

    # ------------------------------------------------------------------
    # Cover Letter
    # ------------------------------------------------------------------

    async def _fill_cover_letter(self, page: Page, field: FormField) -> bool:
        """Generate and fill a cover letter field."""
        letter = await self.cover_letter_writer.generate(
            resume_text=self.resume_text[:3000],
            job_description=self.job_description[:2000],
            company_name=self.company_name,
            job_title=self.job_title,
        )
        if letter:
            self.cover_letter_generated = True
            return await self._apply_answer(page, field, letter)
        return False

    # ------------------------------------------------------------------
    # Apply Answer to Field
    # ------------------------------------------------------------------

    async def _apply_answer(
        self, page: Page, field: FormField, answer: str
    ) -> bool:
        """Apply an answer to a form field element."""
        try:
            if field.field_type in ("text", "textarea"):
                # Try human typing first via element ID
                el_id = await field.element.get_attribute("id")
                if el_id:
                    try:
                        await human_fill(page, f"#{el_id}", answer)
                    except Exception:
                        await field.element.fill(answer)
                else:
                    # No ID -- fill directly via element handle
                    await field.element.fill(answer)
                self.fields_filled += 1
                await random_delay(0.3, 0.8)
                return True

            elif field.field_type == "select":
                best_option = self._find_best_option(answer, field.options)
                if best_option:
                    await field.element.select_option(label=best_option)
                    self.fields_filled += 1
                    await random_delay(0.3, 0.8)
                    return True

            elif field.field_type == "radio":
                await field.element.check()
                self.fields_filled += 1
                return True

            elif field.field_type == "checkbox":
                if answer.lower() in ("yes", "true", "1"):
                    await field.element.check()
                self.fields_filled += 1
                return True

        except Exception as exc:
            logger.warning(
                "Failed to fill field '%s' (%s): %s",
                field.label,
                field.field_type,
                exc,
            )
        return False

    # ------------------------------------------------------------------
    # Select Option Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _find_best_option(answer: str, options: list[str]) -> str:
        """Find the closest matching option for a select field."""
        if not options:
            return ""
        answer_lower = answer.lower()

        # Exact match
        for opt in options:
            if opt.lower() == answer_lower:
                return opt

        # Substring match
        for opt in options:
            if answer_lower in opt.lower() or opt.lower() in answer_lower:
                return opt

        # Fuzzy match (50% cutoff)
        option_lowers = [o.lower() for o in options]
        matches = difflib.get_close_matches(
            answer_lower, option_lowers, n=1, cutoff=0.5
        )
        if matches:
            idx = option_lowers.index(matches[0])
            return options[idx]

        return ""

    # ------------------------------------------------------------------
    # Gap Recording
    # ------------------------------------------------------------------

    def _record_gap(self, field: FormField, job_id: str) -> None:
        """Record an unfilled field as a skill gap."""
        self.gaps.append(
            SkillGap(
                job_id=job_id,
                field_label=field.label,
                category=self._categorize_field(field.label),
                resume_label=self.resume_label,
                source="",
            )
        )
        logger.debug("Recorded skill gap: '%s' (%s)", field.label, self.gaps[-1].category)

    def _record_unanswered(self, question: str) -> None:
        """Record a new unanswered question for future wizard display."""
        unanswered: list[dict] = []
        if UNANSWERED_FILE.exists():
            try:
                with open(UNANSWERED_FILE, "r", encoding="utf-8") as f:
                    unanswered = json.load(f)
            except (json.JSONDecodeError, Exception):
                pass

        existing = {q.get("question", "").lower() for q in unanswered}
        if question.lower() not in existing:
            unanswered.append({"question": question, "encountered": 1})
        else:
            for q in unanswered:
                if q.get("question", "").lower() == question.lower():
                    q["encountered"] = q.get("encountered", 0) + 1
                    break

        try:
            with open(UNANSWERED_FILE, "w", encoding="utf-8") as f:
                json.dump(unanswered, f, indent=2)
        except Exception as exc:
            logger.warning("Failed to write unanswered file: %s", exc)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _categorize_field(label: str) -> str:
        """Categorize a field label into gap types."""
        label_lower = label.lower()
        if any(w in label_lower for w in ["certif", "license", "credential"]):
            return "certification"
        if any(
            w in label_lower
            for w in ["experience", "years", "how long", "how many"]
        ):
            return "experience"
        if any(
            w in label_lower
            for w in ["skill", "proficien", "familiar", "knowledge"]
        ):
            return "skill"
        return "other"

    @staticmethod
    def _load_answers() -> list[dict]:
        """Load pre-configured answers from answers.json."""
        if ANSWERS_FILE.exists():
            try:
                with open(ANSWERS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, Exception):
                pass
        return []
