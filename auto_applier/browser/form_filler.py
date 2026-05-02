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
    # Past-tense / variant phrasings — Indeed and ZR ask "Where are
    # you located?" and "Current location" which our word-boundary
    # match on bare "location" missed. ZR live run 2026-05-02 hit
    # this and fell through to fuzzy answers.json which returned
    # "Yes" — silently invalid for a city/zip text field.
    "located": "city_state",
    "where are you located": "city_state",
    "current location": "city_state",
    "where do you live": "city_state",
    "where are you based": "city_state",
    "based": "city_state",
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
    # Salary / compensation — long compound keys first so they beat
    # plain "salary". Without these, an LLM call composed prose like
    # "I am seeking a salary in the range of $100k-$120k" into a
    # text input that wanted a single number, and the form rejected
    # it on validation. Map them all to desired_salary.
    "desired hourly rate": "desired_salary",
    "hourly rate of pay": "desired_salary",
    "rate of pay": "desired_salary",
    "hourly rate": "desired_salary",
    "expected salary": "desired_salary",
    "desired salary": "desired_salary",
    "salary expectation": "desired_salary",
    "salary expectations": "desired_salary",
    "compensation expectation": "desired_salary",
    "annual salary": "desired_salary",
    "salary": "desired_salary",
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
        (or why it wasn't). On every return path we ALSO emit one
        INFO-level FIELD_RESULT line in a fixed format that the
        scripts/audit_qa.py parser can ingest without losing fields
        to mid-flow log gaps.
        """
        self.fields_total += 1
        label_lower = field.label.lower()

        # Mutable closure over result state so the FIELD_RESULT line
        # can be emitted from any of the (many) early-return paths
        # without a try/finally that would also catch genuine errors.
        result: dict[str, object] = {
            "applied": False,
            "source": "",
            "answer": "",
        }

        def _log_result() -> None:
            try:
                logger.info(
                    "FIELD_RESULT label=%r type=%s applied=%s "
                    "source=%s answer=%r",
                    field.label, field.field_type,
                    bool(result["applied"]),
                    result["source"] or "none",
                    result["answer"],
                )
            except Exception:
                pass

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
            result["source"] = "honeypot"
            _log_result()
            return False
        try:
            is_visible = await field.element.is_visible()
        except Exception:
            is_visible = True  # assume visible if the check itself fails
        if not is_visible:
            logger.debug("  → hidden field, skipping (likely honeypot)")
            result["source"] = "hidden"
            _log_result()
            return False

        # Pre-fill check — if the site already populated this field
        # from the signed-in user's profile, leave it alone. Re-typing
        # a value that's already there is how we broke Indeed's phone
        # input and zip validation in the last three runs.
        if await self._field_already_has_value(field):
            logger.debug("  → already pre-filled, skipping")
            result["applied"] = True
            result["source"] = "pre-filled"
            _log_result()
            return True

        # File upload fields are handled by the platform adapter
        if field.field_type == "file" and any(
            kw in label_lower for kw in RESUME_UPLOAD_KEYWORDS
        ):
            logger.debug("  → file upload, deferring to platform")
            result["source"] = "platform-upload"
            _log_result()
            return False

        # Cover letter fields get special treatment
        if any(kw in label_lower for kw in COVER_LETTER_KEYWORDS):
            logger.debug("  → cover letter field, generating")
            ok = await self._fill_cover_letter(page, field)
            logger.debug("  ← cover letter fill result: %s", ok)
            result["applied"] = ok
            result["source"] = "cover_letter"
            result["answer"] = "(generated cover letter)" if ok else ""
            _log_result()
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
                result["applied"] = True
                result["source"] = source
                result["answer"] = candidate
                _log_result()
                return True
            # Apply failed — dump the element's outerHTML so the next
            # diagnosis cycle can see WHY (e.g. detected as text but
            # the underlying element is a hidden checkbox sibling).
            await self._dump_failed_element(field, candidate, source)
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
                result["applied"] = True
                result["source"] = "LLM"
                result["answer"] = llm_answer
                _log_result()
                return True
            await self._dump_failed_element(field, llm_answer, "LLM")

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
                result["applied"] = True
                result["source"] = "neutral-fallback"
                result["answer"] = fallback
                _log_result()
                return True

        # Priority 5: Record as gap
        logger.debug("  → NO ANSWER FOUND, recording as skill gap")
        self._record_gap(field, job_id)
        self._record_unanswered(field.label)
        result["source"] = "GAP"
        result["answer"] = ""
        _log_result()
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
        # Work-authorization questions: skip the geographic personal_info
        # keys (country / state / city / region / location). Live run
        # 2026-05-02 hit "Are you legally authorized to work IN THE
        # COUNTRY in which the job is located?" and the substring match
        # on "country" returned "United States" → tried to apply that
        # to a Yes/No radio → failed → form stuck. Work-auth questions
        # are yes/no by intent; the country reference is just framing.
        # Force fall-through to YES_NO_RULES / answers.json which have
        # the real "Yes" answer for a US-citizen profile.
        is_work_auth_question = any(
            kw in label_lower for kw in (
                "authorized to work",
                "legally authorized",
                "legal to work",
                "eligible to work",
                "permitted to work",
                "right to work",
                "sponsorship",
                "visa",
                "work permit",
                "work auth",
            )
        )
        for keyword, config_key in PERSONAL_INFO_KEYS.items():
            if is_work_auth_question and config_key in (
                "country", "state", "city", "city_state",
            ):
                continue
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

        # Conditional "If not [X], do you [Y]?" questions. Live run
        # 2026-05-02 hit "If not a US Citizen, do you have a non-
        # academic visa that permits you to work in the US?" which
        # got the wrong answer because the LLM defaulted to a
        # conservative "No" without grasping the conditional. For
        # a US-citizen profile, the correct answer is "No" — they
        # don't have a visa BECAUSE they don't need one. Generic
        # rule: if the question's "if not [X]" matches a fact the
        # candidate IS, answer "No" (the conditional doesn't apply
        # to them).
        conditional = self._answer_conditional_not_applicable(
            label_lower, field,
        )
        if conditional:
            return conditional

        return ""

    def _answer_conditional_not_applicable(
        self, label_lower: str, field: FormField,
    ) -> str:
        """Match "If not [trait], do you [Y]?" → "No" when candidate
        is [trait].

        Only fires on yes/no-shaped fields (radio, checkbox) or short
        select fields where Yes/No is a likely option. Returns "" if
        the conditional clause doesn't appear, or if we can't tell
        whether the candidate satisfies the trait.
        """
        if field.field_type not in ("radio", "checkbox", "select"):
            return ""
        # Need to find an "if not X, ..." or "if you are not X, ..."
        # clause to inspect.
        m = re.search(
            r"if (?:you are )?not (?:a |an )?"
            r"(us citizen|u\.s\. citizen|citizen|permanent resident|"
            r"green card holder|green card|authorized to work)"
            r"[,;:.]?",
            label_lower,
        )
        if not m:
            return ""
        trait = m.group(1)
        # Map the matched trait to the personal_info attribute that
        # confirms it. work_auth: "US Citizen" / "Permanent Resident" /
        # "Green Card" all imply work-authorized; bare "citizen" =
        # citizen-shaped status.
        work_auth = (self.personal_info.get("work_auth") or "").lower()
        if not work_auth:
            return ""
        candidate_is_trait = False
        if "citizen" in trait:
            candidate_is_trait = "citizen" in work_auth
        elif "permanent resident" in trait or "green card" in trait:
            candidate_is_trait = (
                "permanent resident" in work_auth
                or "green card" in work_auth
                or "lpr" in work_auth
            )
        elif "authorized to work" in trait:
            candidate_is_trait = (
                "citizen" in work_auth
                or "permanent resident" in work_auth
                or "authorized" in work_auth
                or "green card" in work_auth
            )
        if candidate_is_trait:
            logger.debug(
                "Conditional N/A match: candidate IS %r → No "
                "(question: %s)", trait, label_lower[:80],
            )
            return "No"
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

        # For free-text fields, detect any maxlength / character cap
        # so we don't produce text the form will silently reject. Live
        # run 2026-05-02 generated multi-paragraph cover-letter-style
        # text into a 250-char "Why are you interested?" textarea;
        # the form truncated mid-word and Continue stayed disabled.
        char_cap = 0
        if field.field_type in ("text", "textarea"):
            char_cap = await self._read_char_cap(field)
            if char_cap > 0:
                prompt_text += (
                    f"\n\nHARD LIMIT: This answer must be no more than "
                    f"{char_cap} characters total — including spaces. "
                    f"Be concise. Prefer 1-2 short sentences over a "
                    f"paragraph."
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
            # Post-trim if the LLM ignored the cap. Sentence-aware
            # trim — find the last sentence boundary that still fits,
            # otherwise hard-truncate. A rejected mid-word answer is
            # worse than a clean 1-sentence one.
            if char_cap > 0 and len(answer) > char_cap:
                trimmed = self._sentence_aware_trim(answer, char_cap)
                logger.info(
                    "LLM answer for '%s' overshot cap (%d > %d) — "
                    "trimmed to %d chars",
                    field.label[:60], len(answer), char_cap,
                    len(trimmed),
                )
                answer = trimmed
            logger.debug("LLM generated answer for '%s': %.50s...", field.label, answer)
            return answer
        except Exception as exc:
            logger.warning("LLM answer generation failed for '%s': %s", field.label, exc)
            return ""

    async def _read_char_cap(self, field: FormField) -> int:
        """Discover the field's character cap via maxlength or visible
        counter text. Returns 0 if no cap is found.

        Two sources:
          1. ``maxlength`` attribute on the input/textarea
          2. Visible "X / N characters" counter near the field
             (matches "X / N", "X of N", "Max N characters",
             "(N max)", etc.)
        """
        try:
            ml = await field.element.get_attribute("maxlength")
        except Exception:
            ml = None
        if ml:
            try:
                cap = int(ml)
                if cap > 0:
                    return cap
            except ValueError:
                pass
        # Walk up to find a visible counter sibling.
        try:
            counter_text = (await field.element.evaluate(
                """(el) => {
                    let p = el.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const t = (p.innerText || '').trim();
                        if (t && t.length < 800) return t;
                        p = p.parentElement;
                    }
                    return '';
                }"""
            )) or ""
        except Exception:
            counter_text = ""
        if not counter_text:
            return 0
        for pattern in (
            r"(?:max(?:imum)?[:\s]*)(\d{2,5})\s*(?:character|char)",
            r"(\d{1,5})\s*(?:character|char)\s*(?:limit|max)",
            r"\(\s*max\s*(\d{2,5})\s*\)",
            r"\b(\d{1,5})\s*characters?\s*max\b",
            r"(?:\b0\s*/\s*)(\d{2,5})\b",
            r"(?:\bof\s+)(\d{2,5})\s*characters?",
        ):
            m = re.search(pattern, counter_text, re.IGNORECASE)
            if m:
                try:
                    cap = int(m.group(1))
                    if 20 <= cap <= 50000:  # sanity range
                        return cap
                except ValueError:
                    continue
        return 0

    @staticmethod
    def _sentence_aware_trim(text: str, cap: int) -> str:
        """Trim ``text`` to fit within ``cap`` chars at a sentence
        boundary. Falls back to a hard char-truncate with an ellipsis
        when no sentence ending fits."""
        if len(text) <= cap:
            return text
        # Find the last sentence-ending punctuation within the cap.
        chunk = text[:cap]
        for end in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
            idx = chunk.rfind(end)
            if idx > 0:
                # Keep the punctuation, drop the trailing space/newline.
                return chunk[: idx + 1].rstrip()
        # No sentence boundary — break at the last word and add ellipsis.
        space_idx = chunk.rfind(" ")
        if space_idx > 0 and (cap - space_idx) < 20:
            return chunk[:space_idx].rstrip()
        return chunk[: cap - 1].rstrip()

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
        """Generate and fill a cover letter field.

        Branches on field type — textareas/text get the LLM-generated
        text directly; FILE inputs get a PDF rendered from that text
        via the same render pipeline used by `cli cover`. The previous
        version only handled textareas, so on Dice (which uses a file
        input for cover letter) the generated text dropped on the
        floor and the user's resume PDF ended up in the cover letter
        slot via the platform's blind upload path.
        """
        letter = await self.cover_letter_writer.generate(
            resume_text=self.resume_text[:3000],
            job_description=self.job_description[:2000],
            company_name=self.company_name,
            job_title=self.job_title,
        )
        if not letter:
            return False
        self.cover_letter_generated = True

        if field.field_type == "file":
            # Render to PDF and set_input_files. PDF lives at
            # data/cover_letters/<job_id>/<First>_<Last>_Cover_Letter.pdf
            # — same path `cli cover` uses, so a manual cli run + a
            # live apply share the same artifact.
            try:
                pdf_path = await self._render_cover_letter_pdf(letter)
                if pdf_path is None:
                    return False
                await field.element.set_input_files(str(pdf_path))
                # Wait for the upload to actually settle before we let
                # the form-walker advance to the next step. Otherwise
                # "Continue" can fire before the file is attached and
                # the application submits without the cover letter.
                await self.wait_for_upload_complete(
                    page, expected_name=str(pdf_path), timeout=15.0,
                )
                self.fields_filled += 1
                await random_delay(0.5, 1.5)
                return True
            except Exception as exc:
                logger.warning(
                    "Cover letter file upload failed: %s", exc,
                )
                return False
        # Textarea / text field — fill the generated text directly.
        return await self._apply_answer(page, field, letter)

    async def _render_cover_letter_pdf(self, letter_text: str):
        """Render the LLM-generated letter to a PDF on disk.

        Reuses the cover_letter_service HTML template + tailor's
        Playwright PDF renderer. Returns the Path written to, or None
        on failure (Playwright not installed, render error, etc.).
        """
        try:
            from auto_applier.config import COVER_LETTERS_DIR, USER_CONFIG_FILE
            from auto_applier.resume.cover_letter_service import (
                _render_cover_letter_html,
            )
            from auto_applier.resume.tailor import (
                _user_filename_prefix, render_pdf,
            )
            import json as _json

            # Look up display name for the PDF body (greeting + sign-off).
            display_name = ""
            try:
                if USER_CONFIG_FILE.exists():
                    cfg = _json.loads(
                        USER_CONFIG_FILE.read_text(encoding="utf-8")
                    )
                    p = cfg.get("personal_info", {}) or {}
                    display_name = (p.get("name") or "").strip()
                    if not display_name:
                        first = (p.get("first_name") or "").strip()
                        last = (p.get("last_name") or "").strip()
                        display_name = " ".join(x for x in (first, last) if x)
            except Exception:
                display_name = ""

            html = _render_cover_letter_html(letter_text, display_name)

            # Path: data/cover_letters/<job_id>/<First>_<Last>_Cover_Letter.pdf
            # Use job_id from form_filler context if we have it.
            job_id = getattr(self, "job_id", "") or "current"
            safe_job = "".join(
                c if c.isalnum() or c in "-_." else "_" for c in job_id
            )
            prefix = _user_filename_prefix()
            filename = (
                f"{prefix}_Cover_Letter.pdf" if prefix else "Cover_Letter.pdf"
            )
            out_dir = COVER_LETTERS_DIR / safe_job
            out_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = out_dir / filename

            ok = await render_pdf(html, pdf_path)
            if ok:
                logger.info(
                    "Rendered cover letter PDF for upload: %s", pdf_path,
                )
                return pdf_path
            return None
        except Exception as exc:
            logger.warning("Cover letter PDF render failed: %s", exc)
            return None

    @staticmethod
    async def classify_file_input(element) -> str:
        """Classify a file input element by what it's asking for.

        Returns one of "resume", "cover_letter", "transcript",
        "portfolio", or "unknown". Conservative — if we can't tell,
        return "unknown" so callers can decide whether to upload
        anything.

        Inspection sources (in order):
          1. data-cy / data-testid / name / id attributes
          2. aria-label
          3. <label for=...> if id exists
          4. closest <label> wrap
          5. nearby preceding text (heading / sentence)

        Implemented as a single JS evaluate so we don't round-trip
        five times per input.
        """
        try:
            text = await element.evaluate(
                """(el) => {
                    const bits = [];
                    for (const a of ['name', 'id', 'data-cy',
                                      'data-testid', 'aria-label',
                                      'placeholder', 'accept']) {
                        const v = el.getAttribute(a);
                        if (v) bits.push(v);
                    }
                    if (el.id) {
                        const lbl = el.ownerDocument.querySelector(
                            'label[for="' + el.id.replace(/"/g, '\\\\"') + '"]'
                        );
                        if (lbl) bits.push(lbl.innerText || '');
                    }
                    const wrap = el.closest('label');
                    if (wrap) bits.push(wrap.innerText || '');
                    // Walk up 4 levels for nearby heading text.
                    let p = el.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const heading = p.querySelector(
                            ':scope > label, :scope > h1, :scope > h2,'
                            + ' :scope > h3, :scope > h4, :scope > h5,'
                            + ' :scope > h6, :scope > legend,'
                            + ' :scope > p, :scope > span, :scope > div'
                        );
                        if (heading && heading !== el) {
                            const t = (heading.innerText || '').trim();
                            if (t && t.length < 200) bits.push(t);
                        }
                        p = p.parentElement;
                    }
                    return bits.join(' | ').toLowerCase();
                }"""
            )
        except Exception:
            return "unknown"

        text = text or ""
        # Order matters — cover letter tested first because some sites
        # use "resume / cv / cover letter" in a single label and we
        # want the more specific match to win.
        if any(kw in text for kw in (
            "cover letter", "letter of interest", "cover note",
            "motivation letter", "coverletter",
        )):
            return "cover_letter"
        if any(kw in text for kw in (
            "transcript", "academic record",
        )):
            return "transcript"
        if any(kw in text for kw in (
            "portfolio", "work sample", "writing sample",
        )):
            return "portfolio"
        if any(kw in text for kw in (
            "resume", "cv", "curriculum vitae",
        )):
            return "resume"
        return "unknown"

    @staticmethod
    async def pick_resume_input(page, platform_name: str = "form"):
        """Find the file input on the current page that wants the resume.

        Shared by every platform's ``_handle_resume_upload`` so a fix
        to the picker logic lands in one place. Returns the element
        handle to upload to, or ``None`` if nothing safe was found.

        Selection rules, in order:
          1. First visible input classified as ``resume`` wins.
          2. If no visible ``resume`` input exists but a HIDDEN
             ``resume`` input does, upload there. Rationale: modern
             web apps (Indeed, Workday, Greenhouse, Lever) overlay
             a styled card / "Select file" button on top of a hidden
             ``<input type=file>``. ``set_input_files()`` works on
             hidden inputs just fine, and the live run on
             2026-05-02 dead-locked Indeed's resume-selection step
             three times because we required visible=True and
             therefore never uploaded anything.
          3. Else first visible ``unknown`` input wins, with a WARN.
          4. Else, if there is exactly one visible file input on
             the page (any classification — including
             ``cover_letter``), upload to it with a stronger WARN.
             Rationale: when the page only has one slot, nearby
             chrome text might be misleading.
          5. Else return ``None``.

        Always logs every classified input it saw at debug level so
        we can diagnose stuck-upload reports without re-running.
        """
        try:
            inputs = await page.query_selector_all("input[type='file']")
        except Exception:
            inputs = []
        if not inputs:
            return None

        # Classify every input and remember which were visible.
        seen: list[tuple[object, str, bool]] = []
        for inp in inputs:
            try:
                visible = await inp.is_visible()
            except Exception:
                visible = True
            try:
                kind = await FormFiller.classify_file_input(inp)
            except Exception:
                kind = "unknown"
            seen.append((inp, kind, visible))

        for i, (_, kind, visible) in enumerate(seen):
            logger.debug(
                "%s: file input[%d] kind=%s visible=%s",
                platform_name, i, kind, visible,
            )

        visible_inputs = [
            (inp, kind) for inp, kind, vis in seen if vis
        ]
        hidden_inputs = [
            (inp, kind) for inp, kind, vis in seen if not vis
        ]

        # Rule 1: visible resume-classified wins.
        for inp, kind in visible_inputs:
            if kind == "resume":
                return inp

        # Rule 2: hidden resume-classified — common on modern UIs
        # (Indeed, Workday, Greenhouse) where the file input is
        # behind a styled card. set_input_files() works on hidden
        # inputs.
        for inp, kind in hidden_inputs:
            if kind == "resume":
                logger.info(
                    "%s: only hidden file input matched 'resume' "
                    "— uploading anyway. Normal for sites that "
                    "overlay file inputs with custom UI (Indeed, "
                    "Workday, Greenhouse).",
                    platform_name,
                )
                return inp

        # Rule 3: visible unknown.
        for inp, kind in visible_inputs:
            if kind == "unknown":
                logger.warning(
                    "%s: no input classified as 'resume'; falling back "
                    "to first 'unknown' input — verify the right slot "
                    "got the resume.", platform_name,
                )
                return inp

        # Rule 4: single visible file input regardless of class.
        if len(visible_inputs) == 1:
            inp, kind = visible_inputs[0]
            logger.warning(
                "%s: only one visible file input on this page (kind="
                "%s). Uploading resume there to avoid dead-lock — if "
                "this looks wrong in the screenshot, the classifier "
                "needs another keyword.", platform_name, kind,
            )
            return inp

        # Rule 5: nothing safe — bail.
        all_kinds = [k for _, k, _ in seen]
        logger.warning(
            "%s: %d file input(s) on page (kinds: %s) but none safe "
            "to upload to. Skipping upload.",
            platform_name, len(seen), ", ".join(all_kinds),
        )
        return None

    @staticmethod
    async def wait_for_upload_complete(
        page, expected_name: str = "", timeout: float = 15.0,
    ) -> bool:
        """Block until an in-flight file upload visually finishes.

        ``set_input_files()`` resolves the moment the file is queued,
        not when the server has accepted it. On Dice and a handful of
        Indeed steps the form's "Continue" button stays disabled until
        the upload spinner clears, and clicking it early advances
        without the file attached. Earlier code papered over this with
        a fixed ``random_delay(1.0, 2.0)`` which is both too short for
        big PDFs and too long when the upload was instant.

        Heuristics tried in parallel — first to fire wins:

          1. The expected filename appears as text on the page (most
             sites render the chosen filename next to the input).
          2. All visible upload-spinner / progress / "uploading"
             indicators disappear.
          3. Hard timeout.

        Returns True if either of (1) or (2) fired before timeout,
        False on timeout (caller can still proceed — this is a
        best-effort polish, not a hard gate).
        """
        import asyncio
        import os

        # get_running_loop() is the 3.12+ replacement for the
        # deprecated get_event_loop() inside coroutines.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, timeout)
        stem = ""
        if expected_name:
            stem = os.path.splitext(os.path.basename(expected_name))[0]

        spinner_selectors = [
            "[class*='spinner']:not([style*='display: none'])",
            "[class*='Spinner']:not([style*='display: none'])",
            "[class*='loading']:not([style*='display: none'])",
            "[class*='Loading']:not([style*='display: none'])",
            "[class*='uploading']",
            "[class*='Uploading']",
            "[role='progressbar']",
            "progress",
            "[aria-busy='true']",
        ]

        # Quick poll loop. Cheap evaluations, ~200ms cadence.
        while loop.time() < deadline:
            # 1. Filename rendered on page?
            if stem and len(stem) >= 3:
                try:
                    found = await page.evaluate(
                        """(needle) => {
                            const t = (document.body.innerText || '');
                            return t.toLowerCase().includes(
                                needle.toLowerCase()
                            );
                        }""",
                        stem,
                    )
                    if found:
                        return True
                except Exception:
                    pass

            # 2. Any spinners still visible?
            spinner_visible = False
            for sel in spinner_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el is None:
                        continue
                    try:
                        if await el.is_visible():
                            spinner_visible = True
                            break
                    except Exception:
                        spinner_visible = True
                        break
                except Exception:
                    continue
            if not spinner_visible and stem:
                # No spinner AND we had a filename to look for but
                # didn't find it — give the page a moment longer.
                await asyncio.sleep(0.2)
                continue
            if not spinner_visible:
                # No spinner at all, no filename hint — assume done.
                return True

            await asyncio.sleep(0.2)

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
                # Capture the group name BEFORE the click — if the
                # element handle goes stale on .check(), we need this
                # to re-resolve the target from the live DOM.
                radio_group_name = ""
                try:
                    radio_group_name = (
                        await target.get_attribute("name")
                    ) or ""
                except Exception:
                    pass
                try:
                    await target.check(timeout=4000)
                except Exception as exc:
                    msg = str(exc).lower()
                    is_stale = (
                        "not attached" in msg
                        or "detached" in msg
                        or "no node found" in msg
                    )
                    if is_stale and radio_group_name:
                        # Indeed re-renders questions modules between
                        # detection and click; the original handle
                        # then points at a node that's no longer in
                        # the DOM. Re-find any radio in the same
                        # group, hand it to the resolver, and retry.
                        logger.debug(
                            "  → radio target detached, re-resolving "
                            "from live DOM (group=%s)",
                            radio_group_name,
                        )
                        try:
                            anchor = await page.query_selector(
                                "input[type=radio][name='"
                                + radio_group_name.replace("'", "\\'")
                                + "']"
                            )
                        except Exception:
                            anchor = None
                        if anchor is not None:
                            from dataclasses import replace
                            try:
                                fresh_field = replace(
                                    field, element=anchor
                                )
                            except Exception:
                                fresh_field = field
                            new_target = await self._resolve_radio_target(
                                fresh_field, answer,
                            )
                            if new_target is not None:
                                try:
                                    await new_target.check(timeout=4000)
                                    self.fields_filled += 1
                                    return True
                                except Exception as retry_exc:
                                    msg2 = str(retry_exc).lower()
                                    if (
                                        "not attached" in msg2
                                        or "detached" in msg2
                                    ):
                                        logger.warning(
                                            "Radio still detached "
                                            "after re-resolve for "
                                            "%r — giving up; "
                                            "field will be skipped",
                                            field.label[:60],
                                        )
                                        return False
                                    # Fall through to force=True path.
                                    exc = retry_exc
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

        # Location-shaped text fields: "Where are you located?", "City",
        # "Zip code", "Address" — these expect a place name or postal
        # code. ZR live run 2026-05-02 fuzzy-matched "Where are you
        # located?" against answers.json at 60% and got "Yes" — passed
        # the persistence check (non-empty value) but ZR rejected the
        # form silently. Reject obviously-bool answers ("Yes"/"No")
        # for these. Bare 1-3 letter answers also rejected (a real
        # location should be at least 4 chars: "NYC", "Boston", etc.).
        if field.field_type in ("text", "textarea"):
            label_lower = (field.label or "").lower()
            location_shape = any(
                kw in label_lower for kw in (
                    "where are you located",
                    "current location",
                    "where do you live",
                    "where are you based",
                    "city, state",
                    "city/state",
                    "city state",
                    "zip code",
                    "postal code",
                    "address",
                )
            )
            if location_shape:
                if ans.lower() in ("yes", "no", "y", "n", "true", "false"):
                    return False
                # Pure-bool tokens disguised as longer strings ("yes,
                # i live there") still slip — but a 1-3 char location
                # is meaningless either way.
                if len(ans) < 3:
                    return False

        if field.field_type in ("radio", "checkbox"):
            # Reject obvious cross-type contamination: a candidate
            # that's clearly a phone number, email, URL, address,
            # or postal code shouldn't be applied to a yes/no radio.
            # Real-world bug: the keyword "mobile" in a personal_info
            # PERSONAL_INFO_KEYS map matched against an apply form
            # asking "Do you consent to SMS at the mobile number..."
            # → returned the user's phone digits → resolver tried to
            # match "15550100" against "Yes"/"No". Doomed.
            stripped = ans
            for ch in (" ", "-", "+", "(", ")", "."):
                stripped = stripped.replace(ch, "")
            # Phone-shaped: 7+ digits and ONLY digits after stripping.
            if stripped.isdigit() and len(stripped) >= 7:
                return False
            # Email
            if "@" in ans and "." in ans:
                return False
            # URL
            if ans.startswith(("http://", "https://", "www.")):
                return False
            # Long free-text (anything > 60 chars is almost certainly
            # not a radio/checkbox label).
            if len(ans) > 60:
                return False

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

        # Substring keyword sets used to grade longer option labels
        # like "Yes, I am authorized to work in the United States" or
        # "No, I require sponsorship". A bucketed-affirmative answer
        # like "US Citizen" wouldn't otherwise score against them.
        AFFIRMATIVE_OPT_HINTS = (
            "yes", "authorized", "able to work", "can legally work",
            "i am authorized",
        )
        NEGATIVE_OPT_HINTS = (
            "no", "sponsorship", "require visa", "not authorized",
            "need a visa", "i need a visa",
        )

        # Word-boundary helper: "no" matches "no, i don't have one"
        # but NOT "Latino" (where "no" is mid-word). Bare-substring
        # matching produced real-world false positives where the LLM
        # said "No" to an ethnicity question and the resolver picked
        # "Hispanic or Latino" because "no" appears at the end of
        # "Latino". Likewise "No" was matching "I do not consent"
        # via "n-o-t". Word boundaries kill that whole class of bug.
        def _wb_in(needle: str, haystack: str) -> bool:
            if not needle:
                return False
            return bool(re.search(
                r"\b" + re.escape(needle) + r"\b", haystack,
            ))

        # Numeric-bucket pre-pass: when the answer is a bare integer
        # (e.g. "6" for years of experience) AND the options look like
        # numeric ranges ("1-3 years", "4-6 years", "7+", "10+"),
        # find the bucket that CONTAINS the number and short-circuit
        # the rest of the resolver. Without this the answer "6" scores
        # 0 against all options, the resolver returns None, and the
        # form dead-locks on the questions step. Live run 2026-05-02
        # hit this at "How many years of experience do you have in
        # data science...".
        try:
            numeric_answer = int(answer_lower.strip())
        except (ValueError, AttributeError):
            numeric_answer = None
        if numeric_answer is not None:
            # Parse each option for a range / N+ / single-N pattern.
            range_pattern = re.compile(
                r"(\d+)\s*(?:-|–|—|to|\s)\s*(\d+)"
            )
            plus_pattern = re.compile(r"(\d+)\s*\+")
            less_than_pattern = re.compile(
                r"(?:less than|<|under)\s*(\d+)", re.IGNORECASE
            )
            bucket_idx = -1
            for opt in options:
                text = (
                    opt.get("label") or opt.get("value") or ""
                ).strip().lower()
                if not text:
                    continue
                m_range = range_pattern.search(text)
                if m_range:
                    lo, hi = int(m_range.group(1)), int(m_range.group(2))
                    if lo <= numeric_answer <= hi:
                        bucket_idx = opt["idx"]
                        break
                    continue
                m_plus = plus_pattern.search(text)
                if m_plus:
                    threshold = int(m_plus.group(1))
                    if numeric_answer >= threshold:
                        bucket_idx = opt["idx"]
                        # Keep scanning in case a tighter range matches
                        # before the open-ended bucket. We only commit
                        # to the plus-bucket if no exact range hit.
                    continue
                m_lt = less_than_pattern.search(text)
                if m_lt:
                    threshold = int(m_lt.group(1))
                    if numeric_answer < threshold:
                        bucket_idx = opt["idx"]
                    continue
            if bucket_idx >= 0:
                logger.info(
                    "radio resolver: numeric-bucket match for "
                    "answer=%r → option[%d]=%r",
                    answer, bucket_idx,
                    options[bucket_idx].get("label", "")[:60],
                )
                # Skip directly to re-grab logic below by faking
                # best_idx/score.
                best_idx = bucket_idx
                best_score = 1.0
                # Fall through to the re-grab block at the bottom.
                # (Other scoring loops below will still execute but
                # never beat score=1.0.)
            else:
                best_idx = -1
                best_score = 0.0
        else:
            best_idx = -1
            best_score = 0.0

        for opt in options:
            text = (opt.get("label") or opt.get("value") or "").strip().lower()
            if not text:
                continue
            if text == answer_lower:
                score = 1.0
            elif answer_lower and _wb_in(answer_lower, text):
                score = 0.85
            elif _wb_in(text, answer_lower):
                score = 0.75
            elif affirmative and text in {"yes", "y", "true"}:
                score = 0.95
            elif negative and text in {"no", "n", "false"}:
                score = 0.95
            elif affirmative and any(_wb_in(h, text) for h in AFFIRMATIVE_OPT_HINTS):
                score = 0.9
            elif negative and any(_wb_in(h, text) for h in NEGATIVE_OPT_HINTS):
                score = 0.9
            else:
                score = (
                    difflib.SequenceMatcher(None, answer_lower, text).ratio()
                    * 0.6
                )
            if score > best_score:
                best_score = score
                best_idx = opt["idx"]

        if best_idx < 0 or best_score < 0.5:
            # US-default safety net for work-authorization questions:
            # this software is currently shipped to US-citizens applying
            # to US jobs (see memory: project context). When the radio
            # is clearly a work-auth question and we couldn't otherwise
            # match, default to the "Yes / authorized" option. Tighten
            # to per-user opt-in once the audience expands.
            field_label_lower = ""
            try:
                # field.label isn't accessible from this static method,
                # so probe the radio's own ancestry for the question
                # text instead.
                field_label_lower = (await field.element.evaluate(
                    """el => {
                        let cur = el.parentElement;
                        for (let i = 0; i < 8 && cur; i++) {
                            const t = (cur.innerText || '').trim().toLowerCase();
                            if (t.length > 5 && t.length < 400) return t;
                            cur = cur.parentElement;
                        }
                        return '';
                    }"""
                )) or ""
            except Exception:
                field_label_lower = ""
            is_work_auth_q = any(
                kw in field_label_lower
                for kw in (
                    "work authorization", "authorized to work",
                    "legally authorized", "authorization status",
                    "right to work",
                )
            )
            if is_work_auth_q:
                for opt in options:
                    text = (opt.get("label") or opt.get("value") or "").strip().lower()
                    if any(h in text for h in AFFIRMATIVE_OPT_HINTS):
                        logger.warning(
                            "radio resolver: work-auth fallback → "
                            "%r (US-default: assume Yes for citizens)",
                            opt.get("label", "")[:60],
                        )
                        best_idx = opt["idx"]
                        best_score = 0.5  # accept
                        break
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

    # Field-label substrings that are NOT skill-shaped — recording
    # them as "skill gaps" would surface EEO disclosures, demographic
    # questions, and yes/no compliance prompts as "skills the user
    # should add to their resume" in the refine flow. They're real
    # unanswered fields (the LLM still gets them via _record_unanswered
    # so the user can teach answers.json), but they don't belong in
    # the skill-gap → resume-bullet pipeline.
    _NON_SKILL_LABEL_FRAGMENTS = (
        # EEO / demographics — never surface as skills
        "voluntary self identif",
        "self-identification",
        "race", "ethnicity", "gender", "sex",
        "veteran", "military",
        "disability", "disabled",
        "pronoun",
        "lgbt",
        "sexual orientation",
        "marital status",
        # Age compliance
        "18 years of age",
        "at least 18",
        "minimum age",
        # Address / contact (handled by personal_info)
        "street address", "mailing address", "home address",
        "city ", "city,", "city*", " city",
        "zip code", "postal code",
        "phone number", "mobile number",
        # Authorization / sponsorship — handled by work_auth + answers.json
        "work authorization",
        "authorized to work",
        "require sponsorship", "visa sponsorship",
        "visa",
        "us citizen", "u.s. citizen",
        "permanent resident",
        # Education credentialing — degree-checks aren't skills
        "bachelor's degree", "bachelors degree",
        "master's degree", "masters degree",
        "ph.d", "phd",
        "diploma",
        # Employment-status compliance — not a skill
        "employed with", "currently employed",
        # Salary / compensation — handled by smart_fallback
        "desired salary", "expected salary",
        "salary expectation",
        "hourly rate",
        # Compliance yes/no boilerplate
        "background check",
        "drug test",
        "criminal", "felony", "convicted",
        "non-compete", "ndas", "non-disclosure",
        "debarred", "excluded",
        "physical requirements",
        "certify that the information",
        # Communications opt-in
        "text messages", "sms",
        "receive emails", "email updates",
        # Source attribution
        "how did you hear",
        "where did you hear",
        "referred by",
    )

    # Label-prefix patterns rejected as non-skill. Substring matching
    # in _NON_SKILL_LABEL_FRAGMENTS is too greedy for these (every
    # short stem like "do you" appears in genuine skill questions),
    # so we anchor them to the start of the (lowered, stripped) label.
    _NON_SKILL_LABEL_PREFIXES = (
        "are you currently",
        "do you have a ",
    )

    @classmethod
    def _is_skill_shaped(cls, label: str) -> bool:
        """True if the field label looks like a skill / experience question.

        Negative signal beats positive signal: anything matching the
        EEO/demographic/compliance fragments above is not a skill,
        no matter what other words appear.
        """
        lower = label.lower()
        stripped = lower.lstrip()
        for prefix in cls._NON_SKILL_LABEL_PREFIXES:
            if stripped.startswith(prefix):
                return False
        for frag in cls._NON_SKILL_LABEL_FRAGMENTS:
            if frag in lower:
                return False
        return True

    async def _dump_failed_element(
        self, field: FormField, answer: str, source: str,
    ) -> None:
        """Log the element's outerHTML when apply_answer returned False.

        When the form filler has a non-empty answer and the apply
        step still fails, the most common cause is that the detected
        element is the WRONG element — e.g. ``find_form_fields``
        matched a label wrapper that resolved to a sibling text input
        when the actual control is a hidden checkbox or a custom
        widget two divs over. Dumping outerHTML at the point of
        failure makes the next diagnosis a copy-paste away from a
        fix instead of another live run.

        Truncated to 400 chars so the run log stays readable.
        """
        try:
            outer = await field.element.evaluate(
                "el => el.outerHTML"
            )
        except Exception as exc:
            outer = f"<could not capture: {exc}>"
        outer = (outer or "")[:400]
        logger.warning(
            "FAILED_FILL label=%r type=%s source=%s answer=%r "
            "element_html=%s",
            field.label[:80], field.field_type, source,
            answer[:60], outer,
        )

    def _record_gap(self, field: FormField, job_id: str) -> None:
        """Record an unfilled field as a skill gap.

        Filtered: EEO / demographic / compliance / boilerplate
        questions are NOT recorded as skill gaps. They still flow
        through ``_record_unanswered`` so the LLM and answers.json
        layer eventually learn them; they just don't pollute the
        refine-flow skill list.
        """
        if not self._is_skill_shaped(field.label):
            logger.debug(
                "Skipping non-skill gap: %r (boilerplate / demographic / compliance)",
                field.label[:80],
            )
            return
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
