"""Ollama local LLM backend via HTTP API."""

import json
import time

import httpx

from auto_applier.llm.base import LLMBackend, LLMResponse


def version_gte(actual: str, minimum: str) -> bool:
    """Return True if ``actual`` dotted-numeric version is >= ``minimum``.

    Non-numeric suffixes like '-rc1' are stripped. Returns False on
    parse failure — callers should treat that as "version unknown,
    fail closed".
    """
    def parts(v: str) -> tuple:
        out = []
        for chunk in v.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            if not digits:
                return ()
            out.append(int(digits))
        return tuple(out)

    a, b = parts(actual), parts(minimum)
    if not a or not b:
        return False
    return a >= b


class OllamaBackend(LLMBackend):
    """Local Ollama server backend.

    Communicates with the Ollama REST API at ``/api/generate``.
    Preferred backend because it is entirely free and local.
    """

    name = "ollama"

    def __init__(self, base_url: str = "", model: str = "") -> None:
        from auto_applier.config import OLLAMA_BASE_URL, OLLAMA_MODEL

        self.base_url = (base_url or OLLAMA_BASE_URL).rstrip("/")
        self.model = model or OLLAMA_MODEL
        self._client = httpx.AsyncClient(timeout=120.0)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Return *True* if Ollama is running and the target model is pulled."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            # Match on full tag first (e.g. "gemma4:e4b"), then fall back
            # to family prefix ("gemma4") so variants like gemma4:e4b-instruct
            # also count as available.
            target = self.model
            family = target.split(":")[0]
            for m in models:
                name = m.get("name", "")
                if name == target or name.startswith(target + "-"):
                    return True
                if name.startswith(family + ":") or name.startswith(family + "-"):
                    return True
            return False
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return False

    async def get_version(self) -> str:
        """Return the running Ollama server version, or '' if unreachable."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/version")
            if resp.status_code != 200:
                return ""
            return resp.json().get("version", "")
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return ""

    async def list_local_models(self) -> list[str]:
        """Return a list of model tags currently pulled on the Ollama server."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return []
            return [m.get("name", "") for m in resp.json().get("models", [])]
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            return []

    async def pull_model(self, on_progress=None) -> bool:
        """Pull ``self.model`` from the Ollama registry with streaming progress.

        ``on_progress`` is an optional callback ``fn(status, pct)`` where
        ``status`` is a human-readable string like "pulling manifest" or
        "downloading 2.3 GB / 9.6 GB" and ``pct`` is a float 0-100 (or
        None if indeterminate). Called repeatedly from the download loop.

        Returns True on success, False on any failure. The Ollama server
        must be running — callers should ``start_ollama_server`` first.
        """
        import json as _json

        payload = {"name": self.model, "stream": True}
        try:
            # Use a fresh long-lived client — pulls can take minutes.
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/api/pull", json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        if on_progress:
                            on_progress(f"pull failed: HTTP {resp.status_code}", None)
                        return False
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        if "error" in event:
                            if on_progress:
                                on_progress(f"pull failed: {event['error']}", None)
                            return False
                        status = event.get("status", "")
                        total = event.get("total")
                        completed = event.get("completed")
                        pct = None
                        if total and completed is not None:
                            try:
                                pct = 100.0 * float(completed) / float(total)
                            except (TypeError, ValueError, ZeroDivisionError):
                                pct = None
                            # Human-readable byte counts
                            gb = 1024 ** 3
                            status = (
                                f"{status}: {completed / gb:.2f} GB / "
                                f"{total / gb:.2f} GB"
                            )
                        if on_progress:
                            on_progress(status, pct)
                        if status.startswith("success"):
                            return True
            return True
        except Exception as e:
            if on_progress:
                on_progress(f"pull error: {e}", None)
            return False


def start_ollama_server() -> bool:
    """Start ``ollama serve`` as a detached background process.

    Cross-platform: looks for the ``ollama`` binary on PATH and
    launches it without waiting. On Windows this spawns a hidden
    console window; on macOS/Linux the process is daemonized via
    the standard subprocess.Popen mechanics.

    Returns True if the binary was found and launched, False if
    Ollama isn't installed. Does NOT verify that the server actually
    came up — callers should poll ``get_version()`` after a short
    delay.
    """
    import shutil
    import subprocess
    import sys

    binary = shutil.which("ollama")
    if not binary:
        return False

    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW + DETACHED_PROCESS so we don't spawn a
            # visible console and the server survives the wizard exit.
            CREATE_NO_WINDOW = 0x08000000
            DETACHED_PROCESS = 0x00000008
            kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([binary, "serve"], **kwargs)
        return True
    except Exception:
        return False


def ollama_binary_installed() -> bool:
    """Return True if the ``ollama`` CLI is on PATH."""
    import shutil
    return shutil.which("ollama") is not None

    # ------------------------------------------------------------------
    # Text completion
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        start = time.monotonic()
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt

        resp = await self._client.post(
            f"{self.base_url}/api/generate", json=payload
        )
        resp.raise_for_status()
        data = resp.json()

        elapsed = (time.monotonic() - start) * 1000
        return LLMResponse(
            text=data.get("response", ""),
            model=self.model,
            tokens_used=data.get("eval_count", 0),
            cached=False,
            latency_ms=elapsed,
        )

    # ------------------------------------------------------------------
    # JSON completion
    # ------------------------------------------------------------------

    async def complete_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.1,
    ) -> dict:
        start = time.monotonic()
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt or "Respond with valid JSON only.",
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
        }

        resp = await self._client.post(
            f"{self.base_url}/api/generate", json=payload
        )
        resp.raise_for_status()
        data = resp.json()

        text = data.get("response", "{}")
        return self._parse_json(text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Best-effort parse of a JSON string, with brace extraction fallback."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try to extract the outermost { ... } block
        start_idx = text.find("{")
        end_idx = text.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            try:
                return json.loads(text[start_idx:end_idx])
            except json.JSONDecodeError:
                pass
        return {}
