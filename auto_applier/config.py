"""Application configuration loaded from .env and defaults."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root is one level up from this file
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
RESUMES_DIR = DATA_DIR / "resumes"

# CSV data files
JOBS_CSV = DATA_DIR / "jobs.csv"
APPLICATIONS_CSV = DATA_DIR / "applications.csv"
SKILL_GAPS_CSV = DATA_DIR / "skill_gaps.csv"
USER_CONFIG_FILE = DATA_DIR / "user_config.json"

# Ensure data directories exist
DATA_DIR.mkdir(exist_ok=True)
BROWSER_PROFILE_DIR.mkdir(exist_ok=True)
RESUMES_DIR.mkdir(exist_ok=True)

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")


def get_linkedin_email() -> str:
    return os.getenv("LINKEDIN_EMAIL", "")


def get_linkedin_password() -> str:
    return os.getenv("LINKEDIN_PASSWORD", "")


# Rate limiting / anti-detection defaults
MAX_APPLICATIONS_PER_DAY = int(os.getenv("MAX_APPLICATIONS_PER_DAY", "10"))
MIN_DELAY_BETWEEN_ACTIONS = float(os.getenv("MIN_DELAY_BETWEEN_ACTIONS", "3"))
MAX_DELAY_BETWEEN_ACTIONS = float(os.getenv("MAX_DELAY_BETWEEN_ACTIONS", "8"))
MIN_DELAY_BETWEEN_APPLICATIONS = float(os.getenv("MIN_DELAY_BETWEEN_APPLICATIONS", "60"))
MAX_DELAY_BETWEEN_APPLICATIONS = float(os.getenv("MAX_DELAY_BETWEEN_APPLICATIONS", "180"))
