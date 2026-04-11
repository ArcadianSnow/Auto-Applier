"""Main wizard controller -- 8-step setup flow for Auto Applier v2."""
import json
import tkinter as tk
from tkinter import ttk, messagebox

from auto_applier.config import USER_CONFIG_FILE, DATA_DIR
from auto_applier.gui.styles import (
    apply_theme,
    BG,
    BG_CARD,
    PRIMARY,
    PRIMARY_LIGHT,
    TEXT,
    TEXT_LIGHT,
    TEXT_MUTED,
    BORDER,
    FONT_HEADING,
    FONT_BODY,
    FONT_SMALL,
    FONT_BUTTON,
    PAD_X,
    PAD_Y,
)


class WizardApp(tk.Tk):
    """Multi-step configuration wizard."""

    WINDOW_WIDTH = 980
    WINDOW_HEIGHT = 820

    def __init__(self) -> None:
        super().__init__()

        self.title("Auto Applier v2")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._center_window()

        apply_theme(self)

        # Shared state -- Variables accessible by all steps.
        # These MUST be created before _init_variables() because
        # _load_saved_config() (called at the end of _init_variables)
        # reads resume_list/answer_vars back from user_config.json.
        self.data: dict[str, tk.Variable] = {}
        self.resume_list: list[tuple[str, str]] = []
        self.answer_vars: dict[str, tk.StringVar] = {}

        self._init_variables()

        # Build UI structure
        self._build_header()
        self._build_content()
        self._build_footer()

        # Import and register steps (lazy to avoid circular imports)
        from auto_applier.gui.steps.welcome import WelcomeStep
        from auto_applier.gui.steps.sites import SitesStep
        from auto_applier.gui.steps.resumes import ResumesStep
        from auto_applier.gui.steps.personal import PersonalStep
        from auto_applier.gui.steps.preferences import PreferencesStep
        from auto_applier.gui.steps.llm_setup import LLMSetupStep
        from auto_applier.gui.steps.answers import AnswersStep
        from auto_applier.gui.steps.ready import ReadyStep

        self.step_classes = [
            WelcomeStep,
            SitesStep,
            ResumesStep,
            PersonalStep,
            PreferencesStep,
            LLMSetupStep,
            AnswersStep,
            ReadyStep,
        ]
        self.step_labels = [
            "Welcome",
            "Platforms",
            "Resumes",
            "Personal",
            "Preferences",
            "AI Setup",
            "Answers",
            "Ready",
        ]

        self.steps: list[ttk.Frame] = []
        for cls in self.step_classes:
            step = cls(self.content_frame, self)
            step.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.steps.append(step)

        self.current_step = 0
        self._show_step(0)

    # ------------------------------------------------------------------
    # Variable initialization
    # ------------------------------------------------------------------

    def _init_variables(self) -> None:
        """Create all shared tk.Variable instances."""
        # Platform toggles. LinkedIn is opt-in by default because its
        # automation defenses are aggressive enough that starting with
        # it tends to block new users before they get a single real
        # application through. The other three work out of the box.
        platform_defaults = {
            "linkedin": False,
            "indeed": True,
            "dice": True,
            "ziprecruiter": True,
        }
        for key, default in platform_defaults.items():
            self.data[f"{key}_enabled"] = tk.BooleanVar(value=default)

        # Personal info
        for key in ("first_name", "last_name", "email", "phone", "city",
                     "linkedin_url", "website"):
            self.data[key] = tk.StringVar(value="")

        # Job preferences
        self.data["search_keywords"] = tk.StringVar(value="")
        self.data["location"] = tk.StringVar(value="")
        self.data["max_applications_per_day"] = tk.IntVar(value=10)
        self.data["auto_apply_min"] = tk.IntVar(value=7)
        self.data["cli_auto_apply_min"] = tk.IntVar(value=7)
        self.data["review_min"] = tk.IntVar(value=4)

        # LLM settings
        from auto_applier.config import OLLAMA_MODEL
        self.data["ollama_model"] = tk.StringVar(value=OLLAMA_MODEL)
        self.data["gemini_api_key"] = tk.StringVar(value="")

        # Load saved config if it exists
        self._load_saved_config()

    def _load_saved_config(self) -> None:
        """Pre-populate variables from existing user_config.json."""
        if not USER_CONFIG_FILE.exists():
            return
        try:
            with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        # Personal info
        personal = cfg.get("personal_info", {})
        for key in ("first_name", "last_name", "email", "phone", "city",
                     "linkedin_url", "website"):
            if key in personal and key in self.data:
                self.data[key].set(personal[key])

        # Platforms
        for plat in cfg.get("enabled_platforms", []):
            var_key = f"{plat}_enabled"
            if var_key in self.data:
                self.data[var_key].set(True)

        # Preferences
        kws = cfg.get("search_keywords", [])
        if kws:
            self.data["search_keywords"].set(", ".join(kws))
        if cfg.get("location"):
            self.data["location"].set(cfg["location"])
        if cfg.get("max_applications_per_day"):
            self.data["max_applications_per_day"].set(cfg["max_applications_per_day"])

        scoring = cfg.get("scoring", {})
        for key in ("auto_apply_min", "cli_auto_apply_min", "review_min"):
            if key in scoring:
                self.data[key].set(scoring[key])

        # LLM
        llm = cfg.get("llm", {})
        if llm.get("ollama_model"):
            self.data["ollama_model"].set(llm["ollama_model"])
        if llm.get("gemini_api_key"):
            self.data["gemini_api_key"].set(llm["gemini_api_key"])

        # Resumes — reload any previously added resumes so the
        # user doesn't have to re-label them on every wizard open.
        # Only include entries whose file still exists on disk.
        from pathlib import Path as _Path
        for entry in cfg.get("resumes", []):
            if not isinstance(entry, dict):
                continue
            label = entry.get("label", "").strip()
            path = entry.get("path", "").strip()
            if not label or not path:
                continue
            if not _Path(path).exists():
                continue
            self.resume_list.append((label, path))

    # ------------------------------------------------------------------
    # Window layout
    # ------------------------------------------------------------------

    def _center_window(self) -> None:
        """Center the window on screen."""
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.WINDOW_WIDTH) // 2
        y = (sh - self.WINDOW_HEIGHT) // 2
        self.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}")

    def _build_header(self) -> None:
        """Build the progress indicator header."""
        self.header = tk.Frame(self, bg=BG_CARD, height=70)
        self.header.pack(fill="x", side="top")
        self.header.pack_propagate(False)

        # Separator under header
        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x", side="top")

        # Progress dots container
        self.dots_frame = tk.Frame(self.header, bg=BG_CARD)
        self.dots_frame.place(relx=0.5, rely=0.5, anchor="center")

        self.dot_canvases: list[tk.Canvas] = []
        self.dot_labels: list[tk.Label] = []

    def _build_progress_dots(self) -> None:
        """Render numbered progress circles with connecting lines."""
        # Clear previous
        for w in self.dots_frame.winfo_children():
            w.destroy()
        self.dot_canvases.clear()
        self.dot_labels.clear()

        num = len(self.step_labels)
        for i, label in enumerate(self.step_labels):
            # Connecting line (before dot, except first)
            if i > 0:
                line = tk.Frame(self.dots_frame, bg=BORDER, height=2, width=30)
                line.pack(side="left", pady=(0, 14))
                # Store the line so we can color it
                line._step_index = i  # type: ignore[attr-defined]

            # Dot + label column
            col = tk.Frame(self.dots_frame, bg=BG_CARD)
            col.pack(side="left")

            canvas = tk.Canvas(
                col, width=28, height=28, bg=BG_CARD,
                highlightthickness=0, bd=0,
            )
            canvas.pack()
            self.dot_canvases.append(canvas)

            lbl = tk.Label(
                col, text=label, font=FONT_SMALL, bg=BG_CARD,
                fg=TEXT_MUTED,
            )
            lbl.pack(pady=(2, 0))
            self.dot_labels.append(lbl)

    def _update_dots(self) -> None:
        """Redraw progress dots to reflect the current step."""
        if not self.dot_canvases:
            self._build_progress_dots()

        for i, canvas in enumerate(self.dot_canvases):
            canvas.delete("all")
            if i < self.current_step:
                # Completed
                canvas.create_oval(2, 2, 26, 26, fill=PRIMARY, outline=PRIMARY)
                canvas.create_text(14, 14, text="\u2713", fill="white",
                                   font=("Segoe UI", 10, "bold"))
                self.dot_labels[i].configure(fg=PRIMARY)
            elif i == self.current_step:
                # Current
                canvas.create_oval(2, 2, 26, 26, fill=PRIMARY, outline=PRIMARY)
                canvas.create_text(14, 14, text=str(i + 1), fill="white",
                                   font=("Segoe UI", 10, "bold"))
                self.dot_labels[i].configure(fg=PRIMARY)
            else:
                # Future
                canvas.create_oval(2, 2, 26, 26, fill=BG_CARD, outline=BORDER,
                                   width=2)
                canvas.create_text(14, 14, text=str(i + 1), fill=TEXT_MUTED,
                                   font=("Segoe UI", 9))
                self.dot_labels[i].configure(fg=TEXT_MUTED)

        # Color connecting lines
        for widget in self.dots_frame.winfo_children():
            idx = getattr(widget, "_step_index", None)
            if idx is not None:
                color = PRIMARY if idx <= self.current_step else BORDER
                widget.configure(bg=color)

    def _build_content(self) -> None:
        """Build the main content area."""
        self.content_frame = ttk.Frame(self, style="TFrame")
        self.content_frame.pack(fill="both", expand=True, padx=0, pady=0)

    def _build_footer(self) -> None:
        """Build the navigation footer with Back/Next buttons."""
        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x", side="bottom", before=self.content_frame)

        self.footer = tk.Frame(self, bg=BG_CARD, height=60)
        self.footer.pack(fill="x", side="bottom", before=sep)
        self.footer.pack_propagate(False)

        inner = tk.Frame(self.footer, bg=BG_CARD)
        inner.place(relx=0.5, rely=0.5, anchor="center")

        self.btn_back = ttk.Button(
            inner, text="Back", command=self._on_back,
        )
        self.btn_back.pack(side="left", padx=(0, 12))

        self.btn_next = ttk.Button(
            inner, text="Next", style="Primary.TButton",
            command=self._on_next,
        )
        self.btn_next.pack(side="left")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_step(self, index: int) -> None:
        """Show the step at *index* and update navigation state."""
        self.current_step = index
        self.steps[index].tkraise()

        # Notify step it is being shown (for dynamic updates)
        step = self.steps[index]
        if hasattr(step, "on_show"):
            step.on_show()

        # Update dots
        self._update_dots()

        # Update button visibility
        if index == 0:
            self.btn_back.pack_forget()
        else:
            self.btn_back.pack(side="left", padx=(0, 12))

        # Last step has no Next (it has its own buttons)
        if index == len(self.steps) - 1:
            self.btn_next.pack_forget()
        else:
            self.btn_next.pack(side="left")
            self.btn_next.configure(text="Next")

    def _on_next(self) -> None:
        """Validate current step and advance."""
        step = self.steps[self.current_step]
        if hasattr(step, "validate"):
            if not step.validate():
                return

        if self.current_step < len(self.steps) - 1:
            self._show_step(self.current_step + 1)

    def _on_back(self) -> None:
        """Go to the previous step."""
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    # ------------------------------------------------------------------
    # Config builder
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        """Build the full configuration dict from all wizard state."""
        # Enabled platforms
        enabled = []
        for key in ("linkedin", "indeed", "dice", "ziprecruiter"):
            if self.data[f"{key}_enabled"].get():
                enabled.append(key)

        # Personal info. Start from the on-disk user_config so fields
        # the wizard UI doesn't edit (zip_code, state, street_address,
        # etc.) survive the round-trip instead of being silently
        # dropped on save. The wizard's UI-editable fields then
        # overlay the loaded values.
        personal: dict = {}
        try:
            if USER_CONFIG_FILE.exists():
                with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                saved_personal = existing.get("personal_info", {})
                if isinstance(saved_personal, dict):
                    personal.update(saved_personal)
        except (json.JSONDecodeError, OSError):
            pass

        for key in ("first_name", "last_name", "email", "phone", "city",
                     "linkedin_url", "website"):
            personal[key] = self.data[key].get().strip()

        # Derive a combined 'name' so doctor's user_config check
        # passes without requiring a separate full-name input.
        fn = personal.get("first_name", "")
        ln = personal.get("last_name", "")
        if fn or ln:
            personal["name"] = f"{fn} {ln}".strip()

        # Search keywords as list
        raw_kw = self.data["search_keywords"].get()
        keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]

        config = {
            "enabled_platforms": enabled,
            "personal_info": personal,
            "search_keywords": keywords,
            "location": self.data["location"].get().strip(),
            "max_applications_per_day": self.data["max_applications_per_day"].get(),
            "scoring": {
                "auto_apply_min": self.data["auto_apply_min"].get(),
                "cli_auto_apply_min": self.data["cli_auto_apply_min"].get(),
                "review_min": self.data["review_min"].get(),
            },
            "llm": {
                "ollama_model": self.data["ollama_model"].get().strip(),
                "gemini_api_key": self.data["gemini_api_key"].get().strip(),
            },
            "resumes": [
                {"label": label, "path": path}
                for label, path in self.resume_list
            ],
        }
        return config

    def save_config(self) -> None:
        """Write the current config to user_config.json."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        config = self.get_config()
        with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def save_answers(self) -> None:
        """Write collected answers to answers.json.

        Merges with the existing file instead of overwriting, so
        that:
        - Questions the wizard didn't show this session keep their
          saved answers.
        - Edits from this session update their questions in place.
        - Blanking a field in the wizard removes that entry.

        Uses the form_filler loader to canonicalize whatever shape
        is on disk, then writes back the merged result as a flat
        {question: answer} dict.
        """
        from auto_applier.config import ANSWERS_FILE
        from auto_applier.browser.form_filler import FormFiller

        # Load existing as canonical list of {question, answer}
        existing_entries = FormFiller._load_answers()
        merged: dict[str, str] = {
            e["question"]: e["answer"] for e in existing_entries
            if e.get("question")
        }

        # Apply wizard-session edits. Empty values in the session
        # are treated as 'delete this question' so users can
        # remove stale answers from the Answers step.
        for question, var in self.answer_vars.items():
            value = var.get().strip()
            if value:
                merged[question] = value
            elif question in merged:
                del merged[question]

        with open(ANSWERS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)


def launch_wizard() -> None:
    """Create and run the wizard application."""
    app = WizardApp()
    app.mainloop()
