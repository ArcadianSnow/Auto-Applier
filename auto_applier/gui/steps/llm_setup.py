"""Step 6: LLM configuration -- Ollama and Gemini setup."""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, ACCENT, DANGER, TEXT, TEXT_LIGHT, TEXT_MUTED,
    BORDER, STATUS_SUCCESS, STATUS_ERROR,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
    PAD_X, PAD_Y, make_scrollable,
)


from auto_applier.llm.ollama_backend import version_gte as _version_gte


class LLMSetupStep(ttk.Frame):
    """LLM backend configuration with connection testing."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._ollama_available = False
        self._gemini_available = False
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="AI Setup", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Configure which AI backends to use for scoring and form filling.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Scrollable area
        scroll_container = ttk.Frame(self)
        scroll_container.pack(fill="both", expand=True, padx=PAD_X, pady=(0, PAD_Y))
        _canvas, inner = make_scrollable(scroll_container)

        # --- Ollama card ---
        ollama_card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        ollama_card.pack(fill="x", padx=4, pady=(4, 8))

        header_row = tk.Frame(ollama_card, bg=BG_CARD)
        header_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            header_row, text="Ollama (Local AI)", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(side="left")

        self._ollama_dot = tk.Canvas(
            header_row, width=12, height=12, bg=BG_CARD,
            highlightthickness=0, bd=0,
        )
        self._ollama_dot.pack(side="left", padx=(8, 0), pady=2)
        self._draw_dot(self._ollama_dot, TEXT_MUTED)

        self._ollama_status_label = tk.Label(
            header_row, text="Not tested", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD,
        )
        self._ollama_status_label.pack(side="left", padx=(4, 0))

        # Model entry
        model_row = tk.Frame(ollama_card, bg=BG_CARD)
        model_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            model_row, text="Model:", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(side="left")

        from auto_applier.config import OLLAMA_MODEL_PRESETS
        ttk.Combobox(
            model_row, textvariable=self.wizard.data["ollama_model"],
            values=OLLAMA_MODEL_PRESETS, font=FONT_MONO, width=28,
        ).pack(side="left", padx=(8, 12))

        # Three-button action row: Check, Start server, Install model
        actions_row = tk.Frame(ollama_card, bg=BG_CARD)
        actions_row.pack(fill="x", pady=(4, 8))

        ttk.Button(
            actions_row, text="Check Status",
            command=self._test_ollama,
        ).pack(side="left", padx=(0, 6))

        self._start_server_btn = ttk.Button(
            actions_row, text="Start Server",
            command=self._start_server,
        )
        self._start_server_btn.pack(side="left", padx=(0, 6))

        self._install_model_btn = ttk.Button(
            actions_row, text="Install Model",
            command=self._install_model,
        )
        self._install_model_btn.pack(side="left")

        # Live progress line (used during pull + server startup)
        self._ollama_progress = tk.Label(
            ollama_card, text="", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD, anchor="w", justify="left",
            wraplength=560,
        )
        self._ollama_progress.pack(fill="x", pady=(2, 6))

        tk.Label(
            ollama_card,
            text=(
                "New here?  1) Install Ollama from ollama.com  "
                "2) Click Start Server  3) Click Install Model"
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")

        tk.Label(
            ollama_card,
            text=(
                "Model presets:  e2b (small, CPU-friendly)  •  "
                "e4b (default, 16 GB RAM)  •  31b (dev machines)"
            ),
            font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
        ).pack(anchor="w", pady=(2, 0))

        # --- Gemini card ---
        gemini_card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        gemini_card.pack(fill="x", padx=4, pady=(0, 8))

        g_header = tk.Frame(gemini_card, bg=BG_CARD)
        g_header.pack(fill="x", pady=(0, 8))

        tk.Label(
            g_header, text="Gemini (Cloud Fallback)", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(side="left")

        self._gemini_dot = tk.Canvas(
            g_header, width=12, height=12, bg=BG_CARD,
            highlightthickness=0, bd=0,
        )
        self._gemini_dot.pack(side="left", padx=(8, 0), pady=2)
        self._draw_dot(self._gemini_dot, TEXT_MUTED)

        self._gemini_status_label = tk.Label(
            g_header, text="Not tested", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD,
        )
        self._gemini_status_label.pack(side="left", padx=(4, 0))

        # API key entry
        key_row = tk.Frame(gemini_card, bg=BG_CARD)
        key_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            key_row, text="API Key:", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(side="left")

        ttk.Entry(
            key_row, textvariable=self.wizard.data["gemini_api_key"],
            font=FONT_MONO, width=40, show="*",
        ).pack(side="left", padx=(8, 12))

        ttk.Button(
            key_row, text="Test API Key",
            command=self._test_gemini,
        ).pack(side="left")

        tk.Label(
            gemini_card,
            text="Free at ai.google.dev -- 1,000 requests/day, no credit card required.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")

        # --- Status summary card ---
        summary_card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        summary_card.pack(fill="x", padx=4, pady=(0, 4))

        tk.Label(
            summary_card, text="Routing Order", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 8))

        tk.Label(
            summary_card,
            text=(
                "1.  Ollama (local, free, fastest)\n"
                "2.  Gemini (cloud, free tier, fallback)\n"
                "3.  Rule-based matching (no AI needed, always available)"
            ),
            font=FONT_BODY, fg=TEXT, bg=BG_CARD,
            justify="left",
        ).pack(anchor="w")

        self._summary_label = tk.Label(
            summary_card, text="", font=FONT_SMALL,
            fg=TEXT_LIGHT, bg=BG_CARD,
        )
        self._summary_label.pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------------
    # Status dot helper
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_dot(canvas: tk.Canvas, color: str) -> None:
        """Draw a small colored circle on a canvas."""
        canvas.delete("all")
        canvas.create_oval(1, 1, 11, 11, fill=color, outline=color)

    # ------------------------------------------------------------------
    # Ollama test
    # ------------------------------------------------------------------

    def _test_ollama(self) -> None:
        """Check Ollama connection + model availability in a background thread."""
        self._ollama_status_label.configure(text="Checking...", fg=TEXT_MUTED)
        self._draw_dot(self._ollama_dot, TEXT_MUTED)
        self._ollama_progress.configure(text="")

        def check():
            try:
                result = asyncio.run(_check())
            except Exception as e:
                result = {"error": str(e)}
            self.after(0, lambda: self._apply_status(result))

        async def _check():
            from auto_applier.llm.ollama_backend import (
                OllamaBackend, ollama_binary_installed,
            )
            from auto_applier.config import OLLAMA_MIN_VERSION
            model = self.wizard.data["ollama_model"].get()
            backend = OllamaBackend(model=model)
            version = await backend.get_version()
            if not version:
                return {
                    "binary_installed": ollama_binary_installed(),
                    "server_running": False,
                    "model_pulled": False,
                    "version": "",
                    "version_ok": False,
                }
            version_ok = _version_gte(version, OLLAMA_MIN_VERSION)
            model_pulled = await backend.is_available()
            return {
                "binary_installed": True,
                "server_running": True,
                "model_pulled": model_pulled,
                "version": version,
                "version_ok": version_ok,
            }

        threading.Thread(target=check, daemon=True).start()

    def _apply_status(self, result: dict) -> None:
        """Translate the check result into UI state + action hints."""
        from auto_applier.config import OLLAMA_MIN_VERSION

        if "error" in result:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, STATUS_ERROR)
            self._ollama_status_label.configure(
                text="Check failed", fg=STATUS_ERROR,
            )
            self._ollama_progress.configure(
                text=f"Error: {result['error']}", fg=DANGER,
            )
            self._update_summary()
            return

        binary = result.get("binary_installed")
        server = result.get("server_running")
        pulled = result.get("model_pulled")
        version = result.get("version", "")
        version_ok = result.get("version_ok")
        model = self.wizard.data["ollama_model"].get() or "gemma4:e4b"

        if not binary:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, STATUS_ERROR)
            self._ollama_status_label.configure(
                text="Ollama not installed", fg=STATUS_ERROR,
            )
            self._ollama_progress.configure(
                text="Download and install Ollama from ollama.com, then click Check Status again.",
                fg=TEXT_LIGHT,
            )
        elif not server:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, DANGER)
            self._ollama_status_label.configure(
                text="Server not running", fg=DANGER,
            )
            self._ollama_progress.configure(
                text="Ollama is installed but the server isn't responding. Click Start Server.",
                fg=TEXT_LIGHT,
            )
        elif not version_ok:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, STATUS_ERROR)
            self._ollama_status_label.configure(
                text=f"Upgrade Ollama (v{version} < {OLLAMA_MIN_VERSION})",
                fg=STATUS_ERROR,
            )
            self._ollama_progress.configure(
                text=f"Your Ollama is v{version}. Gemma 4 needs v{OLLAMA_MIN_VERSION}+. Reinstall from ollama.com.",
                fg=TEXT_LIGHT,
            )
        elif not pulled:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, DANGER)
            self._ollama_status_label.configure(
                text=f"Model '{model}' not installed", fg=DANGER,
            )
            self._ollama_progress.configure(
                text=f"Server v{version} is running but {model} hasn't been downloaded. Click Install Model.",
                fg=TEXT_LIGHT,
            )
        else:
            self._ollama_available = True
            self._draw_dot(self._ollama_dot, STATUS_SUCCESS)
            self._ollama_status_label.configure(
                text=f"Ready (v{version}, {model})", fg=STATUS_SUCCESS,
            )
            self._ollama_progress.configure(
                text="Everything's wired up. You can move on when you're ready.",
                fg=STATUS_SUCCESS,
            )

        self._update_summary()

    # ------------------------------------------------------------------
    # Start server button
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        """Launch 'ollama serve' in the background and poll for readiness."""
        from auto_applier.llm.ollama_backend import (
            ollama_binary_installed, start_ollama_server,
        )

        if not ollama_binary_installed():
            self._ollama_progress.configure(
                text="Ollama isn't installed. Get it from ollama.com first.",
                fg=DANGER,
            )
            return

        self._ollama_progress.configure(
            text="Starting Ollama server...", fg=TEXT_LIGHT,
        )
        self._start_server_btn.configure(state="disabled")

        def worker():
            launched = start_ollama_server()
            if not launched:
                self.after(0, lambda: self._on_server_start_failed())
                return
            # Poll for readiness for up to 30 seconds
            import time as _time
            for _ in range(30):
                _time.sleep(1)
                try:
                    version = asyncio.run(_get_version())
                except Exception:
                    version = ""
                if version:
                    self.after(0, lambda v=version: self._on_server_ready(v))
                    return
            self.after(0, lambda: self._on_server_start_failed())

        async def _get_version():
            from auto_applier.llm.ollama_backend import OllamaBackend
            return await OllamaBackend().get_version()

        threading.Thread(target=worker, daemon=True).start()

    def _on_server_ready(self, version: str) -> None:
        self._start_server_btn.configure(state="normal")
        self._ollama_progress.configure(
            text=f"Server started (v{version}). Click Check Status or Install Model next.",
            fg=STATUS_SUCCESS,
        )
        self._test_ollama()

    def _on_server_start_failed(self) -> None:
        self._start_server_btn.configure(state="normal")
        self._ollama_progress.configure(
            text=(
                "Couldn't start the server automatically. Open a terminal "
                "and run 'ollama serve' yourself, then click Check Status."
            ),
            fg=DANGER,
        )

    # ------------------------------------------------------------------
    # Install model button
    # ------------------------------------------------------------------

    def _install_model(self) -> None:
        """Download the target model via the Ollama pull API with live progress."""
        model = self.wizard.data["ollama_model"].get().strip() or "gemma4:e4b"

        self._install_model_btn.configure(state="disabled")
        self._ollama_progress.configure(
            text=f"Starting download of {model}... this is ~10 GB for gemma4:e4b.",
            fg=TEXT_LIGHT,
        )

        def worker():
            try:
                ok = asyncio.run(_pull())
            except Exception as e:
                self.after(0, lambda: self._on_pull_done(False, str(e)))
                return
            self.after(0, lambda: self._on_pull_done(ok, ""))

        async def _pull():
            from auto_applier.llm.ollama_backend import OllamaBackend
            backend = OllamaBackend(model=model)
            # First ensure the server is even reachable
            if not await backend.get_version():
                return False
            return await backend.pull_model(on_progress=_progress)

        def _progress(status: str, pct):
            label = status if pct is None else f"{status} ({pct:.1f}%)"
            self.after(0, lambda l=label: self._ollama_progress.configure(
                text=l, fg=TEXT_LIGHT,
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_pull_done(self, ok: bool, err: str) -> None:
        self._install_model_btn.configure(state="normal")
        if ok:
            self._ollama_progress.configure(
                text="Model installed.", fg=STATUS_SUCCESS,
            )
            self._test_ollama()
        else:
            msg = err or (
                "Download failed. Make sure the server is running "
                "(click Start Server) and try again."
            )
            self._ollama_progress.configure(text=msg, fg=DANGER)

    # ------------------------------------------------------------------
    # Gemini test
    # ------------------------------------------------------------------

    def _test_gemini(self) -> None:
        """Test Gemini API key in a background thread."""
        self._gemini_status_label.configure(text="Testing...", fg=TEXT_MUTED)
        self._draw_dot(self._gemini_dot, TEXT_MUTED)

        def check():
            try:
                result = asyncio.run(_check())
            except Exception:
                result = False
            self.after(0, lambda: self._update_gemini_status(result))

        async def _check():
            from auto_applier.llm.gemini_backend import GeminiBackend
            backend = GeminiBackend(
                api_key=self.wizard.data["gemini_api_key"].get()
            )
            return await backend.is_available()

        threading.Thread(target=check, daemon=True).start()

    def _update_gemini_status(self, available: bool) -> None:
        """Update Gemini status UI after test completes."""
        self._gemini_available = available
        if available:
            self._draw_dot(self._gemini_dot, STATUS_SUCCESS)
            self._gemini_status_label.configure(
                text="API key valid", fg=STATUS_SUCCESS,
            )
        else:
            self._draw_dot(self._gemini_dot, STATUS_ERROR)
            self._gemini_status_label.configure(
                text="Invalid or no key", fg=STATUS_ERROR,
            )
        self._update_summary()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _update_summary(self) -> None:
        """Update the routing summary label."""
        parts = []
        if self._ollama_available:
            parts.append("Ollama")
        if self._gemini_available:
            parts.append("Gemini")
        parts.append("Rule-based")

        self._summary_label.configure(
            text=f"Active backends: {' -> '.join(parts)}"
        )
