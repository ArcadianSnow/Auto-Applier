"""First-run setup operations (spec §11a onboarding restructure).

Shared, progress-emitting helpers behind BOTH the CLI (`av3 install-browser`) and the web
dashboard's in-app setup flow (`POST /api/setup/...`). Keeping them here means the two
surfaces run one implementation. ``progress_cb`` receives snapshot-fragment dicts the caller
renders — the web mutates its module-level job dict; the CLI prints them.

The model PULL is HTTP-NDJSON (`POST {ollama}/api/pull`) so the web flow gets structured
per-layer progress without scraping a console — the same HTTP surface the app already uses
for `/api/tags` and `/api/embeddings`. (`av3 setup-llm` keeps its own native `ollama pull`
subprocess because that auto-starts a stopped server and streams Ollama's own bars; see
``cli/main.py``.)

Everything here is read-only/idempotent except the two installs (which are themselves
idempotent — re-running just re-verifies).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from auto_applier.config import Settings
from auto_applier.doctor import CheckResult, check_browser, check_llm

ProgressCb = Callable[[dict], None] | None


@dataclass
class PullResult:
    ok: bool
    models: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class InstallResult:
    ok: bool
    backend_used: str = ""
    error: str = ""


def _emit(cb: ProgressCb, frag: dict) -> None:
    if cb is not None:
        cb(frag)


def _human_bytes(n: int | None) -> str:
    if not n:
        return "0"
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.0f} MB"


def pull_models(settings: Settings, progress_cb: ProgressCb = None) -> PullResult:
    """Pull the completion + embedding models via Ollama's streaming HTTP API.

    Streams ``POST {host}/api/pull`` NDJSON for each of ``settings.llm.ollama_model`` and
    ``settings.llm.embed_model``, emitting per-layer percent through ``progress_cb``. Blocking
    (run it via ``asyncio.to_thread`` from async callers). On a connection failure (Ollama not
    installed or not running — indistinguishable over HTTP) returns
    ``error="ollama_not_running"`` so the UI can show the Get/Start-Ollama affordance.
    """
    host = settings.llm.ollama_host.rstrip("/")
    models = [settings.llm.ollama_model, settings.llm.embed_model]
    failed: list[str] = []

    for idx, model in enumerate(models, start=1):
        base = {
            "action": "pull-models", "status": "running",
            "model": model, "model_index": idx, "model_count": len(models),
        }
        _emit(progress_cb, {**base, "phase": "starting", "percent": 0, "detail": ""})
        try:
            with httpx.stream(
                "POST", f"{host}/api/pull",
                json={"model": model, "stream": True},
                timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0),
            ) as resp:
                resp.raise_for_status()
                last_pct = 0
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        failed.append(model)
                        _emit(progress_cb, {**base, "status": "error",
                                            "phase": msg["error"], "error": msg["error"]})
                        break
                    total = msg.get("total")
                    if total:
                        last_pct = round(100 * (msg.get("completed") or 0) / total)
                        detail = f"{_human_bytes(msg.get('completed'))}/{_human_bytes(total)}"
                    else:
                        detail = ""  # manifest / verify / writing phases carry no bytes
                    _emit(progress_cb, {**base, "phase": msg.get("status", ""),
                                        "percent": last_pct, "detail": detail})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Server unreachable: bail with a structured signal; the remaining models
            # can't be pulled either.
            return PullResult(ok=False, models=models, failed=models[idx - 1:],
                              error="ollama_not_running")
        except httpx.HTTPError as exc:
            failed.append(model)
            _emit(progress_cb, {**base, "status": "error", "phase": str(exc),
                                "error": str(exc)})

    ok = not failed
    return PullResult(ok=ok, models=models, failed=failed,
                      error="" if ok else f"failed to pull: {', '.join(failed)}")


def install_browser(progress_cb: ProgressCb = None, backend: str = "auto") -> InstallResult:
    """Download the Chromium browser binary via patchright (preferred) or playwright.

    A running/done/error spinner — ``capture_output=True`` means there's no stream to surface
    (the playwright installer writes its own progress, which we capture and discard). Blocking;
    run via ``asyncio.to_thread`` from async callers. Idempotent.
    """
    order = ["patchright", "playwright"] if backend == "auto" else [backend]
    last_err = ""
    for pkg in order:
        _emit(progress_cb, {"action": "install-browser", "status": "running",
                            "phase": f"installing via {pkg}"})
        try:
            proc = subprocess.run(
                [sys.executable, "-m", pkg, "install", "chromium"],
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            last_err = f"{pkg}: {exc}"
            continue
        if proc.returncode == 0:
            return InstallResult(ok=True, backend_used=pkg)
        last_err = (proc.stderr or proc.stdout or "").strip()[:300]
    return InstallResult(ok=False, error=last_err or "could not install Chromium")


def readiness(settings: Settings) -> list[CheckResult]:
    """The first-run setup checklist: just the two bootstrap concerns (LLM models + browser).

    Reuses the doctor checks. Scoped to setup so the dashboard panel is a focused checklist,
    not the full ``run_doctor()`` dump (which stays the CLI/CI surface).
    """
    return [check_llm(settings), check_browser(settings)]


def ensure_data_dirs(settings: Settings) -> None:
    """Create the data / artifacts / backups dirs (idempotent). Used by first-run `serve`
    and `init-db` so a fresh install never trips a spurious ``check_backups`` WARN."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
