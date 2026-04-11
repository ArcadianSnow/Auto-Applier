"""AI-powered form filling engine shared by all platform adapters.

Fills application form fields using a priority chain:
1. Personal info match (name, email, phone, etc.)
2. Contextual match (how did you hear, previously worked here, start date)
3. answers.json match (exact then fuzzy)
4. LLM generation (via the fallback router)
5. Record as skill gap (unfilled field)

Also handles cover letter generation, file upload detection, and
native date input pickers.
"""
import difflib
import json
import logging
import re
from datetime import date, timedelta

from playwright.async_api import Page

from auto_applier.browser.anti_detect import human_fill, random_delay
from auto_applier.browser.selector_utils import FormField
from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.llm.prompts import FORM_FILL
from auto_applier.llm.router import LLMRouter
from auto_applier.resume.cover_letter import CoverLetterWriter
from auto_applier.storage.dedup import normalize_company
from auto_applier.storage.models import SkillGap

logger = logging.getLogger(__name__)


# Keywords that identify specific personal info fields.
#
# Order matters: more specific keys come first so "zip code" matches
# before the generic "zip" substring, "city, state" before plain
# "city", etc. Python dict iteration preserves insertion order.
PERSONAL_INFO_KEYS: dict[str, str] = {
    # Name
    "first name": "first_name",
    "last name": "last_name",
    "full name": "full_name",
    # Contact
    "email": "email",
    "phone": "phone",
    "mobile": "phone",
    # Location — compound labels FIRST so they beat the plain-word ones
    "city, state": "city_state",
    "city/state": "city_state",
    "city state": "city_state",
    "zip code": "zip_code",
    "zipcode": "zip_code",
    "postal code": "postal_code",
    "postcode": "postal_code",
    "street address": "street_address",
    "mailing address": "address",
    "home address": "address",
    # Single-word location keys — last so compounds win
    "address": "address",
    "street": "street_address",
    "zip": "zip_code",
    "postal": "postal_code",
    "state": "state",
    "province": "state",
    "region": "state",
    "country": "country",
    "city": "city",
    "location": "city",
    # Social / web
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "website": "portfolio_url",
    "personal site": "portfolio_url",
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

# Keywords for contextual auto-answers. Each group is matched
# substring-wise against the lowercased label.
SOURCE_QUESTION_KEYWORDS = [
    "how did you hear",
    "where did you hear",
    "how did you find",
    "where did you find",
    "how were you referred",
    "source of application",
    "referral source",
    "where did you learn",
    "how did you discover",
    "where did you discover",
    "how did you come across",
    "source of this application",
    "job source",
    "application source",
]

# A looser regex fallback for source-attribution questions — catches
# variants like 'where did you FIRST hear about this role' and 'how
# did you eventually find this job' that the fixed keyword list
# above misses because of extra words between the verbs.
SOURCE_QUESTION_REGEX = re.compile(
    r"(how|where)\s+(?:did|do)\s+you\s+(?:\w+\s+){0,3}(hear|find|discover|learn|come\s+across)",
    re.IGNORECASE,
)

PREVIOUSLY_WORKED_KEYWORDS = [
    "previously worked",
    "former employee",
    "formerly employed",
    "ever worked for",
    "worked here before",
    "prior employment with",
    "past employee",
]

START_DATE_KEYWORDS = [
    "start date",
    "available start",
    "earliest start",
    "when can you start",
    "availability date",
    "date you can start",
    "date available",
]

# Default start-date offset used when a form asks for the earliest
# date the candidate can begin. Two weeks is the conventional notice
# period in most industries.
DEFAULT_START_DATE_OFFSET_DAYS = 14


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
        platform_display_name: str = "",
    ) -> None:
        self.router = router
        self.personal_info = personal_info
        self.resume_text = resume_text
        self.job_description = job_description
        self.company_name = company_name
        self.job_title = job_title
        self.resume_label = resume_label
        # Platform display name like "LinkedIn" or "Indeed", used for
        # the "how did you hear about this?" auto-answer. Empty string
        # means the contextual matcher will fall through to the LLM.
        self.platform_display_name = platform_display_name
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

        Every step of the priority chain emits a DEBUG log line so
        run-log files capture exactly how each field was resolved
        (or why it wasn't). When something goes wrong mid-form-fill,
        the log shows the label, the field type, the options list,
        which layer matched, what value was returned, and whether
        the apply step actually wrote to the DOM.
        """
        self.fields_total += 1
        label_lower = field.label.lower()

        logger.debug(
            "fill_field: label=%r type=%s options=%s",
            field.label, field.field_type,
            field.options if field.options else "none",
        )

        # File upload fields are handled by the platform adapter
        if field.field_type == "file" and any(
            kw in label_lower for kw in RESUME_UPLOAD_KEYWORDS
        ):
            logger.debug("  → file upload, deferring to platform")
            return False

        # Cover letter fields get special treatment
        if any(kw in label_lower for kw in COVER_LETTER_KEYWORDS):
            logger.debug("  → cover letter field, generating")
            ok = await self._fill_cover_letter(page, field)
            logger.debug("  ← cover letter fill result: %s", ok)
            return ok

        # Priority 1: Personal info match
        answer = self._match_personal_info(label_lower)
        if answer:
            logger.debug("  matched personal_info → %r", answer)

        # Priority 2: Contextual auto-answers (source, prior employment,
        # start date). These are deterministic and free — no LLM cost.
        if not answer:
            answer = self._match_contextual(label_lower, field)
            if answer:
                logger.debug("  matched contextual → %r", answer)

        # Priority 3: answers.json match
        if not answer:
            answer = self._match_answers(field.label)
            if answer:
                logger.debug("  matched answers.json → %r", answer)

        # Priority 4: LLM generation
        if not answer:
            logger.debug("  no deterministic match, calling LLM...")
            answer = await self._generate_answer(field)
            if answer:
                self.used_llm = True
                logger.debug("  LLM returned → %r", answer)

        # Priority 5: Record as gap
        if not answer:
            logger.debug("  → NO ANSWER FOUND, recording as skill gap")
            self._record_gap(field, job_id)
            self._record_unanswered(field.label)
            return False

        ok = await self._apply_answer(page, field, answer)
        logger.debug("  ← apply_answer returned %s for %r", ok, field.label)
        return ok

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
    # Priority 2: Contextual auto-answers
    # ------------------------------------------------------------------

    def _match_contextual(self, label_lower: str, field: FormField) -> str:
        """Answer context-dependent questions from state the filler already has.

        Three cases, all deterministic and free:

        - **Source attribution** ('how did you hear about this?'):
          reply with the platform the applicant is applying from.

        - **Previous employment** ('have you worked for us before?'):
          check whether the hiring company name appears in the
          candidate's resume, using the same canonical-name
          normalization we use for dedup so 'Acme' matches 'Acme Inc.'.

        - **Earliest start date**: return a date 14 days from today
          in ISO format. The _apply_answer date branch will normalize
          it for whatever date widget the site is using.
        """
        # Source attribution — only if we know which platform we're on
        if self.platform_display_name:
            if any(kw in label_lower for kw in SOURCE_QUESTION_KEYWORDS):
                return self.platform_display_name
            if SOURCE_QUESTION_REGEX.search(label_lower):
                return self.platform_display_name

        # Previously worked for this company
        if any(kw in label_lower for kw in PREVIOUSLY_WORKED_KEYWORDS):
            return self._check_prior_employment()

        # Earliest start date
        if any(kw in label_lower for kw in START_DATE_KEYWORDS):
            default = date.today() + timedelta(
                days=DEFAULT_START_DATE_OFFSET_DAYS
            )
            return default.isoformat()

        return ""

    def _check_prior_employment(self) -> str:
        """Return 'Yes' or 'No' based on resume vs company-name match.

        Matches canonically (lowercase, corporate suffixes stripped)
        so 'Acme Inc.' on the resume counts as having worked at 'Acme
        Corporation'. Returns 'No' if either the company or resume
        text is empty — safer than returning nothing and asking the
        user.
        """
        if not self.company_name or not self.resume_text:
            return "No"

        canonical = normalize_company(self.company_name)
        if not canonical:
            return "No"
        # Also test the un-normalized form in case the resume has the
        # company name with its original casing / suffix intact.
        resume_lower = self.resume_text.lower()
        if canonical in resume_lower:
            return "Yes"
        raw = self.company_name.lower().strip()
        if raw and raw in resume_lower:
            return "Yes"
        return "No"

    # ------------------------------------------------------------------
    # Priority 2: Answers.json
    # ------------------------------------------------------------------

    def _match_answers(self, label: str) -> str:
        """Match field label against answers.json.

        Two-pass match:
        1. Exact case-insensitive match against the question or any
           of its aliases. Aliases let the user register short
           keyword variants (e.g. 'work authorization' as an alias
           for 'Are you authorized to work in this country?').
        2. Substring containment in either direction — the field
           label contains the question, or vice versa. Handles
           forms that pad questions with extra boilerplate.
        3. Fuzzy match (SequenceMatcher, 60% threshold) as a last
           resort.
        """
        answers = self._load_answers()
        label_lower = label.lower().strip()

        # Pass 1: exact match on question or any alias
        for entry in answers:
            q = entry.get("question", "").lower().strip()
            if q and q == label_lower:
                logger.debug("Exact answers.json match for: '%s'", label)
                return entry.get("answer", "")
            for alias in entry.get("aliases", []) or []:
                if alias and alias.lower().strip() == label_lower:
                    logger.debug(
                        "Alias match '%s' -> '%s'", alias, entry.get("question"),
                    )
                    return entry.get("answer", "")

        # Pass 2: substring containment (either direction)
        for entry in answers:
            q = entry.get("question", "").lower().strip()
            if not q:
                continue
            if q in label_lower or label_lower in q:
                logger.debug(
                    "Substring answers.json match for '%s' against '%s'",
                    label, entry.get("question"),
                )
                return entry.get("answer", "")
            # Alias substring too
            for alias in entry.get("aliases", []) or []:
                a = alias.lower().strip()
                if a and (a in label_lower or label_lower in a):
                    return entry.get("answer", "")

        # Pass 3: fuzzy
        best_match = ""
        best_ratio = 0.0
        for entry in answers:
            q = entry.get("question", "").lower()
            if not q:
                continue
            ratio = difflib.SequenceMatcher(None, label_lower, q).ratio()
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
            company_name=self.company_name or "(not specified)",
            question=field.label,
        )

        # For select fields, include the available options
        if field.field_type == "select" and field.options:
            prompt_text += (
                f"\n\nAvailable options (choose exactly one): "
                f"{', '.join(field.options)}"
            )

        # For number fields, ask explicitly for an integer
        if field.field_type == "number":
            prompt_text += (
                "\n\nThis field accepts only a number. Reply with one "
                "integer and nothing else."
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

            elif field.field_type == "date":
                # Native HTML date pickers accept YYYY-MM-DD via .fill().
                # Normalize whatever string we got into that format;
                # fall back to the raw string if parsing fails.
                iso = self._coerce_iso_date(answer)
                await field.element.fill(iso)
                self.fields_filled += 1
                await random_delay(0.3, 0.8)
                return True

            elif field.field_type == "number":
                # Strip non-digits and fill as-is — browsers validate
                # number inputs on submit.
                digits = "".join(c for c in answer if c.isdigit() or c == ".")
                if not digits:
                    return False
                await field.element.fill(digits)
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
                "Failed to fill field %r (%s): %s",
                field.label,
                field.field_type,
                exc,
            )
            logger.debug(
                "apply_answer exception detail: value=%r field.options=%s",
                answer, field.options,
                exc_info=True,
            )
        return False

    @staticmethod
    def _coerce_iso_date(value: str) -> str:
        """Return a YYYY-MM-DD date string.

        Accepts the already-ISO output of the contextual matcher, the
        common US / EU date formats the LLM might produce, and
        natural-language offsets like 'in two weeks'. Falls back to
        two weeks from today for anything it can't parse — never
        raises, so form filling keeps moving.
        """
        from datetime import datetime

        fallback = (date.today() + timedelta(days=DEFAULT_START_DATE_OFFSET_DAYS)).isoformat()
        s = (value or "").strip()
        if not s:
            return fallback

        # Already ISO (YYYY-MM-DD)
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            pass

        # Common alternative formats
        for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue

        return fallback

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
        """Load pre-configured answers from answers.json.

        Historically the file has been written in three different
        shapes by different parts of the codebase:

        1. Flat dict (what the wizard's save_answers produces)::

            {"Q1": "A1", "Q2": "A2"}

        2. List of entries (what the form filler originally
           assumed)::

            [{"question": "Q1", "answer": "A1"}, ...]

        3. Wrapped-list (what the fixture generator produces)::

            {"questions": [{"question": "Q1", "answer": "A1",
                            "aliases": [...]}]}

        This loader accepts all three and returns a canonical list
        of ``{"question": str, "answer": str, "aliases": [str]}``
        dicts. When the shape is unrecognized, returns an empty
        list and logs a warning once per run.
        """
        if not ANSWERS_FILE.exists():
            return []
        try:
            with open(ANSWERS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        normalized: list[dict] = []

        # Shape 1: flat {question: answer}
        if isinstance(raw, dict) and "questions" not in raw:
            for q, a in raw.items():
                if not isinstance(q, str):
                    continue
                normalized.append({
                    "question": q,
                    "answer": str(a) if a is not None else "",
                    "aliases": [],
                })
            return normalized

        # Shape 3: {"questions": [...]}
        if isinstance(raw, dict) and "questions" in raw:
            raw = raw.get("questions", [])

        # Shape 2: list of entries
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                question = str(entry.get("question", "")).strip()
                answer = str(entry.get("answer", "")).strip()
                if not question:
                    continue
                aliases_raw = entry.get("aliases", [])
                if not isinstance(aliases_raw, list):
                    aliases_raw = []
                aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
                normalized.append({
                    "question": question,
                    "answer": answer,
                    "aliases": aliases,
                })
            return normalized

        logger.warning(
            "answers.json has an unrecognized format (type=%s); "
            "no pre-canned answers will be used this run",
            type(raw).__name__,
        )
        return []
