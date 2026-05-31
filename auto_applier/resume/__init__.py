"""Résumé: master fact bank + per-job generation + fabrication guard (spec §6b)."""

from auto_applier.resume.factbank import (
    Contact,
    EducationEntry,
    FactBank,
    WorkEntry,
)
from auto_applier.resume.guard import (
    Finding,
    GeneratedResume,
    GenEducation,
    GenWorkEntry,
    GuardResult,
    Severity,
    Verdict,
    guard_l1,
)

__all__ = [
    "Contact",
    "EducationEntry",
    "FactBank",
    "Finding",
    "GenEducation",
    "GenWorkEntry",
    "GeneratedResume",
    "GuardResult",
    "Severity",
    "Verdict",
    "WorkEntry",
    "guard_l1",
]
