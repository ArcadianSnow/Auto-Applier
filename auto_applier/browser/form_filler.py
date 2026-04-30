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
    # Work authorization — compound keys first so they beat short ones
    "how are you authorized to work": "work_auth",
    "what is your work authorization": "work_auth",
    "work authorization status": "work_auth",
    "work authorization": "work_auth",
    "authorization to work": "work_auth",
    "current work authorization": "work_auth",
    # Social / web
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "website": "portfolio_url",
    "personal site": "portfolio_url",
}

def _normalize_phone_for_field(raw: str) -> str:
    """Strip leading country code from a phone value.

    Most job sites (Indeed, LinkedIn, Dice) render phone fields with
    a separate country-code dropdown pre-selected to +1. Typing a
    value that starts with "+1" results in "+1+1 555 0100" which
    fails validation and silently disables the Continue button. We
    keep digits only, drop a leading US country code ("1") if the
    result is 11 digits, and preserve standard US formatting so the
    field still displays a human-readable number.
    """
    if not raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Return raw digits, not formatted. Indeed's phone input mask
    # silently drops any non-digit characters you type — including
    # the parentheses and dashes from "(206) 555-0100" — which can
    # leave the field in an intermediate state that fails validation
    # and disables the Continue button. Raw digits work on every
    # mask we've seen (Indeed, LinkedIn, Dice) because they either
    # accept them directly or auto-format on blur.
    return digits or raw


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

# Honeypot / anti-bot trap fields that humans never see. These are
# invisible inputs job sites add to their forms specifically to
# catch automation — if a submission has a non-empty value in one
# of these, the site knows it's a bot. NEVER fill.
HONEYPOT_KEYWORDS = [
    "leave this blank",
    "leave blank",
    "if you're a human",
    "if you are a human",
    "honeypot",
    "bot trap",
    "do not fill",
    "anti-spam",
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
    2. answers.json (exact match, substring, fuzzy at 60% with length guard)
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

    async def _field_already_has_value(self, field: FormField) -> bool:
        """True if the input already has a non-empty value.

        Job sites that know the user (LinkedIn, Indeed with an account)
        pre-fill many fields from the stored profile. Re-typing those
        values risks:
        - Overwriting a known-good value with a normalized variant
          that fails the site's validation (phone masks, zip formats)
        - Turning a pre-filled field into a dirty/edited state that
          re-triggers async validation
        - Wasting time on fields that already pass

        Skip anything that already reads non-empty. Only non-text
        inputs (radio, checkbox, select) bypass this check — their
        "value" semantics are different and their pre-fill detection
        lives inside each handler.
        """
        if field.field_type not in ("text", "textarea"):
            return False
        try:
            value = await field.element.input_value()
            return bool(value and value.strip())
        except Exception:
            return False

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

        # Honeypot check — skip invisible anti-bot trap fields before
        # anything else. Match by label keyword OR by visibility: a
        # field that isn't visible to the user is almost always a
        # honeypot (the exception is progressive reveal, which these
        # forms rarely use for text inputs).
        if any(kw in label_lower for kw in HONEYPOT_KEYWORDS):
            logger.debug("  → honeypot field by label, skipping")
            return False
        try:
            is_visible = await field.element.is_visible()
        except Exception:
            is_visible = True  # assume visible if the check itself fails
        if not is_visible:
            logger.debug("  → hidden field, skipping (likely honeypot)")
            return False

        # Pre-fill check — if the site already populated this field
        # from the signed-in user's profile, leave it alone. Re-typing
        # a value that's already there is how we broke Indeed's phone
        # input and zip validation in the last three runs.
        if await self._field_already_has_value(field):
            logger.debug("  → already pre-filled, skipping")
            return True

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

        # Walk the priority chain. Each layer proposes an answer; if
        # _apply_answer can't actually commit it (e.g. answers.json
        # gave "Yes" for a select with 8 work-auth options, or the
        # personal-info match gave "WA" for a yes/no radio), we keep
        # going instead of abandoning the field. Without this, weak
        # priority-3 matches silently win and the form sits with a
        # required field unfilled — Indeed then dead-locks Continue
        # with a misleading "Select an option" validation error.
        priorities = [
            ("personal_info", lambda: self._match_personal_info(label_lower)),
            ("contextual", lambda: self._match_contextual(label_lower, field)),
            ("answers.json", lambda: self._match_answers(field.label)),
        ]

        for source, get_answer in priorities:
            try:
                candidate = get_answer()
            except Exception as exc:
                logger.debug("  %s lookup failed: %s", source, exc)
                continue
            if not candidate:
                continue
            if not self._answer_fits_field(field, candidate):
                logger.debug(
                    "  %s gave %r but it doesn't fit %s field — skipping",
                    source, candidate, field.field_type,
                )
                continue
            logger.debug("  matched %s → %r", source, candidate)
            ok = await self._apply_answer(page, field, candidate)
            logger.debug(
                "  ← apply_answer returned %s for %r (via %s)",
                ok, field.label, source,
            )
            if ok:
                return True
            # Apply failed — continue down the chain instead of giving up.

        # Priority 4: LLM generation
        logger.debug("  no deterministic match, calling LLM...")
        llm_answer = await self._generate_answer(field)
        if llm_answer:
            self.used_llm = True
            logger.debug("  LLM returned → %r", llm_answer)
            ok = await self._apply_answer(page, field, llm_answer)
            logger.debug(
                "  ← apply_answer returned %s for %r (via LLM)",
                ok, field.label,
            )
            if ok:
                return True

        # Priority 4.5: Neutral fallback for free-text fields only.
        # When Ollama exhausts and returns empty on a required textarea,
        # the whole form dead-locks because required-field validation
        # blocks Continue. A neutral "I'd be happy to discuss..." answer
        # keeps the form moving without fabricating factual claims.
        #
        # DELIBERATELY LIMITED to textarea / long-form text fields —
        # never fabricates for yes/no, number, select, radio, checkbox
        # where a made-up answer could be actively wrong on the app.
        fallback = self._neutral_fallback(field)
        if fallback:
            logger.info(
                "  → LLM empty, using neutral fallback for '%s'",
                field.label[:60],
            )
            self._record_gap(field, job_id)
            ok = await self._apply_answer(page, field, fallback)
            logger.debug(
                "  ← apply_answer returned %s for %r (via neutral fallback)",
                ok, field.label,
            )
            if ok:
                return True

        # Priority 5: Record as gap
        logger.debug("  → NO ANSWER FOUND, recording as skill gap")
        self._record_gap(field, job_id)
        self._record_unanswered(field.label)
        return False

    # ------------------------------------------------------------------
    # Priority 1: Personal Info
    # ------------------------------------------------------------------

    def _match_personal_info(self, label_lower: str) -> str:
        """Match field label to personal info from config.

        Uses word-boundary matching so short keys like ``state`` only
        fire on the standalone word — not on substrings like ``United
        States``. Without this, every visa/sponsorship/work-auth
        radio inherited the user's state code (``WA``) and Indeed's
        validation rejected the form with a misleading "Select an
        option" error.
        """
        for keyword, config_key in PERSONAL_INFO_KEYS.items():
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, label_lower):
                value = self.personal_info.get(config_key, "")
                if value and config_key == "phone":
                    value = _normalize_phone_for_field(value)
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
        3. Fuzzy match (SequenceMatcher, 60% threshold) WITH a
           length-ratio guard: shorter string must be at least 50%
           the length of the longer one. Without the guard, short
           labels like "Years" fuzzy-matched "Years of experience"
           on raw character overlap and returned the wrong answer
           (different question, same first word).
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
            # Length-ratio guard: reject matches where one string is
            # less than half the length of the other. SequenceMatcher
            # gives 0.66 for "years" vs "years of experience" purely
            # from the shared prefix — clearly different questions.
            shorter = min(len(label_lower), len(q))
            longer = max(len(label_lower), len(q))
            if longer == 0 or shorter / longer < 0.5:
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
    # Neutral fallback for dead-locked required fields
    # ------------------------------------------------------------------

    # Phrases that suggest an open-ended question. When the label
    # matches ANY of these AND the field is text/textarea, we use
    # the neutral fallback instead of leaving the field empty.
    _OPEN_ENDED_PATTERNS = (
        "tell us about",
        "tell me about",
        "describe",
        "why do you want",
        "why are you",
        "what interests",
        "what makes you",
        "what attracted",
        "cover letter",  # already handled earlier, but safe to include
        "anything else",
        "additional information",
        "share your",
        "elaborate",
        "explain",
        "questions for us",
    )

    _NEUTRAL_OPEN_ENDED = (
        "I would be happy to discuss this in more detail during an "
        "interview."
    )

    def _neutral_fallback(self, field: FormField) -> str:
        """Return a safe placeholder for required fields when LLM ran dry.

        Two layers:

        1. **Pattern-based smart fallback** (common required questions
           the LLM can fail on but for which we can synthesize a
           reasonable answer from resume + JD + personal info):
           salary, notice period, years of experience, how-you-heard,
           willingness to relocate, work-auth confirmation, etc.

        2. **Generic open-ended placeholder** ("I'd be happy to
           discuss...") for textarea / open-ended text inputs.

        Returns empty string for number / select / radio / checkbox /
        date — fabricating those could disqualify the candidate at
        pre-screen (e.g. inventing a years-of-experience number).
        """
        smart = self._smart_fallback(field)
        if smart:
            return smart

        if field.field_type not in ("text", "textarea"):
            return ""

        label_lower = field.label.lower()

        # Textareas are almost always free-text open-ended by UI
        # convention. Safe to fallback.
        if field.field_type == "textarea":
            return self._NEUTRAL_OPEN_ENDED

        # Text inputs — only fallback when the label LOOKS like an
        # open-ended question. Avoid text inputs that want a number,
        # date, yes/no, or specific factual answer.
        for pattern in self._OPEN_ENDED_PATTERNS:
            if pattern in label_lower:
                return self._NEUTRAL_OPEN_ENDED

        return ""

    # Pattern -> handler. Ordering matters: most specific first.
    _SMART_FALLBACK_PATTERNS = (
        ("salary", "_smart_salary"),
        ("hourly rate", "_smart_salary"),
        ("compensation", "_smart_salary"),
        ("desired pay", "_smart_salary"),
        ("expected pay", "_smart_salary"),
        ("notice period", "_smart_notice_period"),
        ("how soon can you start", "_smart_start_date_text"),
        ("when can you start", "_smart_start_date_text"),
        ("how did you hear", "_smart_referral_source"),
        ("where did you hear", "_smart_referral_source"),
        ("willing to relocate", "_smart_relocation"),
        ("able to relocate", "_smart_relocation"),
        ("years of experience", "_smart_years_experience"),
        ("years experience", "_smart_years_experience"),
    )

    def _smart_fallback(self, field: FormField) -> str:
        """Compute a sensible answer for known required-question patterns.

        Triggered when LLM returns empty AND the question matches a
        known shape we can answer from local context (resume text,
        job description, personal info, source platform). All
        handlers must return either a string answer or "" — they are
        each individually free to bail when the available signal is
        too weak to commit to a number/date.
        """
        if field.field_type not in ("text", "textarea"):
            return ""
        label_lower = field.label.lower()
        for pattern, handler_name in self._SMART_FALLBACK_PATTERNS:
            if pattern in label_lower:
                handler = getattr(self, handler_name, None)
                if handler is None:
                    continue
                try:
                    out = handler(field)
                except Exception as exc:
                    logger.debug(
                        "smart fallback %s failed: %s", handler_name, exc,
                    )
                    out = ""
                if out:
                    logger.info(
                        "  smart fallback (%s) → %r for '%s'",
                        handler_name, out[:60], field.label[:60],
                    )
                    return out
        return ""

    # --- smart fallback handlers ---

    def _smart_salary(self, field: FormField) -> str:
        """Salary range derived from resume seniority + JD (if mentioned).

        Strategy:
        1. Scan JD for an explicit salary range; if found, restate it.
        2. Otherwise, infer level from resume keywords and pick a
           reasonable USD annual range. Never invents a single number
           — always a range, with "open to discussion".
        """
        jd_range = self._extract_jd_salary_range()
        if jd_range:
            return f"My target is in line with the posted range: {jd_range}."

        resume = (self.resume_text or "").lower()
        seniority = "mid"
        if any(k in resume for k in ("principal", "staff engineer", "director")):
            seniority = "principal"
        elif any(k in resume for k in ("senior", "lead", "sr.", "sr ")):
            seniority = "senior"
        elif any(k in resume for k in ("junior", "entry-level", "graduate")):
            seniority = "junior"

        # USD annual ranges — kept conservative on purpose. Users
        # tune via answers.json once they see this come up; the goal
        # here is "form unblocks" not "this number is exactly right".
        ranges = {
            "junior":   "$60,000 - $80,000",
            "mid":      "$90,000 - $115,000",
            "senior":   "$130,000 - $160,000",
            "principal": "$170,000 - $210,000",
        }
        return (
            f"My target range is {ranges[seniority]}, but I'm flexible "
            f"based on the full compensation package."
        )

    def _extract_jd_salary_range(self) -> str:
        """Find an explicit USD range in the JD if one was posted."""
        jd = self.job_description or ""
        # $XXX,XXX - $XXX,XXX or $XXk - $XXk
        m = re.search(
            r"\$\s?\d{2,3}(?:,\d{3})?(?:k)?\s*[-–to]+\s*\$\s?\d{2,3}(?:,\d{3})?(?:k)?",
            jd, re.IGNORECASE,
        )
        return m.group(0).strip() if m else ""

    def _smart_notice_period(self, field: FormField) -> str:
        return "Two weeks"

    def _smart_start_date_text(self, field: FormField) -> str:
        return "Two weeks from offer acceptance"

    def _smart_referral_source(self, field: FormField) -> str:
        # If the platform adapter set source_platform on us, use that.
        source = (self.personal_info.get("source", "") or "").strip()
        if source:
            return source
        # Otherwise fall back to a neutral plausible answer.
        return "Online job board"

    def _smart_relocation(self, field: FormField) -> str:
        # Default to "Yes" — most candidates filtering for these jobs
        # are open. Users who aren't can put "No" in answers.json.
        return "Yes"

    def _smart_years_experience(self, field: FormField) -> str:
        """Estimate years from resume work-history dates if available."""
        text = self.resume_text or ""
        years = re.findall(r"(20\d{2}|19\d{2})", text)
        if not years:
            return ""
        try:
            ints = sorted({int(y) for y in years})
            from datetime import date
            this_year = date.today().year
            # Conservative: span between earliest year and now,
            # capped so a one-line "graduated 2010" doesn't read as
            # 16 YoE for someone who took a long career break.
            span = max(0, min(this_year - ints[0], 25))
            if span <= 0:
                return ""
            return str(span)
        except Exception:
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
                # Indeed-style detection returns ONE FormField per radio
                # group (pointing at the first radio in the group), so
                # blindly checking field.element always picks the first
                # option regardless of the LLM's answer. Resolve the
                # right sibling in the same group by matching the LLM's
                # answer text against each radio's label, then click
                # that one.
                target = await self._resolve_radio_target(field, answer)
                if target is None:
                    # No confident match. The OLD behavior was to fall
                    # back to field.element (the first radio in the
                    # group) — which on Dice's work-auth question
                    # silently committed "Prefer Not to Answer" for
                    # users whose actual status was "US Citizen".
                    # Better to return False here so the priority
                    # chain falls through to the LLM with the radio's
                    # actual options visible. Caller will retry.
                    logger.debug(
                        "  → radio resolver couldn't match %r; "
                        "deferring to next priority",
                        answer,
                    )
                    return False
                try:
                    await target.check(timeout=4000)
                except Exception as exc:
                    logger.debug(
                        "Normal check() failed (%s), retrying with force=True",
                        exc,
                    )
                    await target.check(force=True, timeout=2000)
                self.fields_filled += 1
                return True

            elif field.field_type == "checkbox":
                if answer.lower() in ("yes", "true", "1"):
                    try:
                        await field.element.check(timeout=4000)
                    except Exception as exc:
                        logger.debug(
                            "Normal check() failed (%s), retrying with force=True",
                            exc,
                        )
                        await field.element.check(force=True, timeout=2000)
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
    # React State Commit
    # ------------------------------------------------------------------

    @staticmethod
    async def commit_react_state(page: Page, settle_seconds: float = 0.8) -> None:
        """Force React's controlled-component state to commit our fills.

        A plain Playwright `.check()` / `.fill()` updates the DOM but
        React intercepts the native property setters; without going
        through them, React's internal state stays "uncommitted" and
        the host site's Continue button keeps seeing required fields
        as empty. The well-known workaround below explicitly calls
        HTMLInputElement / HTMLTextAreaElement / HTMLSelectElement
        prototype setters with the element's *current* value, then
        dispatches input/change/blur so React's synthetic-event system
        picks up the change.

        Platform-agnostic — Indeed, Dice, ZipRecruiter, and any future
        React-based job-board form should call this after their fill
        loop finishes and BEFORE clicking Continue. Cheap on non-React
        pages (the dispatched events are no-ops there).

        ``settle_seconds`` gives React time to re-render Continue from
        disabled to enabled before the walker clicks it.
        """
        try:
            await page.evaluate("""() => {
                const inputProto = window.HTMLInputElement.prototype;
                const checkedSetter = Object.getOwnPropertyDescriptor(
                    inputProto, 'checked'
                )?.set;
                const valueSetter = Object.getOwnPropertyDescriptor(
                    inputProto, 'value'
                )?.set;
                const textareaValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                const selectValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value'
                )?.set;
                const els = [...document.querySelectorAll(
                    'input:not([type=hidden]), textarea, select'
                )].filter(el => el.offsetParent !== null);
                for (const el of els) {
                    try {
                        if (el.tagName === 'INPUT') {
                            if (el.type === 'radio' || el.type === 'checkbox') {
                                if (checkedSetter) {
                                    checkedSetter.call(el, el.checked);
                                }
                            } else {
                                if (valueSetter) {
                                    valueSetter.call(el, el.value);
                                }
                            }
                        } else if (el.tagName === 'TEXTAREA') {
                            if (textareaValueSetter) {
                                textareaValueSetter.call(el, el.value);
                            }
                        } else if (el.tagName === 'SELECT') {
                            if (selectValueSetter) {
                                selectValueSetter.call(el, el.value);
                            }
                        }
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    } catch (_) {}
                }
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
            }""")
            if settle_seconds > 0:
                import asyncio
                await asyncio.sleep(settle_seconds)
        except Exception as exc:
            logger.debug("commit_react_state failed: %s", exc)

    # ------------------------------------------------------------------
    # Answer-Fits-Field Pre-Check
    # ------------------------------------------------------------------

    @staticmethod
    def _answer_fits_field(field: FormField, answer: str) -> bool:
        """Cheap pre-flight: does this candidate answer plausibly fit?

        Filters out the most damaging cross-type contamination before
        we even try to commit it:

        - Select with explicit options: reject answers that aren't
          fuzzy-similar to any option (catches answers.json giving
          "Yes" for a work-auth select with 8 specific statuses).
        - Number field: reject answers with no digits.
        - Date field: reject anything that isn't ISO-style or a date
          phrase the date-coercer can handle.

        Radio/checkbox fall through unchanged — the radio resolver
        does its own match-or-fallback on commit. Returning True here
        does NOT guarantee apply_answer will succeed; it just lets the
        chain skip obviously bad candidates without spending a click.
        """
        if not answer:
            return False
        ans = answer.strip()
        if not ans:
            return False

        if field.field_type == "select" and field.options:
            ans_lower = ans.lower()
            for opt in field.options:
                opt_lower = opt.lower()
                if ans_lower == opt_lower:
                    return True
                if ans_lower in opt_lower or opt_lower in ans_lower:
                    return True
            # Fuzzy fallback so "Yes" doesn't pass against
            # ["US Citizen", "Green Card Holder", ...].
            matches = difflib.get_close_matches(
                ans_lower, [o.lower() for o in field.options],
                n=1, cutoff=0.7,
            )
            return bool(matches)

        if field.field_type == "number":
            return any(c.isdigit() for c in ans)

        return True

    # ------------------------------------------------------------------
    # Radio Group Resolution
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolve_radio_target(field, answer: str):
        """Pick the right radio in a group by matching the LLM's answer.

        Indeed's questions handler returns one FormField per radio
        *group*, with ``field.element`` pointing at the first radio.
        Without resolving, we always click the first option (typically
        "Yes") regardless of what the LLM said — that's how a "willing
        to relocate" question gets a misleading "Yes" while a "do you
        have a felony conviction" gets the same misleading "Yes".

        Returns the matched radio element handle, or ``None`` if no
        confident match exists. Caller falls back to ``field.element``.
        """
        if not answer:
            return None
        try:
            name = await field.element.get_attribute("name")
        except Exception:
            return None
        if not name:
            return None

        answer_lower = (answer or "").strip().lower()
        # Work-authorization statuses that imply YES (legally able to
        # work without sponsorship). When personal_info hands us
        # "US Citizen" but the radio's options are bare Yes/No, the
        # status string scores 0 against any option and we fall back
        # to clicking the first radio — historically "Prefer Not to
        # Answer" on Dice's work-auth question. Treat these as
        # affirmative so a US citizen reliably gets "Yes" picked.
        AUTH_AFFIRMATIVE = {
            "us citizen", "u.s. citizen", "citizen",
            "green card holder", "green card", "permanent resident",
            "permanent resident card", "lpr",
            "authorized", "yes, i am authorized",
        }
        # Statuses that imply NO (need sponsorship / not authorized).
        AUTH_NEGATIVE = {
            "need sponsorship", "require sponsorship",
            "require visa sponsorship", "require a visa",
            "i need a visa", "not authorized",
        }
        affirmative = (
            answer_lower in {"yes", "true", "1", "y", "affirmative"}
            or answer_lower in AUTH_AFFIRMATIVE
        )
        negative = (
            answer_lower in {"no", "false", "0", "n", "negative"}
            or answer_lower in AUTH_NEGATIVE
        )

        # One JS call to enumerate every radio in this group with its
        # visible label text. Returning an array of {label} keeps the
        # element handle juggling out of Python and avoids the
        # evaluate_handle/get_properties dance that's fragile across
        # patchright/playwright versions.
        try:
            options = await field.element.evaluate(
                """(el) => {
                    const name = el.getAttribute('name');
                    if (!name) return [];
                    const radios = [...el.ownerDocument.querySelectorAll(
                        'input[type=radio][name="' + name.replace(/"/g, '\\\\"') + '"]'
                    )];
                    return radios.map((r, i) => {
                        let label = '';
                        if (r.id) {
                            const lbl = r.ownerDocument.querySelector(
                                'label[for="' + r.id + '"]'
                            );
                            if (lbl) label = lbl.innerText || '';
                        }
                        if (!label) {
                            const lbl = r.closest('label');
                            if (lbl) label = lbl.innerText || '';
                        }
                        if (!label) {
                            const lbl = r.parentElement?.querySelector('label, span');
                            if (lbl) label = lbl.innerText || '';
                        }
                        if (!label) label = r.value || '';
                        return { idx: i, label: label.trim(), value: r.value || '' };
                    });
                }"""
            )
        except Exception as exc:
            logger.debug("radio resolver: enumerate failed: %s", exc)
            return None

        if not options:
            return None

        best_idx = -1
        best_score = 0.0
        for opt in options:
            text = (opt.get("label") or opt.get("value") or "").strip().lower()
            if not text:
                continue
            if text == answer_lower:
                score = 1.0
            elif answer_lower and answer_lower in text:
                score = 0.85
            elif text in answer_lower:
                score = 0.75
            elif affirmative and text in {"yes", "y", "true"}:
                score = 0.95
            elif negative and text in {"no", "n", "false"}:
                score = 0.95
            else:
                score = (
                    difflib.SequenceMatcher(None, answer_lower, text).ratio()
                    * 0.6
                )
            if score > best_score:
                best_score = score
                best_idx = opt["idx"]

        if best_idx < 0 or best_score < 0.5:
            logger.debug(
                "radio resolver: no match for answer=%r among %s",
                answer, [o.get("label", "")[:30] for o in options],
            )
            return None

        # Re-grab the matching radio as an ElementHandle.
        try:
            target = await field.element.evaluate_handle(
                """(el, idx) => {
                    const name = el.getAttribute('name');
                    const radios = [...el.ownerDocument.querySelectorAll(
                        'input[type=radio][name="' + name.replace(/"/g, '\\\\"') + '"]'
                    )];
                    return radios[idx] || null;
                }""",
                best_idx,
            )
            element = target.as_element()
            if element is None:
                return None
            chosen_label = options[best_idx].get("label", "")
            logger.debug(
                "radio resolver: matched answer=%r → option=%r (score=%.2f)",
                answer, chosen_label[:50], best_score,
            )
            return element
        except Exception as exc:
            logger.debug("radio resolver: re-grab failed: %s", exc)
            return None

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
        """Record a new unanswered question for future wizard display.

        Defensive loader: unanswered.json has occasionally been
        written in the wrong shape (plain dict instead of a list of
        entry dicts). Any iteration that assumes the list shape
        crashes the platform's apply flow with 'str has no
        attribute get'. Normalize whatever we find and keep going.
        """
        raw = None
        if UNANSWERED_FILE.exists():
            try:
                with open(UNANSWERED_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                raw = None

        # Normalize to list[{question, encountered}]
        unanswered: list[dict] = []
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and entry.get("question"):
                    unanswered.append({
                        "question": str(entry.get("question", "")),
                        "encountered": int(entry.get("encountered", 1) or 1),
                    })
        elif isinstance(raw, dict):
            # Someone wrote it as a {question: count} dict — convert.
            for q, count in raw.items():
                if isinstance(q, str) and q:
                    try:
                        n = int(count) if count else 1
                    except (TypeError, ValueError):
                        n = 1
                    unanswered.append({"question": q, "encountered": n})

        existing_lower = {u["question"].lower() for u in unanswered}
        if question.lower() not in existing_lower:
            unanswered.append({"question": question, "encountered": 1})
        else:
            for u in unanswered:
                if u["question"].lower() == question.lower():
                    u["encountered"] = u.get("encountered", 0) + 1
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
