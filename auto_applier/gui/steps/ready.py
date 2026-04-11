"""Step 8: Summary + inline preflight + launch."""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, DANGER, TEXT, TEXT_LIGHT, TEXT_MUTED,
    BORDER, STATUS_SUCCESS, STATUS_ERROR,
    FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)


class ReadyStep(ttk.Frame):
    """Final step: configuration summary, inline doctor check, and launch buttons."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._preflight_card: tk.Widget | None = None
        self._preflight_rows: list[tk.Widget] = []
        self._run_button: ttk.Button | None = None
        self._has_fails = False
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

        self._run_button = ttk.Button(
            btn_frame, text="Run", style="Primary.TButton",
            command=lambda: self._launch(dry_run=False),
        )
        self._run_button.pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="Dry Run", style="Accent.TButton",
            command=lambda: self._launch(dry_run=True),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="Recheck",
            command=self._recheck,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="Exit",
            command=self.wizard.destroy,
        ).pack(side="right")

    def _recheck(self) -> None:
        """Re-run the preflight checks without rebuilding the summary."""
        for w in self._preflight_rows:
            w.destroy()
        self._preflight_rows.clear()
        self._preflight_status_label.configure(
            text="Running preflight checks...", fg=TEXT_LIGHT,
        )
        self._start_preflight()

    def on_show(self) -> None:
        """Rebuild the summary when this step is shown."""
        # Clear old summary widgets
        for w in self._inner.winfo_children():
            w.destroy()
        self._render_summary()
        self._start_preflight()

    def _render_summary(self) -> None:
        """Build the configuration summary cards."""
        # Preflight card goes first so users see it before scrolling.
        self._build_preflight_card()

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
    # Preflight (inline doctor)
    # ------------------------------------------------------------------

    def _build_preflight_card(self) -> None:
        """Render the System Check card with a 'Running...' placeholder."""
        self._preflight_card = tk.Frame(
            self._inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=16, pady=12,
        )
        self._preflight_card.pack(fill="x", padx=4, pady=4)

        tk.Label(
            self._preflight_card, text="System Check", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 6))

        self._preflight_status_label = tk.Label(
            self._preflight_card,
            text="Running preflight checks...",
            font=FONT_BODY, fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left",
        )
        self._preflight_status_label.pack(anchor="w", pady=(0, 4))

        self._preflight_results_frame = tk.Frame(self._preflight_card, bg=BG_CARD)
        self._preflight_results_frame.pack(fill="x")

        self._preflight_rows = []

    def _start_preflight(self) -> None:
        """Kick off a doctor run in a background thread."""
        def worker():
            try:
                from auto_applier import doctor as doctor_module
                results = asyncio.run(doctor_module._run_all())
            except Exception as e:
                self.after(0, lambda err=e: self._on_preflight_error(err))
                return
            self.after(0, lambda r=results: self._on_preflight_done(r))

        threading.Thread(target=worker, daemon=True).start()

    def _on_preflight_error(self, err: Exception) -> None:
        self._preflight_status_label.configure(
            text=f"Preflight crashed: {err}", fg=DANGER,
        )

    def _on_preflight_done(self, results: list) -> None:
        """Render the doctor results as a compact list."""
        from auto_applier import doctor as doctor_module

        # Clear any stale rows
        for w in self._preflight_rows:
            w.destroy()
        self._preflight_rows.clear()

        passes = sum(1 for r in results if r.status == doctor_module.PASS)
        warns = sum(1 for r in results if r.status == doctor_module.WARN)
        fails = sum(1 for r in results if r.status == doctor_module.FAIL)
        self._has_fails = fails > 0

        if fails:
            summary = f"{passes} pass · {warns} warn · {fails} FAIL"
            summary_color = DANGER
        elif warns:
            summary = f"{passes} pass · {warns} warn · ready (warnings are optional)"
            summary_color = TEXT_LIGHT
        else:
            summary = f"{passes} pass · everything ready"
            summary_color = STATUS_SUCCESS
        self._preflight_status_label.configure(text=summary, fg=summary_color)

        # Render one row per check
        for r in results:
            row = tk.Frame(self._preflight_results_frame, bg=BG_CARD)
            row.pack(fill="x", pady=1)
            self._preflight_rows.append(row)

            color_map = {
                doctor_module.PASS: STATUS_SUCCESS,
                doctor_module.WARN: TEXT_LIGHT,
                doctor_module.FAIL: STATUS_ERROR,
            }
            icon_map = {
                doctor_module.PASS: "OK",
                doctor_module.WARN: "!",
                doctor_module.FAIL: "X",
            }
            color = color_map[r.status]
            icon = icon_map[r.status]

            tk.Label(
                row, text=f"  {icon:>3s}  ", font=FONT_BODY,
                fg=color, bg=BG_CARD, width=6, anchor="w",
            ).pack(side="left")

            tk.Label(
                row, text=f"{r.name}", font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, width=22, anchor="w",
            ).pack(side="left")

            tk.Label(
                row, text=r.message, font=FONT_SMALL,
                fg=TEXT_LIGHT, bg=BG_CARD, anchor="w",
            ).pack(side="left")

            if r.fix and r.status != doctor_module.PASS:
                fix_row = tk.Frame(self._preflight_results_frame, bg=BG_CARD)
                fix_row.pack(fill="x", pady=(0, 2))
                self._preflight_rows.append(fix_row)
                tk.Label(
                    fix_row, text=f"            fix: {r.fix}",
                    font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
                    anchor="w", wraplength=540, justify="left",
                ).pack(anchor="w")

        # Update Run button state based on FAIL count
        if self._run_button is not None:
            state = "disabled" if self._has_fails else "normal"
            self._run_button.configure(state=state)

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
