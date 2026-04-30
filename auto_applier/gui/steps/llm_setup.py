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
        self._auto_checked = False
        self._build()

    def on_show(self) -> None:
        """Auto-run the Ollama check the first time this step is shown.

        If Ollama is already installed and running (common if the user
        installed it during a previous wizard run or via the tray app),
        they get an immediate green status without having to click
        anything.
        """
        if self._auto_checked:
            return
        self._auto_checked = True
        self._test_ollama()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Set up the AI helper", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text=(
                "Auto Applier uses a free AI program called Ollama to read job "
                "postings and fill out applications. Follow the numbered steps "
                "below — it's a one-time setup."
            ),
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
            header_row, text="Local AI (Ollama)", font=FONT_SUBHEADING,
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

        # --- Step 1: Download Ollama ---
        self._step_row(
            ollama_card, "1.",
            "Download and install Ollama from ollama.com",
            "This is the free AI program that powers Auto Applier. "
            "Clicking the button opens your web browser.",
            "Open ollama.com",
            self._open_ollama_website,
        )

        # --- Step 2: Turn it on ---
        self._start_server_btn = self._step_row(
            ollama_card, "2.",
            "Turn on the AI",
            "After you finish installing Ollama, click here to start it "
            "running in the background. You won't see a window — that's "
            "normal.",
            "Turn on AI",
            self._start_server,
        )

        # --- Step 3: Download the AI brain ---
        # Model picker (hidden unless advanced — most people never touch this)
        from auto_applier.config import OLLAMA_MODEL_PRESETS
        self._install_model_btn = self._step_row(
            ollama_card, "3.",
            "Download the AI brain (about 10 GB, one time only)",
            "This downloads the AI model file so everything runs on your "
            "own computer. It can take 15–30 minutes depending on your "
            "internet speed — you'll see a progress bar below.",
            "Download AI brain",
            self._install_model,
        )

        # Advanced model picker folded into a small row beneath Step 3
        adv_row = tk.Frame(ollama_card, bg=BG_CARD)
        adv_row.pack(fill="x", pady=(0, 8), padx=(24, 0))
        tk.Label(
            adv_row, text="Advanced — which AI model:", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD,
        ).pack(side="left")
        ttk.Combobox(
            adv_row, textvariable=self.wizard.data["ollama_model"],
            values=OLLAMA_MODEL_PRESETS, font=FONT_MONO, width=22,
        ).pack(side="left", padx=(8, 0))
        from auto_applier.gui.tooltip import attach_help_icon
        attach_help_icon(adv_row, (
            "Most people should leave this as 'gemma4:e4b' (the default). "
            "Pick 'gemma4:e2b' if your computer has less than 16 GB of "
            "memory, or 'gemma4:31b' if you have a powerful gaming PC. "
            "The tool works fine with any of these — the larger ones are "
            "just a bit smarter at reading job descriptions."
        ), bg=BG_CARD).pack(side="left", padx=(6, 0))

        # --- Step 4: Verify ---
        self._step_row(
            ollama_card, "4.",
            "Make sure everything works",
            "Click here to test your setup. A green dot next to 'Local AI "
            "(Ollama)' up top means you're done.",
            "Check it's working",
            self._test_ollama,
        )

        # Live progress / status line
        self._ollama_progress = tk.Label(
            ollama_card, text="", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD, anchor="w", justify="left",
            wraplength=560,
        )
        self._ollama_progress.pack(fill="x", pady=(6, 0))

        # --- Gemini card ---
        gemini_card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        gemini_card.pack(fill="x", padx=4, pady=(0, 8))

        g_header = tk.Frame(gemini_card, bg=BG_CARD)
        g_header.pack(fill="x", pady=(0, 8))

        tk.Label(
            g_header, text="Backup AI — optional", font=FONT_SUBHEADING,
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

        # Step 1 — get a key
        self._step_row(
            gemini_card, "1.",
            "Get a free Gemini API key (recommended, takes ~60 seconds)",
            "Click the button to open Google AI Studio in your browser. "
            "Sign in with any Google account, click 'Create API key' in "
            "the top right, then 'Create API key in new project' if it "
            "asks. Copy the long string that starts with 'AIzaSy...' and "
            "paste it below. No credit card needed — the free tier is "
            "1,500 requests per day.",
            "Open Google AI Studio",
            self._open_gemini_keypage,
        )

        # Step 2 — paste it in
        paste_label = tk.Frame(gemini_card, bg=BG_CARD)
        paste_label.pack(fill="x", pady=(8, 4))
        tk.Label(
            paste_label, text="2.", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD, width=3, anchor="w",
        ).pack(side="left")
        tk.Label(
            paste_label, text="Paste your key here, then click 'Test API Key'",
            font=FONT_BODY, fg=TEXT, bg=BG_CARD, anchor="w",
        ).pack(side="left")

        # API key entry (indented to match step layout)
        key_row = tk.Frame(gemini_card, bg=BG_CARD)
        key_row.pack(fill="x", pady=(2, 8), padx=(24, 0))

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
            text=(
                "You can skip this whole section if you'd rather. Gemini "
                "is a backup the app falls back to when Ollama has trouble "
                "with a tricky question. Without it, the app still works — "
                "those tricky questions just get logged as 'skill gaps' "
                "for you to answer later."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            justify="left", wraplength=560,
        ).pack(anchor="w", pady=(4, 0))

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
    # Numbered-step row helper
    # ------------------------------------------------------------------

    def _step_row(
        self,
        parent: tk.Widget,
        number: str,
        title: str,
        description: str,
        button_label: str,
        command,
    ) -> ttk.Button:
        """Render one numbered step: title, description, and action button.

        Returns the created button so callers can enable/disable it.
        """
        container = tk.Frame(parent, bg=BG_CARD)
        container.pack(fill="x", pady=(4, 2))

        # Number + title row
        head = tk.Frame(container, bg=BG_CARD)
        head.pack(fill="x")
        tk.Label(
            head, text=number, font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD, width=3, anchor="w",
        ).pack(side="left")
        tk.Label(
            head, text=title, font=FONT_BODY,
            fg=TEXT, bg=BG_CARD, anchor="w",
        ).pack(side="left")

        # Description (indented)
        tk.Label(
            container, text=description, font=FONT_SMALL,
            fg=TEXT_LIGHT, bg=BG_CARD, anchor="w", justify="left",
            wraplength=520,
        ).pack(anchor="w", padx=(24, 0))

        # Button (indented)
        btn = ttk.Button(container, text=button_label, command=command)
        btn.pack(anchor="w", padx=(24, 0), pady=(4, 0))
        return btn

    # ------------------------------------------------------------------
    # Open Ollama website
    # ------------------------------------------------------------------

    def _open_ollama_website(self) -> None:
        """Launch the default browser at the Ollama download page."""
        import webbrowser
        webbrowser.open("https://ollama.com/download", new=2)
        self._ollama_progress.configure(
            text=(
                "Opened ollama.com in your browser. Finish the install, "
                "then come back here and click 'Turn on AI'."
            ),
            fg=TEXT_LIGHT,
        )

    # ------------------------------------------------------------------
    # Open Gemini API key page
    # ------------------------------------------------------------------

    def _open_gemini_keypage(self) -> None:
        """Launch the default browser at Google AI Studio's key page."""
        import webbrowser
        webbrowser.open("https://aistudio.google.com/apikey", new=2)

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
                text="Ollama isn't installed yet", fg=STATUS_ERROR,
            )
            self._ollama_progress.configure(
                text=(
                    "Do Step 1: click 'Open ollama.com' above, download "
                    "Ollama, run the installer, then come back here."
                ),
                fg=TEXT_LIGHT,
            )
        elif not server:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, DANGER)
            self._ollama_status_label.configure(
                text="AI is installed but not turned on", fg=DANGER,
            )
            self._ollama_progress.configure(
                text="Do Step 2: click 'Turn on AI' above.",
                fg=TEXT_LIGHT,
            )
        elif not version_ok:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, STATUS_ERROR)
            self._ollama_status_label.configure(
                text="Ollama is out of date — please update it",
                fg=STATUS_ERROR,
            )
            self._ollama_progress.configure(
                text=(
                    f"Your Ollama version is {version}, but Auto Applier "
                    f"needs {OLLAMA_MIN_VERSION} or newer. Click "
                    f"'Open ollama.com' above to download the latest version."
                ),
                fg=TEXT_LIGHT,
            )
        elif not pulled:
            self._ollama_available = False
            self._draw_dot(self._ollama_dot, DANGER)
            self._ollama_status_label.configure(
                text="AI brain hasn't been downloaded yet", fg=DANGER,
            )
            self._ollama_progress.configure(
                text="Do Step 3: click 'Download AI brain' above.",
                fg=TEXT_LIGHT,
            )
        else:
            self._ollama_available = True
            self._draw_dot(self._ollama_dot, STATUS_SUCCESS)
            self._ollama_status_label.configure(
                text="Ready to go", fg=STATUS_SUCCESS,
            )
            self._ollama_progress.configure(
                text=(
                    f"Everything works (Ollama {version}, AI model {model}). "
                    f"Click 'Next' at the bottom to continue."
                ),
                fg=STATUS_SUCCESS,
            )

        self._update_summary()

    # ------------------------------------------------------------------
    # Start server button
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        """Launch Ollama in the background and poll for readiness.

        First checks whether Ollama is already running — if it is, we
        don't need to launch anything, and we update the status
        immediately instead of trying to spawn a second server.
        """
        from auto_applier.llm.ollama_backend import (
            find_ollama_binary, start_ollama_server,
        )

        self._ollama_progress.configure(
            text="Checking if the AI is already on...",
            fg=TEXT_LIGHT,
        )
        self._start_server_btn.configure(state="disabled")

        def worker():
            # 1. If Ollama is already running, we're done — just
            #    refresh the status display and exit.
            try:
                version = asyncio.run(_get_version())
            except Exception:
                version = ""
            if version:
                self.after(0, lambda v=version: self._on_server_already_running(v))
                return

            # 2. Not running — try to find and launch it.
            if find_ollama_binary() is None:
                self.after(0, self._on_binary_missing)
                return

            launched, detail = start_ollama_server()
            if not launched:
                err = detail
                self.after(
                    0, lambda msg=err: self._on_server_start_failed(msg),
                )
                return

            # 3. Poll for readiness for up to 30 seconds.
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
            self.after(0, lambda: self._on_server_start_failed(
                "The AI didn't come up within 30 seconds. Try clicking "
                "'Turn on AI' again, or open the Ollama app from your "
                "Start menu manually."
            ))

        async def _get_version():
            from auto_applier.llm.ollama_backend import OllamaBackend
            return await OllamaBackend().get_version()

        threading.Thread(target=worker, daemon=True).start()

    def _on_server_already_running(self, version: str) -> None:
        self._start_server_btn.configure(state="normal")
        self._ollama_progress.configure(
            text=(
                f"The AI is already on (Ollama {version}). Moving on — "
                f"do Step 3 if you haven't downloaded the AI brain yet, "
                f"otherwise click 'Check it's working' below."
            ),
            fg=STATUS_SUCCESS,
        )
        self._test_ollama()

    def _on_binary_missing(self) -> None:
        self._start_server_btn.configure(state="normal")
        self._ollama_progress.configure(
            text=(
                "Ollama isn't installed on this computer. Do Step 1 — "
                "click 'Open ollama.com' up top to download it."
            ),
            fg=DANGER,
        )

    def _on_server_ready(self, version: str) -> None:
        self._start_server_btn.configure(state="normal")
        self._ollama_progress.configure(
            text=(
                f"AI turned on successfully (Ollama {version}). Next, do "
                f"Step 3 to download the AI brain."
            ),
            fg=STATUS_SUCCESS,
        )
        self._test_ollama()

    def _on_server_start_failed(self, detail: str = "") -> None:
        self._start_server_btn.configure(state="normal")
        base = (
            "Couldn't turn on the AI automatically. Try opening "
            "'Ollama' from your Start menu yourself, then click 'Check "
            "it's working' below. If that doesn't help, restart your "
            "computer and try once more."
        )
        if detail:
            base += f"\n\nDetails: {detail}"
        self._ollama_progress.configure(text=base, fg=DANGER)

    # ------------------------------------------------------------------
    # Install model button
    # ------------------------------------------------------------------

    def _install_model(self) -> None:
        """Download the target model via the Ollama pull API with live progress."""
        model = self.wizard.data["ollama_model"].get().strip() or "gemma4:e4b"

        self._install_model_btn.configure(state="disabled")
        self._ollama_progress.configure(
            text=(
                f"Downloading the AI brain ({model})... This is about "
                f"10 GB and can take 15–30 minutes. Don't close this "
                f"window. You'll see live progress below."
            ),
            fg=TEXT_LIGHT,
        )

        def worker():
            try:
                ok = asyncio.run(_pull())
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda msg=err_msg: self._on_pull_done(False, msg))
                return
            self.after(0, lambda ok=ok: self._on_pull_done(ok, ""))

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
                text="AI brain downloaded successfully.",
                fg=STATUS_SUCCESS,
            )
            self._test_ollama()
        else:
            msg = (
                "Download failed. Make sure the AI is turned on (Step 2) "
                "and your internet is working, then try again."
            )
            if err:
                msg += f"\n\nDetails: {err}"
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
