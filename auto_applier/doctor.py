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
    env_txt = PROJECT_ROOT / ".env.txt"
    example = PROJECT_ROOT / ".env.example"
    if env.exists():
        return CheckResult(".env file", PASS, "present")
    # Windows hides ".txt" by default, so users who try to make a
    # ".env" file in Notepad often end up with ".env.txt" without
    # realizing it. Detect that case explicitly so they don't sit
    # confused.
    if env_txt.exists():
        return CheckResult(
            ".env file", FAIL,
            "found '.env.txt' (Windows added a hidden .txt extension)",
            fix=(
                "Rename '.env.txt' to '.env'. In File Explorer:\n"
                "           1. View menu -> turn ON 'File name extensions'.\n"
                "           2. Right-click '.env.txt', Rename, remove '.txt'.\n"
                "           Confirm Windows' \"are you sure?\" prompt with Yes."
            ),
        )
    if example.exists():
        return CheckResult(
            ".env file", WARN, "missing (.env.example available)",
            fix=(
                "Copy .env.example to .env and fill in credentials. "
                "If you tried this in Notepad and Windows saved it as "
                "'.env.txt', see this check's repeat output for the "
                "rename steps."
            ),
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
    # Accept either a combined 'name' field or first_name + last_name.
    # The wizard saves them separately; the fixture generator writes
    # both. Either shape counts as "has a name".
    has_name = bool(
        personal.get("name")
        or (personal.get("first_name") and personal.get("last_name"))
    )
    has_email = bool(personal.get("email"))
    missing = []
    if not has_name:
        missing.append("name")
    if not has_email:
        missing.append("email")
    if missing:
        return CheckResult(
            "user_config.json", WARN,
            f"missing fields: {', '.join(missing)}",
            fix="Re-open the wizard and complete the Personal Info step",
        )
    # Soft-check the recommended-but-not-required keys. The form filler
    # falls back to LLM/answers.json when these are missing, but each
    # missing key is one more LLM call per application + one more chance
    # for the answer to drift from the user's actual info.
    recommended = (
        "phone", "first_name", "last_name", "city", "state",
        "zip_code", "country",
    )
    soft_missing = [k for k in recommended if not personal.get(k)]
    if soft_missing:
        return CheckResult(
            "user_config.json", WARN,
            f"name + email OK, but missing recommended: {', '.join(soft_missing)}",
            fix="Open Personal Info step in the wizard to fill the rest",
        )
    return CheckResult("user_config.json", PASS, "name + email + recommended fields")


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
            fix=(
                "Open the GUI wizard (run.bat) and click 'Next' past the "
                "'Answers' step at least once — the file is created on "
                "advance. Empty / partial answers are fine; this just "
                "creates the file so the LLM has a baseline to consult."
            ),
        )
    try:
        data = json.loads(ANSWERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            "answers.json", FAIL, "unreadable JSON",
            fix="Delete data/answers.json and re-run the wizard",
        )
    # answers.json supports two on-disk shapes: a flat
    # {question: answer} dict (current default) or a list of
    # {question, answer, aliases?} entries (richer alias matching).
    # Validate whichever is present so a half-filled or wrong-shape
    # file fails preflight instead of silently producing zero matches
    # at runtime.
    bad_entries = 0
    total = 0
    if isinstance(data, dict):
        total = len(data)
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                bad_entries += 1
            elif not k.strip() or not v.strip():
                bad_entries += 1
    elif isinstance(data, list):
        total = len(data)
        for entry in data:
            if not isinstance(entry, dict):
                bad_entries += 1
                continue
            q = entry.get("question") or ""
            a = entry.get("answer") or ""
            if not q.strip() or not a.strip():
                bad_entries += 1
    else:
        return CheckResult(
            "answers.json", FAIL, "must be a JSON object or list",
            fix="Re-run the wizard's Answers step to regenerate the file",
        )
    if bad_entries:
        return CheckResult(
            "answers.json", WARN,
            f"{bad_entries}/{total} entries missing question or answer",
            fix="Open Answers step in the wizard and remove blanks",
        )
    return CheckResult(
        "answers.json", PASS, f"{total} entries, valid",
    )


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
    # Re-read .env every check rather than rely on the module-level
    # GEMINI_API_KEY constant. config.py loads .env once at import
    # time; the wizard writes a freshly-pasted key to .env in the
    # same process — so the module constant is stale and the check
    # would falsely report "not configured" until the user restarts.
    from auto_applier.config import PROJECT_ROOT
    import os as _os
    GEMINI_API_KEY = ""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("GEMINI_API_KEY="):
                    GEMINI_API_KEY = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    if not GEMINI_API_KEY:
        # Fall back to whatever's in the live process env (in case
        # the user set it via OS env vars instead of .env).
        GEMINI_API_KEY = _os.getenv("GEMINI_API_KEY", "")

    if not GEMINI_API_KEY:
        return CheckResult(
            "Gemini fallback", WARN, "no API key configured",
            fix=(
                "Free, takes ~60 seconds:\n"
                "           1. Open https://aistudio.google.com/apikey in your browser.\n"
                "           2. Sign in with any Google account.\n"
                "           3. Click 'Create API key' (top right).\n"
                "              Pick 'Create API key in new project' if it asks.\n"
                "           4. Copy the long string that starts with 'AIzaSy...'.\n"
                "           5. Open '.env' in Notepad, add a line:\n"
                "                  GEMINI_API_KEY=AIzaSy...your_key_here\n"
                "           6. Save and re-run doctor.\n"
                "           No credit card. Free tier is 1500 req/day, plenty for this app."
            ),
        )
    return CheckResult("Gemini fallback", PASS, "API key present")


async def check_llm_smoke() -> CheckResult:
    """Verify the active LLM backend produces non-empty output.

    `is_available()` only checks that the model is registered with
    Ollama / that the API key is configured — it does NOT check that
    the model actually generates text. A broken Gemma installation
    or a stuck model can pass `check_ollama()` and then return ""
    for every prompt, dead-locking the form filler. This smoke test
    sends a trivial prompt and confirms the backend speaks back.

    Emits WARN (not FAIL) so a sluggish but warming model doesn't
    block the run; the user gets a heads-up before applies start.
    """
    from auto_applier.llm.router import LLMRouter

    router = LLMRouter()
    try:
        await router.initialize()
    except Exception as exc:
        return CheckResult(
            "LLM smoke test", WARN,
            f"could not initialize router: {exc}",
            fix="Run --cli doctor again after fixing earlier checks",
        )

    active = router.active_backend
    if active == "rule-based":
        return CheckResult(
            "LLM smoke test", WARN,
            "only rule-based backend active (no LLM available)",
            fix="Start Ollama or add GEMINI_API_KEY for richer answers",
        )

    try:
        response = await router.complete(
            prompt="Reply with just the word 'ok'.",
            temperature=0.0,
            max_tokens=8,
            use_cache=False,
        )
    except Exception as exc:
        return CheckResult(
            "LLM smoke test", WARN,
            f"{active} call raised: {exc}",
            fix="Check Ollama server logs / Gemini quota",
        )
    text = (response.text or "").strip()
    if not text:
        return CheckResult(
            "LLM smoke test", WARN,
            f"{active} returned empty text on a trivial prompt",
            fix=(
                f"Model may be stuck. Try: ollama stop && ollama serve "
                f"and retry. If repeats, switch OLLAMA_MODEL preset."
            ),
        )
    return CheckResult(
        "LLM smoke test", PASS,
        f"{active} answered ({len(text)} chars)",
    )


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
            fix="Free at least 1 GB to proceed; 2 GB recommended — browser profile and LLM cache grow over time",
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
    # Async checks run sequentially — smoke test depends on Ollama
    # being responsive, so order matters.
    results.append(await check_ollama())
    results.append(await check_llm_smoke())
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
