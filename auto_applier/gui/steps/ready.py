"""Step 8: Summary and launch."""
import asyncio
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from auto_applier.config import DATA_DIR
from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, ACCENT, DANGER, TEXT, TEXT_LIGHT, TEXT_MUTED,
    BORDER, STATUS_SUCCESS, STATUS_ERROR,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
    PAD_X, PAD_Y, make_scrollable,
)


class ReadyStep(ttk.Frame):
    """Final step: configuration summary and launch buttons."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Ready to Apply", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Review your configuration and launch.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Scrollable summary area
        scroll_container = ttk.Frame(self)
        scroll_container.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 8))
        _canvas, self._inner = make_scrollable(scroll_container)

        # Buttons at bottom (outside scroll)
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))

        ttk.Button(
            btn_frame, text="Run", style="Primary.TButton",
            command=lambda: self._launch(dry_run=False),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="Dry Run", style="Accent.TButton",
            command=lambda: self._launch(dry_run=True),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="Exit",
            command=self.wizard.destroy,
        ).pack(side="right")

    def on_show(self) -> None:
        """Rebuild the summary when this step is shown."""
        # Clear old summary widgets
        for w in self._inner.winfo_children():
            w.destroy()
        self._render_summary()

    def _render_summary(self) -> None:
        """Build the configuration summary cards."""
        config = self.wizard.get_config()

        # --- Platforms ---
        self._summary_card("Platforms", self._inner, [
            ("Enabled", ", ".join(p.title() for p in config["enabled_platforms"]) or "None"),
        ])

        # --- Resumes ---
        resumes = config.get("resumes", [])
        resume_lines = [
            ("Loaded", f"{len(resumes)} resume(s)"),
        ]
        for r in resumes:
            resume_lines.append(("", f"  {r['label']}"))
        self._summary_card("Resumes", self._inner, resume_lines)

        # --- Search ---
        keywords = ", ".join(config.get("search_keywords", []))
        self._summary_card("Search", self._inner, [
            ("Keywords", keywords or "None"),
            ("Location", config.get("location", "") or "Not set"),
        ])

        # --- Scoring ---
        scoring = config.get("scoring", {})
        self._summary_card("Scoring", self._inner, [
            ("Max apps/day", str(config.get("max_applications_per_day", 10))),
            ("Auto-apply threshold", str(scoring.get("auto_apply_min", 7))),
            ("Review minimum", str(scoring.get("review_min", 4))),
        ])

        # --- AI ---
        llm = config.get("llm", {})
        ollama_model = llm.get("ollama_model", "")
        gemini_key = llm.get("gemini_api_key", "")
        ai_status = []
        if ollama_model:
            ai_status.append(f"Ollama ({ollama_model})")
        if gemini_key:
            ai_status.append("Gemini (key set)")
        ai_status.append("Rule-based (always available)")

        self._summary_card("AI Backends", self._inner, [
            ("Routing", " -> ".join(ai_status)),
        ])

        # --- Personal ---
        p = config.get("personal_info", {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        self._summary_card("Personal Info", self._inner, [
            ("Name", name or "Not set"),
            ("Email", p.get("email", "") or "Not set"),
            ("Phone", p.get("phone", "") or "Not set"),
            ("City", p.get("city", "") or "Not set"),
        ])

    def _summary_card(
        self, title: str, parent: tk.Widget, rows: list[tuple[str, str]]
    ) -> None:
        """Render a summary card with key-value rows."""
        card = tk.Frame(
            parent, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=16, pady=12,
        )
        card.pack(fill="x", padx=4, pady=4)

        tk.Label(
            card, text=title, font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 6))

        for label, value in rows:
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill="x", pady=1)

            if label:
                tk.Label(
                    row, text=f"{label}:", font=FONT_BODY,
                    fg=TEXT_LIGHT, bg=BG_CARD, width=20, anchor="w",
                ).pack(side="left")

            tk.Label(
                row, text=value, font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, anchor="w",
            ).pack(side="left", fill="x")

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def _launch(self, dry_run: bool) -> None:
        """Save all config and open the dashboard."""
        # Validate resumes
        if not self.wizard.resume_list:
            messagebox.showwarning(
                "No Resumes",
                "Please go back and add at least one resume.",
                parent=self.wizard,
            )
            return

        # Save config
        try:
            self.wizard.save_config()
            self.wizard.save_answers()
        except Exception as e:
            messagebox.showerror(
                "Save Error",
                f"Failed to save configuration:\n{e}",
                parent=self.wizard,
            )
            return

        config = self.wizard.get_config()
        config["dry_run"] = dry_run

        # Open dashboard
        from auto_applier.gui.dashboard import DashboardWindow
        dashboard = DashboardWindow(self.wizard, config)
        dashboard.start_run(config, dry_run)
