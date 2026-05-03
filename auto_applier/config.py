"""Application configuration loaded from .env and defaults."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Project root is one level up from this file
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
RESUMES_DIR = DATA_DIR / "resumes"
CONVERTED_RESUMES_DIR = RESUMES_DIR / ".converted"
PROFILES_DIR = DATA_DIR / "profiles"
CACHE_DIR = DATA_DIR / "cache"
BACKUP_DIR = DATA_DIR / ".backups"
SCHEMA_VERSION_FILE = DATA_DIR / ".schema_version.json"
GENERATED_RESUMES_DIR = PROFILES_DIR / "generated"
RESEARCH_DIR = DATA_DIR / "research"
LOGS_DIR = DATA_DIR / "logs"
COVER_LETTERS_DIR = DATA_DIR / "cover_letters"

# CSV data files
JOBS_CSV = DATA_DIR / "jobs.csv"
APPLICATIONS_CSV = DATA_DIR / "applications.csv"
SKILL_GAPS_CSV = DATA_DIR / "skill_gaps.csv"
FOLLOWUPS_CSV = DATA_DIR / "followups.csv"
USER_CONFIG_FILE = DATA_DIR / "user_config.json"
ANSWERS_FILE = DATA_DIR / "answers.json"
UNANSWERED_FILE = DATA_DIR / "unanswered.json"

# Ensure data directories exist
for d in [DATA_DIR, BROWSER_PROFILE_DIR, RESUMES_DIR, CONVERTED_RESUMES_DIR, PROFILES_DIR, CACHE_DIR, BACKUP_DIR, GENERATED_RESUMES_DIR, RESEARCH_DIR, LOGS_DIR, COVER_LETTERS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

# LLM settings
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_MIN_VERSION = "0.8.0"  # Minimum Ollama version for Gemma 4 support
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# Known-good Ollama model options shown in the wizard dropdown.
# Listed fastest-to-biggest. Users can still type a custom tag.
#
# Hardware tiers (rough; YMMV with quantization):
#   8 GB RAM, no GPU            -> phi-4-mini, gemma4:e2b
#   16 GB RAM, no GPU           -> qwen3:4b, phi-4-mini
#   16 GB RAM, RTX 3060+ (8 GB) -> qwen3:8b, gemma4:e4b (default)
#   32 GB RAM, RTX 3080+ (10GB) -> gemma4:e4b, qwen3:14b
#   64 GB RAM, RTX 4090 (24 GB) -> gemma4:31b, qwen3:32b
OLLAMA_MODEL_PRESETS = [
    # 2026-Q1 additions (per Tier 4 research): Phi-4-mini for 8 GB
    # rigs, Qwen 3 family for general-purpose at every tier.
    "phi-4-mini",   # ~3.8B Microsoft, strong reasoning, runs on 8 GB RAM CPU-only
    "qwen3:4b",     # Dense 4B, 262k context, beats Gemma 4 e4b on coding
    "qwen3:8b",     # Dense 8B, recommended for RTX 3060+ (8 GB VRAM)
    "qwen3:14b",    # Dense 14B, RTX 3080+ (10 GB VRAM)
    # Gemma 4 family — current default tier
    "gemma4:e2b",   # ~2.3B effective MoE, multimodal, 128k ctx, CPU-friendly
    "gemma4:e4b",   # ~4.5B effective MoE, multimodal, 128k ctx, DEFAULT
    "gemma4:31b",   # Full Gemma 4, 256k ctx, RTX 4090 / Mac M-series only
    # Legacy fallbacks — kept for users on Ollama < 0.8 (no Gemma 4)
    "gemma3:4b",
    "llama3.1:8b",
]

# Rate limiting / anti-detection defaults
MAX_APPLICATIONS_PER_DAY = int(os.getenv("MAX_APPLICATIONS_PER_DAY", "10"))
MIN_DELAY_BETWEEN_ACTIONS = float(os.getenv("MIN_DELAY_BETWEEN_ACTIONS", "3"))
MAX_DELAY_BETWEEN_ACTIONS = float(os.getenv("MAX_DELAY_BETWEEN_ACTIONS", "8"))
MIN_DELAY_BETWEEN_APPLICATIONS = float(os.getenv("MIN_DELAY_BETWEEN_APPLICATIONS", "60"))
MAX_DELAY_BETWEEN_APPLICATIONS = float(os.getenv("MAX_DELAY_BETWEEN_APPLICATIONS", "180"))

# Scoring thresholds
DEFAULT_AUTO_APPLY_MIN = 7
DEFAULT_CLI_AUTO_APPLY_MIN = 7
DEFAULT_REVIEW_MIN = 4
DEFAULT_EVOLUTION_TRIGGER_THRESHOLD = 3

# Ghost-job skip threshold. Jobs scoring at or above this on the 0-10
# ghost scale are skipped before apply without wasting scoring cycles
# on them. Override via GHOST_SKIP_THRESHOLD env var — set it to 11
# to disable the skip gate entirely.
GHOST_SKIP_THRESHOLD = int(os.getenv("GHOST_SKIP_THRESHOLD", "8"))

# Follow-up cadence: days after applied_at when reminders fall due.
# Override via FOLLOWUP_CADENCE_DAYS (comma-separated integers).
_cadence_env = os.getenv("FOLLOWUP_CADENCE_DAYS", "7,14,21")
try:
    FOLLOWUP_CADENCE_DAYS = [int(d.strip()) for d in _cadence_env.split(",") if d.strip()]
except ValueError:
    FOLLOWUP_CADENCE_DAYS = [7, 14, 21]

# Title expansion config — set in user_config.json, NOT env vars,
# so each user can opt in via the wizard / config edit.
#
# auto_expand_titles (bool, default False):
#   When True, if a keyword search returns fewer than
#   title_expansion_threshold jobs, the engine asks the LLM (or static
#   fallback dict) for adjacent titles and queues them into the same
#   keyword loop. Each seed only expands once per run.
#
# title_expansion_threshold (int, default 10):
#   Below this raw-job-count threshold, expansion fires. Bigger numbers
#   = more aggressive broadening. Set to 0 to effectively disable.
#
# Both live under config root in user_config.json:
#   {"auto_expand_titles": true, "title_expansion_threshold": 8, ...}
DEFAULT_AUTO_EXPAND_TITLES = False
DEFAULT_TITLE_EXPANSION_THRESHOLD = 10

# Continuous-run mode — loop the pipeline indefinitely instead of a
# single pass. All settings live under user_config.json so each user
# can tune via the wizard.
#
# continuous_mode (bool, default False):
#   Master switch. When False, `run()` executes one pass and exits
#   (the v1 behavior).
#
# continuous_cycle_delay_min/max (seconds, default 1800/5400 = 30/90 min):
#   Sleep this many seconds (uniformly random) between cycles. Longer
#   than 5 min so the browser fingerprint doesn't look like a bot on
#   a fixed timer. Clamped to >=60 at load time.
#
# continuous_active_hours (str, default "09:00-22:00"):
#   Only run auto-apply cycles during this local-time window. Cycles
#   that wake outside the window fall through to refinement-only mode
#   (browser stays warm, no submissions). Format "HH:MM-HH:MM".
#   Overnight ranges are allowed ("22:00-06:00" spans midnight).
#
# continuous_max_cycles (int, default 0):
#   Safety cap. 0 = unlimited, otherwise stop after N cycles. Used by
#   tests and as a training-wheels option for first continuous runs.
DEFAULT_CONTINUOUS_MODE = False
DEFAULT_CONTINUOUS_CYCLE_DELAY_MIN = 30 * 60
DEFAULT_CONTINUOUS_CYCLE_DELAY_MAX = 90 * 60
DEFAULT_CONTINUOUS_ACTIVE_HOURS = "09:00-22:00"
DEFAULT_CONTINUOUS_MAX_CYCLES = 0
