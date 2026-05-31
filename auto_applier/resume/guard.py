"""Fabrication guard (spec §6b, research/fabrication-guard.md).

Phase 1 ships **Layer 1 — the deterministic hard gate**: the highest-precision, highest-
damage checks, fully local, no model. It verifies every *checkable* fact in a generated
résumé against the structured fact bank (allow-list, not block-list):

  * every work COMPANY matches a bank company (normalized + fuzzy)   → else HARD_FAIL
  * every work DATE range ⊆ that company's bank span                  → else HARD_FAIL
  * every TITLE is close to a bank title (rephrasing OK)              → else REVIEW
  * every SKILL matches a bank skill                                  → else HARD_FAIL
  * every DEGREE / INSTITUTION matches the bank                       → else HARD_FAIL
  * every $/% METRIC traces to allowed_metrics                        → else HARD_FAIL
    (scale claims "team of N"/"Nx" not in allowed_metrics             → REVIEW)

Bias to REVIEW; fail closed. Any HARD_FAIL blocks auto-apply (regenerate); any REVIEW
routes the job to the human queue. Layers 2–4 (embedding retrieval → NLI → LLM self-check,
for phrasing-level grounding) are deferred to Phase 3 — L1 alone catches the lies that get
people fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from rapidfuzz import fuzz

from auto_applier.resume.factbank import FactBank

# --- tuning (research operating points; conservative, fail-closed) ----------
_COMPANY_PASS = 88
_COMPANY_REVIEW = 78
_SKILL_PASS = 85
_TITLE_PASS = 70
_CREDENTIAL_PASS = 85

_ABBREV = {
    "sr": "senior", "jr": "junior", "&": "and", "u.s.": "us", "u.s": "us",
    "corp": "corporation", "inc": "", "llc": "", "ltd": "",
}


# --- structured generated résumé (what the generator emits; guard's input) --
@dataclass
class GenWorkEntry:
    company: str
    title: str
    start: str = ""
    end: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class GenEducation:
    institution: str
    degree: str = ""


@dataclass
class GeneratedResume:
    summary: str = ""
    work: list[GenWorkEntry] = field(default_factory=list)
    education: list[GenEducation] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


# --- verdict ----------------------------------------------------------------
class Verdict(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    HARD_FAIL = "HARD_FAIL"


class Severity(str, Enum):
    REVIEW = "REVIEW"
    HARD_FAIL = "HARD_FAIL"


@dataclass
class Finding:
    severity: Severity
    category: str       # company | date | title | skill | credential | metric
    claim: str
    reason: str


@dataclass
class GuardResult:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only if the résumé is eligible to auto-apply."""
        return self.verdict is Verdict.PASS

    def hard_fails(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.HARD_FAIL]


# --- normalization + matching ----------------------------------------------
def _norm(s: str) -> str:
    """Lowercase, expand a few abbreviations, drop punctuation except $ and %,
    collapse whitespace. Keeps $/% so money/percent metrics stay matchable."""
    s = s.lower()
    for k, v in _ABBREV.items():
        s = re.sub(rf"\b{re.escape(k)}\b", v, s)
    s = re.sub(r"[^a-z0-9$%\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _best_score(value: str, candidates: list[str]) -> tuple[float, str]:
    """Best fuzzy (token_sort_ratio) of ``value`` against ``candidates``."""
    nv = _norm(value)
    best, best_c = 0.0, ""
    for c in candidates:
        sc = fuzz.token_sort_ratio(nv, _norm(c))
        if sc > best:
            best, best_c = sc, c
    return best, best_c


# --- date handling ----------------------------------------------------------
_PRESENT = (9999, 12)


def _parse_date(s: str) -> tuple[int, int] | None:
    """Parse 'YYYY-MM' / 'YYYY' / 'Present' / '' → (year, month). None if unparseable."""
    if not s:
        return None
    low = s.strip().lower()
    if low in {"present", "current", "now"}:
        return _PRESENT
    m = re.match(r"(\d{4})(?:[-/](\d{1,2}))?", s.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 1
    return (year, month)


def _range_within(gen_start, gen_end, bank_start, bank_end) -> bool:
    """Is [gen_start, gen_end] ⊆ [bank_start, bank_end]? Unparseable bounds are lenient
    (can't prove a violation → not a hard fail; phrasing layers handle the rest)."""
    gs, ge = _parse_date(gen_start), _parse_date(gen_end or "Present")
    bs, be = _parse_date(bank_start), _parse_date(bank_end or "Present")
    if gs is not None and bs is not None and gs < bs:
        return False
    if ge is not None and be is not None and ge > be:
        return False
    return True


# --- metric extraction ------------------------------------------------------
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*\.?\d*\s?(?:[kmb]|million|billion|thousand)?", re.I)
_PCT_RE = re.compile(r"\d+\.?\d*\s?%")
_SCALE_RE = re.compile(r"\b(?:team of \d+|\d+\s?x|\d{3,}\+?)\b", re.I)


def _metric_supported(metric: str, allowed_norm: str) -> bool:
    """Is the numeric core of ``metric`` present anywhere in the allowed-metrics text?"""
    return _norm(metric) in allowed_norm


# --- the gate ---------------------------------------------------------------
def guard_l1(resume: GeneratedResume, bank: FactBank) -> GuardResult:
    """Run Layer-1 deterministic checks. Returns a :class:`GuardResult`."""
    findings: list[Finding] = []
    allowed_norm = " ".join(_norm(m) for m in bank.allowed_metrics)
    bank_companies = bank.companies()
    bank_skills = bank.skills
    bank_credentials = bank.degrees() + bank.institutions() + bank.certifications

    # --- work history: company, dates, title, bullet metrics ---
    for w in resume.work:
        c_score, c_match = _best_score(w.company, bank_companies)
        if c_score < _COMPANY_REVIEW:
            findings.append(Finding(
                Severity.HARD_FAIL, "company", w.company,
                f"no bank company resembles '{w.company}' (best {c_score:.0f})",
            ))
            continue  # can't date-check against a company that isn't ours
        if c_score < _COMPANY_PASS:
            findings.append(Finding(
                Severity.REVIEW, "company", w.company,
                f"near-miss to bank company '{c_match}' ({c_score:.0f}) — verify",
            ))

        bank_entry = bank.entry_for_company(c_match)
        if bank_entry and not _range_within(w.start, w.end, bank_entry.start, bank_entry.end):
            findings.append(Finding(
                Severity.HARD_FAIL, "date", f"{w.company} {w.start}–{w.end}",
                f"dates fall outside bank span {bank_entry.start}–{bank_entry.end or 'Present'}",
            ))

        if bank_entry:
            t_score, _ = _best_score(w.title, [bank_entry.title] + bank.titles())
            if t_score < _TITLE_PASS:
                findings.append(Finding(
                    Severity.REVIEW, "title", w.title,
                    f"title differs from bank '{bank_entry.title}' ({t_score:.0f}) — possible inflation",
                ))

        for bullet in w.bullets:
            for metric in _MONEY_RE.findall(bullet) + _PCT_RE.findall(bullet):
                if not _metric_supported(metric, allowed_norm):
                    findings.append(Finding(
                        Severity.HARD_FAIL, "metric", metric.strip(),
                        f"'{metric.strip()}' not in allowed_metrics (invented/inflated number)",
                    ))
            for scale in _SCALE_RE.findall(bullet):
                if not _metric_supported(scale, allowed_norm):
                    findings.append(Finding(
                        Severity.REVIEW, "metric", scale.strip(),
                        f"scale claim '{scale.strip()}' not in allowed_metrics — verify",
                    ))

    # --- skills: each must be in the bank (allow-list) ---
    for skill in resume.skills:
        s_score, _ = _best_score(skill, bank_skills)
        if s_score < _SKILL_PASS:
            findings.append(Finding(
                Severity.HARD_FAIL, "skill", skill,
                f"skill '{skill}' not in bank (best {s_score:.0f}) — invented",
            ))

    # --- education: degrees/institutions must be in the bank ---
    for edu in resume.education:
        for value, cat in ((edu.institution, "institution"), (edu.degree, "degree")):
            if not value:
                continue
            score, _ = _best_score(value, bank_credentials)
            if score < _CREDENTIAL_PASS:
                findings.append(Finding(
                    Severity.HARD_FAIL, "credential", value,
                    f"{cat} '{value}' not in bank (best {score:.0f}) — invented credential",
                ))

    # --- aggregate (fail closed) ---
    if any(f.severity is Severity.HARD_FAIL for f in findings):
        verdict = Verdict.HARD_FAIL
    elif findings:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.PASS
    return GuardResult(verdict=verdict, findings=findings)
