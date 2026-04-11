"""Preflight check runner — validates everything Auto Applier needs to run.

Called from ``python -m auto_applier --cli doctor``. Each check is a
small function that returns a :class:`CheckResult`. The runner prints a
structured report and exits non-zero if any required check fails.

Design goals:

- **Fast**: total runtime well under 5 seconds on a healthy machine.
- **Actionable**: every FAIL or WARN carries a one-line fix hint.
- **Safe**: checks are read-only. Doctor never mutates user data.
- **Categorised**: PASS (green), WARN (yellow — non-blocking), FAIL (red — blocks `run`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    message: str
    fix: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_python_version() -> CheckResult:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info.micro}"
    if (major, minor) >= (3, 11):
        return CheckResult("Python version", PASS, version)
    return CheckResult(
        "Python version", FAIL, f"{version} (need >= 3.11)",
        fix="Install Python 3.11+ from python.org",
    )


def check_data_dirs_writable() -> CheckResult:
    from auto_applier.config import (
        DATA_DIR, BROWSER_PROFILE_DIR, RESUMES_DIR, PROFILES_DIR,
        CACHE_DIR, BACKUP_DIR, GENERATED_RESUMES_DIR, RESEARCH_DIR,
    )

    dirs = [
        DATA_DIR, BROWSER_PROFILE_DIR, RESUMES_DIR, PROFILES_DIR,
        CACHE_DIR, BACKUP_DIR, GENERATED_RESUMES_DIR, RESEARCH_DIR,
    ]
    for d in dirs:
        if not d.exists():
            return CheckResult(
                "Data directories", FAIL, f"missing: {d}",
                fix=f"Create {d} or re-run the GUI wizard",
            )
        if not os.access(d, os.W_OK):
            return CheckResult(
                "Data directories", FAIL, f"not writable: {d}",
                fix=f"chmod / grant write access to {d}",
            )
    return CheckResult("Data directories", PASS, f"all {len(dirs)} writable")


def check_env_file() -> CheckResult:
    from auto_applier.config import PROJECT_ROOT

    env = PROJECT_ROOT / ".env"
    example = PROJECT_ROOT / ".env.example"
    if env.exists():
        return CheckResult(".env file", PASS, "present")
    if example.exists():
        return CheckResult(
            ".env file", WARN, "missing (.env.example available)",
            fix="Copy .env.example to .env and fill in credentials",
        )
    return CheckResult(
        ".env file", WARN, "missing",
        fix="Create .env at project root (see CLAUDE.md for format)",
    )


def check_user_config() -> CheckResult:
    from auto_applier.config import USER_CONFIG_FILE

    if not USER_CONFIG_FILE.exists():
        return CheckResult(
            "user_config.json", FAIL, "missing",
            fix="Run the GUI wizard once to create a user profile",
        )
    try:
        data = json.loads(USER_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(
            "user_config.json", FAIL, f"unreadable: {e}",
            fix="Delete data/user_config.json and re-run the wizard",
        )
    personal = data.get("personal_info", {}) or data.get("personal", {}) or data
    missing = [k for k in ("name", "email") if not personal.get(k)]
    if missing:
        return CheckResult(
            "user_config.json", WARN,
            f"missing fields: {', '.join(missing)}",
            fix="Re-open the wizard and complete the Personal Info step",
        )
    return CheckResult("user_config.json", PASS, "name + email present")


def check_resumes_loaded() -> CheckResult:
    from auto_applier.config import RESUMES_DIR, PROFILES_DIR

    resumes = [p for p in RESUMES_DIR.iterdir() if p.is_file()] if RESUMES_DIR.exists() else []
    profiles = list(PROFILES_DIR.glob("*.json")) if PROFILES_DIR.exists() else []

    if not resumes and not profiles:
        return CheckResult(
            "Resumes", FAIL, "none loaded",
            fix="Use the GUI wizard to add at least one resume file",
        )
    if not profiles:
        return CheckResult(
            "Resumes", WARN,
            f"{len(resumes)} file(s) in data/resumes but no parsed profiles",
            fix="Open the wizard so each resume is parsed into data/profiles/",
        )
    if not resumes:
        return CheckResult(
            "Resumes", WARN,
            f"{len(profiles)} profile(s) but no source files in data/resumes",
            fix="Re-add resume files via the wizard so uploads still work",
        )
    return CheckResult(
        "Resumes", PASS, f"{len(resumes)} file(s), {len(profiles)} profile(s)",
    )


def check_answers_file() -> CheckResult:
    from auto_applier.config import ANSWERS_FILE

    if not ANSWERS_FILE.exists():
        return CheckResult(
            "answers.json", WARN, "missing (LLM will answer from scratch)",
            fix="Run the wizard's Answers step, or create an empty {} file",
        )
    try:
        json.loads(ANSWERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            "answers.json", FAIL, "unreadable JSON",
            fix="Delete data/answers.json and re-run the wizard",
        )
    return CheckResult("answers.json", PASS, "valid JSON")


async def check_ollama() -> CheckResult:
    from auto_applier.config import OLLAMA_MODEL, OLLAMA_MIN_VERSION
    from auto_applier.llm.ollama_backend import OllamaBackend, version_gte

    backend = OllamaBackend()
    version = await backend.get_version()
    if not version:
        return CheckResult(
            "Ollama server", FAIL, "not reachable at configured URL",
            fix="Install from ollama.com and run 'ollama serve'",
        )
    if not version_gte(version, OLLAMA_MIN_VERSION):
        return CheckResult(
            "Ollama server", FAIL,
            f"v{version} < minimum v{OLLAMA_MIN_VERSION}",
            fix=f"Upgrade Ollama to >= {OLLAMA_MIN_VERSION} (supports Gemma 4)",
        )
    available = await backend.is_available()
    if not available:
        return CheckResult(
            "Ollama server", FAIL,
            f"v{version} running but model '{OLLAMA_MODEL}' not pulled",
            fix=f"Run: ollama pull {OLLAMA_MODEL}",
        )
    return CheckResult(
        "Ollama server", PASS, f"v{version}, {OLLAMA_MODEL} ready",
    )


def check_gemini_key() -> CheckResult:
    from auto_applier.config import GEMINI_API_KEY

    if not GEMINI_API_KEY:
        return CheckResult(
            "Gemini fallback", WARN, "no API key configured",
            fix="Add GEMINI_API_KEY to .env (free at ai.google.dev)",
        )
    return CheckResult("Gemini fallback", PASS, "API key present")


def check_playwright() -> CheckResult:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return CheckResult(
            "Playwright", FAIL, "not installed",
            fix="pip install -e . (installs playwright)",
        )

    # Check if the chromium binary is on disk. Playwright exposes an
    # executable_path property but only inside an async context — walk
    # the install cache instead.
    candidates = [
        Path.home() / "AppData" / "Local" / "ms-playwright",       # Windows
        Path.home() / ".cache" / "ms-playwright",                  # Linux
        Path.home() / "Library" / "Caches" / "ms-playwright",      # macOS
    ]
    for root in candidates:
        if root.exists() and any(root.glob("chromium*")):
            return CheckResult("Playwright chromium", PASS, "installed")
    return CheckResult(
        "Playwright chromium", FAIL, "browser binary not found",
        fix="Run: playwright install chromium",
    )


def check_patchright() -> CheckResult:
    try:
        import patchright  # noqa: F401
        return CheckResult("patchright", PASS, "available")
    except ImportError:
        return CheckResult(
            "patchright", WARN, "not installed (using vanilla Playwright)",
            fix="pip install patchright (optional, improves anti-detection)",
        )


def check_disk_space() -> CheckResult:
    from auto_applier.config import DATA_DIR

    try:
        total, used, free = shutil.disk_usage(DATA_DIR)
    except OSError as e:
        return CheckResult("Disk space", WARN, f"could not read: {e}")

    free_gb = free / (1024 ** 3)
    if free_gb < 1.0:
        return CheckResult(
            "Disk space", FAIL, f"only {free_gb:.1f} GB free",
            fix="Free at least 2 GB — browser profile and LLM cache grow over time",
        )
    if free_gb < 2.0:
        return CheckResult(
            "Disk space", WARN, f"{free_gb:.1f} GB free (recommend >= 2 GB)",
        )
    return CheckResult("Disk space", PASS, f"{free_gb:.1f} GB free")


def check_screen_resolution() -> CheckResult:
    """The headed browser needs a real display at >= 1280x720."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
    except Exception as e:
        return CheckResult(
            "Screen resolution", WARN, f"could not detect: {e}",
            fix="Headed browser requires a real display (1280x720 minimum)",
        )
    if w < 1280 or h < 720:
        return CheckResult(
            "Screen resolution", WARN, f"{w}x{h} (recommend >= 1280x720)",
        )
    return CheckResult("Screen resolution", PASS, f"{w}x{h}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run_all() -> list[CheckResult]:
    """Run every check. Async so Ollama doesn't block the others."""
    sync_checks = [
        check_python_version,
        check_data_dirs_writable,
        check_env_file,
        check_user_config,
        check_resumes_loaded,
        check_answers_file,
        check_gemini_key,
        check_playwright,
        check_patchright,
        check_disk_space,
        check_screen_resolution,
    ]
    results = [fn() for fn in sync_checks]
    # Ollama is the only async check.
    results.append(await check_ollama())
    return results


def _format(result: CheckResult) -> str:
    icon = {PASS: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}[result.status]
    line = f"  {icon}  {result.name:24s}  {result.message}"
    if result.fix and result.status != PASS:
        line += f"\n           fix: {result.fix}"
    return line


def run(verbose: bool = False) -> int:
    """Entry point called from CLI. Returns an exit code (0 = healthy)."""
    print("Auto Applier preflight check")
    print("=" * 60)

    try:
        results = asyncio.run(_run_all())
    except Exception as e:
        print(f"  fatal: {e}")
        return 2

    for r in results:
        print(_format(r))

    print("=" * 60)
    failures = sum(1 for r in results if r.status == FAIL)
    warnings = sum(1 for r in results if r.status == WARN)
    passes = len(results) - failures - warnings

    print(f"  {passes} pass  /  {warnings} warn  /  {failures} fail")

    if failures:
        print("\nOne or more required checks failed. Fix them before running.")
        return 1
    if warnings:
        print("\nAll required checks passed. Warnings are optional to fix.")
    else:
        print("\nReady to run.")
    return 0
