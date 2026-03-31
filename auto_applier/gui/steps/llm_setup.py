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

        ttk.Entry(
            model_row, textvariable=self.wizard.data["ollama_model"],
            font=FONT_MONO, width=30,
        ).pack(side="left", padx=(8, 12))

        ttk.Button(
            model_row, text="Test Connection",
            command=self._test_ollama,
        ).pack(side="left")

        tk.Label(
            ollama_card,
            text="Install from ollama.com, then run:  ollama pull llama3.1:8b",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")

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
        """Test Ollama connection in a background thread."""
        self._ollama_status_label.configure(text="Testing...", fg=TEXT_MUTED)
        self._draw_dot(self._ollama_dot, TEXT_MUTED)

        def check():
            try:
                result = asyncio.run(_check())
            except Exception:
                result = False
            self.after(0, lambda: self._update_ollama_status(result))

        async def _check():
            from auto_applier.llm.ollama_backend import OllamaBackend
            backend = OllamaBackend(
                model=self.wizard.data["ollama_model"].get()
            )
            return await backend.is_available()

        threading.Thread(target=check, daemon=True).start()

    def _update_ollama_status(self, available: bool) -> None:
        """Update Ollama status UI after test completes."""
        self._ollama_available = available
        if available:
            self._draw_dot(self._ollama_dot, STATUS_SUCCESS)
            self._ollama_status_label.configure(
                text="Connected", fg=STATUS_SUCCESS,
            )
        else:
            self._draw_dot(self._ollama_dot, STATUS_ERROR)
            self._ollama_status_label.configure(
                text="Not available", fg=STATUS_ERROR,
            )
        self._update_summary()

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
