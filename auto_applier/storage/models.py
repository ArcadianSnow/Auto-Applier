"""Data models used across the application."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    job_id: str  # Platform-specific job ID
    title: str
    company: str
    url: str
    description: str = ""
    search_keyword: str = ""
    source: str = "linkedin"  # Platform source_id
    found_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Application:
    job_id: str
    status: str  # 'applied', 'failed', 'skipped', 'dry_run'
    failure_reason: str = ""
    source: str = "linkedin"  # Platform source_id
    applied_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class SkillGap:
    job_id: str
    field_label: str  # The question/field that was asked
    category: str = "other"  # 'skill', 'certification', 'experience', 'other'
    first_seen: str = field(default_factory=lambda: datetime.now().isoformat())
