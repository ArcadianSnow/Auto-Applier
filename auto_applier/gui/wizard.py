"""Main wizard controller -- 8-step setup flow for Auto Applier v2."""
import json
import tkinter as tk
from pathlib import Path
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

        # AI Setup moved to position 2 — needs to run before
        # Resumes (skill extraction uses LLM) and Preferences
        # (title expansion uses LLM). Verifying Ollama early
        # prevents the user from configuring everything else only
        # to find the AI step fails at the end.
        self.step_classes = [
            WelcomeStep,
            LLMSetupStep,
            SitesStep,
            ResumesStep,
            PersonalStep,
            PreferencesStep,
            AnswersStep,
            ReadyStep,
        ]
        self.step_labels = [
            "Welcome",
            "AI Setup",
            "Platforms",
            "Resumes",
            "Personal",
            "Preferences",
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
        for key in ("first_name", "last_name", "email", "phone",
                     "street_address", "city", "state", "zip_code",
                     "country", "linkedin_url", "website"):
            self.data[key] = tk.StringVar(value="")
        # Country defaults to United States — that's the project's
        # current audience, and pre-filling spares the user one
        # required field. They can edit it before saving.
        self.data["country"].set("United States")

        # Job preferences
        self.data["search_keywords"] = tk.StringVar(value="")
        self.data["location"] = tk.StringVar(value="")
        self.data["max_applications_per_day"] = tk.IntVar(value=10)
        self.data["auto_apply_min"] = tk.IntVar(value=7)
        self.data["cli_auto_apply_min"] = tk.IntVar(value=7)
        self.data["review_min"] = tk.IntVar(value=4)

        # Continuous-run mode (loop the pipeline on a cadence). Off by
        # default; users opt in via the Preferences step.
        self.data["continuous_mode"] = tk.BooleanVar(value=False)
        # Delays stored in MINUTES in the UI, converted to seconds on save.
        self.data["continuous_cycle_delay_min"] = tk.IntVar(value=30)
        self.data["continuous_cycle_delay_max"] = tk.IntVar(value=90)
        self.data["continuous_active_hours"] = tk.StringVar(value="09:00-22:00")
        self.data["continuous_max_cycles"] = tk.IntVar(value=0)

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
        for key in ("first_name", "last_name", "email", "phone",
                     "street_address", "city", "state", "zip_code",
                     "country", "linkedin_url", "website"):
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

        # Continuous-mode settings (flat at config root).
        if "continuous_mode" in cfg:
            self.data["continuous_mode"].set(bool(cfg["continuous_mode"]))
        if "continuous_cycle_delay_min" in cfg:
            self.data["continuous_cycle_delay_min"].set(
                max(1, int(cfg["continuous_cycle_delay_min"]) // 60)
            )
        if "continuous_cycle_delay_max" in cfg:
            self.data["continuous_cycle_delay_max"].set(
                max(1, int(cfg["continuous_cycle_delay_max"]) // 60)
            )
        if "continuous_active_hours" in cfg:
            self.data["continuous_active_hours"].set(
                cfg["continuous_active_hours"]
            )
        if "continuous_max_cycles" in cfg:
            self.data["continuous_max_cycles"].set(
                int(cfg["continuous_max_cycles"])
            )

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

        for key in ("first_name", "last_name", "email", "phone",
                     "street_address", "city", "state", "zip_code",
                     "country", "linkedin_url", "website"):
            if key in self.data:
                personal[key] = self.data[key].get().strip()

        # Derive a combined 'name' so doctor's user_config check
        # passes without requiring a separate full-name input.
        fn = personal.get("first_name", "")
        ln = personal.get("last_name", "")
        if fn or ln:
            personal["name"] = f"{fn} {ln}".strip()
        # Convenience: the form filler reads either 'zip_code' or
        # 'postal_code' depending on which keyword the JD form used.
        # Mirror zip_code -> postal_code so both styles match.
        if personal.get("zip_code") and not personal.get("postal_code"):
            personal["postal_code"] = personal["zip_code"]
        # Combined city+state for forms that ask for "City, State".
        cs_city = personal.get("city", "")
        cs_state = personal.get("state", "")
        if cs_city and cs_state:
            personal["city_state"] = f"{cs_city}, {cs_state}"
        # Combined address for forms that ask for a single "Address" line.
        addr = personal.get("street_address", "")
        if addr and cs_city and cs_state:
            zc = personal.get("zip_code", "")
            personal["address"] = f"{addr}, {cs_city}, {cs_state} {zc}".strip()

        # Search keywords as list
        raw_kw = self.data["search_keywords"].get()
        keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]

        # Clamp continuous delay min/max so the min is never above the max.
        delay_min_min = max(1, self.data["continuous_cycle_delay_min"].get())
        delay_max_min = max(
            delay_min_min, self.data["continuous_cycle_delay_max"].get(),
        )

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
            "continuous_mode": bool(self.data["continuous_mode"].get()),
            "continuous_cycle_delay_min": delay_min_min * 60,
            "continuous_cycle_delay_max": delay_max_min * 60,
            "continuous_active_hours": self.data["continuous_active_hours"].get().strip(),
            "continuous_max_cycles": int(self.data["continuous_max_cycles"].get()),
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

    def save_personal_info_only(self) -> None:
        """Persist just the personal_info section without touching other keys."""
        self._save_partial(("personal_info",))

    def save_resumes_only(self) -> None:
        """Persist just the resumes section without touching other keys.

        Also eagerly materializes any resume in ``resume_list`` that
        hasn't been copied into ``data/resumes/`` yet — covers the
        case where a user added a resume on an older build (before
        ResumesStep gained eager copy) and now they're advancing
        through the wizard on the newer code.
        """
        self._save_partial(("resumes",))
        self._materialize_pending_resumes()

    def _materialize_pending_resumes(self) -> None:
        """For every (label, source_path) in resume_list, make sure
        the file is present in data/resumes/ and a profile JSON is
        present in data/profiles/.

        Idempotent — skips files already in place. Errors are LOGGED
        (no longer silently swallowed by `except: pass continue`).
        Returns the count of resumes successfully materialized so
        callers / tests can verify behaviour. The previous silent
        version made debugging the parse_resume → extract_text rename
        bug nearly impossible.
        """
        import shutil
        import logging
        from datetime import datetime, timezone
        from auto_applier.config import RESUMES_DIR, PROFILES_DIR
        from auto_applier.resume.parser import extract_text

        log = logging.getLogger(__name__)

        for label, source_path in self.resume_list:
            source = Path(source_path).resolve()
            if not source.exists():
                log.warning(
                    "materialize: source %s for label '%s' missing — skipping",
                    source, label,
                )
                continue
            try:
                RESUMES_DIR.mkdir(parents=True, exist_ok=True)
                PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("materialize: mkdir failed: %s", exc)
                continue

            dest = RESUMES_DIR / f"{label}{source.suffix}"
            if not dest.exists():
                try:
                    shutil.copy2(source, dest)
                except shutil.SameFileError:
                    pass
                except OSError as exc:
                    log.warning(
                        "materialize: copy %s -> %s failed: %s",
                        source, dest, exc,
                    )
                    continue

            profile_path = PROFILES_DIR / f"{label}.json"
            if profile_path.exists():
                continue

            try:
                raw_text = extract_text(str(dest)) if dest.exists() else ""
            except Exception as exc:
                log.warning(
                    "materialize: extract_text failed for %s: %s",
                    dest, exc,
                )
                raw_text = ""

            try:
                profile_path.write_text(
                    json.dumps({
                        "label": label,
                        "source_file": dest.name if dest.exists() else "",
                        "parsed_at": datetime.now(timezone.utc).isoformat(),
                        "raw_text": raw_text,
                        "summary": "",
                        "skills": [],
                        "confirmed_skills": [],
                    }, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                log.warning(
                    "materialize: profile write %s failed: %s",
                    profile_path, exc,
                )

    def save_llm_setup_only(self) -> None:
        """Persist the llm section AND write GEMINI_API_KEY to .env.

        The LLM router reads GEMINI_API_KEY via dotenv from .env, NOT
        from user_config.json — so the wizard form's gemini_api_key
        field has to make it into the .env file or the rest of the
        app will never see it. We write both: user_config.json so
        the wizard remembers it next session, .env so the runtime
        actually uses it.
        """
        self._save_partial(("llm",))
        # Mirror to .env so the runtime can pick it up.
        gemini = self.data["gemini_api_key"].get().strip()
        if gemini:
            self._write_gemini_to_env(gemini)

    def _save_partial(self, sections: tuple[str, ...]) -> None:
        """Write only the named top-level keys of get_config() to disk.

        Reads the existing user_config.json so unrelated sections
        (preferences, platform toggles, search keywords, etc.) keep
        the values they already had — partial saves never wipe
        anything the user already configured.
        """
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if USER_CONFIG_FILE.exists():
            try:
                with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        full_config = self.get_config()
        for section in sections:
            if section in full_config:
                existing[section] = full_config[section]
        with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

    @staticmethod
    def _write_gemini_to_env(api_key: str) -> None:
        """Write or replace the GEMINI_API_KEY line in .env.

        - If .env exists with a GEMINI_API_KEY line, replace just that line.
        - If .env exists without one, append a new line.
        - If .env doesn't exist, create it with just this single line.

        Other lines in .env (LinkedIn creds, etc.) are preserved.
        """
        from auto_applier.config import PROJECT_ROOT
        env_path = PROJECT_ROOT / ".env"
        existing_lines: list[str] = []
        replaced = False
        if env_path.exists():
            try:
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                existing_lines = []
        new_lines: list[str] = []
        for line in existing_lines:
            stripped = line.lstrip()
            if stripped.startswith("GEMINI_API_KEY=") or stripped.startswith("GEMINI_API_KEY ="):
                new_lines.append(f"GEMINI_API_KEY={api_key}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"GEMINI_API_KEY={api_key}")
        # Trailing newline is standard for .env files.
        env_path.write_text(
            "\n".join(new_lines).rstrip() + "\n",
            encoding="utf-8",
        )

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
