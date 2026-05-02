"""Step 5: Job search preferences."""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)
from auto_applier.gui.tooltip import attach_help_icon


# Plain-English help text. Written as if explaining to someone who has
# never used a job search tool before. One place so rewording is easy.
HELP = {
    "keywords": (
        "These are the job titles Auto Applier will search for. "
        "Type them exactly like you would on LinkedIn or Indeed.\n\n"
        "Example: Data Analyst, Business Analyst, Reporting Specialist\n\n"
        "Tip: Add 3–5 related titles. More titles means more jobs "
        "found, but runs will take longer."
    ),
    "location": (
        "Where you want to work. Type a city like 'Seattle, WA', "
        "a whole country like 'United States', or just 'Remote' if "
        "you only want work-from-home jobs.\n\n"
        "Don't worry about matching each site's exact format — Auto "
        "Applier handles the differences for you."
    ),
    "max_apps": (
        "The most applications Auto Applier will submit per site, "
        "per day. So if you set this to 3 and have three sites "
        "enabled, you can get up to 9 applications total in a day "
        "(3 on each site).\n\n"
        "Start small (3–5) while you're trying it out. That way if "
        "something surprises you, only a handful of applications went "
        "out before you noticed.\n\n"
        "When you trust it, you can turn this up to 10 or 15.\n\n"
        "Test runs (the blue button on the last page) ignore this "
        "limit completely — nothing is actually submitted so there's "
        "no quota to protect."
    ),
    "auto_apply": (
        "Auto Applier gives every job a match score from 1 to 10 "
        "based on your resume. Jobs at or above THIS number get "
        "applied to automatically without asking you.\n\n"
        "• 7 (the default) is 'strong match' — safe and recommended\n"
        "• 8 or 9 = pickier, fewer applications but all top matches\n"
        "• 5 or 6 = more aggressive, will apply to average matches\n\n"
        "You can change this later any time."
    ),
    "cli_auto_apply": (
        "Most people can ignore this setting — it only matters for "
        "advanced users who run Auto Applier from a command window "
        "(for example, on a schedule overnight).\n\n"
        "Leave it the same as 'Auto-Apply Score Threshold' above "
        "unless you specifically know you want it different."
    ),
    "review_min": (
        "Jobs scoring BELOW this number are skipped automatically "
        "— Auto Applier won't even bother showing them to you.\n\n"
        "Jobs scoring between this number and the auto-apply number "
        "show up in your Review queue on the dashboard, where you "
        "can decide yes or no one at a time.\n\n"
        "Default is 4. Anything below 4 out of 10 is almost never "
        "worth applying to."
    ),
    "continuous_mode": (
        "Normally Auto Applier runs one pass (hits the application "
        "cap on each site, then stops). Continuous mode keeps the "
        "tool open and repeats that cycle — applying, waiting a "
        "while, applying again, and so on.\n\n"
        "It's a numbers game: more applications over the day beats "
        "fewer, more-perfect ones. The wait between cycles keeps "
        "your activity from looking robotic."
    ),
    "continuous_delay": (
        "How long to wait between cycles, in MINUTES. The tool "
        "picks a random delay somewhere in this range so it doesn't "
        "look like a bot firing on a fixed timer.\n\n"
        "Don't go shorter than 30 minutes — 30 to 90 is a safe, "
        "human-looking rhythm."
    ),
    "continuous_active_hours": (
        "Only apply to jobs during this window of your local day. "
        "Outside the window the tool stays open but doesn't submit "
        "anything — it just uses that time for resume refinement "
        "questions.\n\n"
        "Format: HH:MM-HH:MM (24-hour clock). Example: 09:00-22:00 "
        "for 9am to 10pm. Overnight ranges work too: 22:00-06:00."
    ),
    "continuous_max_cycles": (
        "A safety cap for how many cycles to run before stopping. "
        "Set to 0 for unlimited (run until you hit Stop).\n\n"
        "For your first time using continuous mode, try 3–5 so you "
        "can see how it behaves before committing to an all-day run."
    ),
}


class PreferencesStep(ttk.Frame):
    """Job search preferences -- keywords, location, thresholds."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Job Preferences", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Configure what jobs to search for and how aggressively to apply.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Search card
        search_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        search_card.pack(fill="x", padx=PAD_X, pady=(0, 12))

        tk.Label(
            search_card, text="Search Settings", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 12))

        # Keywords
        kw_row = tk.Frame(search_card, bg=BG_CARD)
        kw_row.pack(fill="x", pady=(0, 12))
        kw_label_row = tk.Frame(kw_row, bg=BG_CARD)
        kw_label_row.pack(fill="x", anchor="w")
        tk.Label(
            kw_label_row, text="Job titles to search for", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(kw_label_row, HELP["keywords"], bg=BG_CARD).pack(
            side="left", padx=(6, 0),
        )
        tk.Label(
            kw_row, text="Type one or more, separated by commas.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")

        # Entry + AI-assist button on the same line. Entry expands
        # to fill, button hugs the right edge so the layout matches
        # the existing "input field with action" pattern in resumes.py.
        kw_input_row = tk.Frame(kw_row, bg=BG_CARD)
        kw_input_row.pack(fill="x", pady=(4, 0))
        ttk.Entry(
            kw_input_row, textvariable=self.wizard.data["search_keywords"],
            font=FONT_BODY,
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            kw_input_row, text="Suggest related titles",
            command=self._open_title_suggester,
        ).pack(side="left", padx=(8, 0))

        # Location
        loc_row = tk.Frame(search_card, bg=BG_CARD)
        loc_row.pack(fill="x", pady=(0, 0))
        loc_label_row = tk.Frame(loc_row, bg=BG_CARD)
        loc_label_row.pack(fill="x", anchor="w")
        tk.Label(
            loc_label_row, text="Where you want to work", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(loc_label_row, HELP["location"], bg=BG_CARD).pack(
            side="left", padx=(6, 0),
        )
        tk.Label(
            loc_row, text="A city, a country, or just 'Remote'.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")
        ttk.Entry(
            loc_row, textvariable=self.wizard.data["location"],
            font=FONT_BODY, width=60,
        ).pack(fill="x", pady=(4, 0))

        # Thresholds card
        threshold_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        threshold_card.pack(fill="x", padx=PAD_X, pady=(0, 12))

        tk.Label(
            threshold_card, text="Application Settings", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 12))

        # Grid of spinboxes
        grid = tk.Frame(threshold_card, bg=BG_CARD)
        grid.pack(fill="x")

        spinbox_fields = [
            ("Most applications per day (per site)", "max_applications_per_day", 1, 50, HELP["max_apps"]),
            ("Auto-apply score (1-10)", "auto_apply_min", 1, 10, HELP["auto_apply"]),
            ("Review score (1-10)", "review_min", 1, 10, HELP["review_min"]),
            ("Command-line auto-apply score (advanced)", "cli_auto_apply_min", 1, 10, HELP["cli_auto_apply"]),
        ]

        for label, key, from_val, to_val, help_text in spinbox_fields:
            row = tk.Frame(grid, bg=BG_CARD)
            row.pack(fill="x", pady=(0, 10))

            tk.Label(
                row, text=label, font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, anchor="w",
            ).pack(side="left")

            attach_help_icon(row, help_text, bg=BG_CARD).pack(
                side="left", padx=(6, 0),
            )

            spin = ttk.Spinbox(
                row,
                textvariable=self.wizard.data[key],
                from_=from_val,
                to=to_val,
                width=6,
                font=FONT_BODY,
            )
            spin.pack(side="right")

        # Plain-English summary at the bottom
        tk.Label(
            threshold_card,
            text=(
                "In plain English:\n"
                "• Jobs with a GREAT match get applied to for you automatically.\n"
                "• Jobs with an OKAY match show up in your review queue to approve one at a time.\n"
                "• Jobs with a POOR match are skipped quietly."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        # Continuous Mode card
        cont_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        cont_card.pack(fill="x", padx=PAD_X, pady=(0, 12))

        header_row = tk.Frame(cont_card, bg=BG_CARD)
        header_row.pack(fill="x")
        tk.Label(
            header_row, text="Continuous Mode (optional)",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(
            header_row, HELP["continuous_mode"], bg=BG_CARD,
        ).pack(side="left", padx=(6, 0))

        tk.Label(
            cont_card,
            text=(
                "Keep Auto Applier running and repeat the application "
                "cycle throughout the day. Off by default — turn on "
                "once you've confirmed a normal run works for you."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            wraplength=680, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        enable_row = tk.Frame(cont_card, bg=BG_CARD)
        enable_row.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            enable_row, text="Enable continuous mode",
            variable=self.wizard.data["continuous_mode"],
        ).pack(side="left")

        # Delay range (minutes)
        delay_row = tk.Frame(cont_card, bg=BG_CARD)
        delay_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            delay_row, text="Wait between cycles (minutes)",
            font=FONT_BODY, fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(
            delay_row, HELP["continuous_delay"], bg=BG_CARD,
        ).pack(side="left", padx=(6, 0))
        spin_frame = tk.Frame(delay_row, bg=BG_CARD)
        spin_frame.pack(side="right")
        ttk.Spinbox(
            spin_frame,
            textvariable=self.wizard.data["continuous_cycle_delay_min"],
            from_=1, to=720, width=5, font=FONT_BODY,
        ).pack(side="left")
        tk.Label(
            spin_frame, text=" to ", font=FONT_BODY, fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        ttk.Spinbox(
            spin_frame,
            textvariable=self.wizard.data["continuous_cycle_delay_max"],
            from_=1, to=720, width=5, font=FONT_BODY,
        ).pack(side="left")

        # Active hours
        hours_row = tk.Frame(cont_card, bg=BG_CARD)
        hours_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            hours_row, text="Active hours (local time)",
            font=FONT_BODY, fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(
            hours_row, HELP["continuous_active_hours"], bg=BG_CARD,
        ).pack(side="left", padx=(6, 0))
        ttk.Entry(
            hours_row,
            textvariable=self.wizard.data["continuous_active_hours"],
            font=FONT_BODY, width=14,
        ).pack(side="right")

        # Max cycles
        cycles_row = tk.Frame(cont_card, bg=BG_CARD)
        cycles_row.pack(fill="x")
        tk.Label(
            cycles_row, text="Safety cap: stop after N cycles (0 = never)",
            font=FONT_BODY, fg=TEXT, bg=BG_CARD,
        ).pack(side="left")
        attach_help_icon(
            cycles_row, HELP["continuous_max_cycles"], bg=BG_CARD,
        ).pack(side="left", padx=(6, 0))
        ttk.Spinbox(
            cycles_row,
            textvariable=self.wizard.data["continuous_max_cycles"],
            from_=0, to=200, width=6, font=FONT_BODY,
        ).pack(side="right")

    def validate(self) -> bool:
        """Require at least one keyword and a location."""
        kw = self.wizard.data["search_keywords"].get().strip()
        loc = self.wizard.data["location"].get().strip()

        missing = []
        if not kw:
            missing.append("Search Keywords")
        if not loc:
            missing.append("Location")

        if missing:
            messagebox.showwarning(
                "Required Fields",
                f"Please fill in: {', '.join(missing)}",
                parent=self.wizard,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Feature A — AI-assisted job-title suggestions
    # ------------------------------------------------------------------

    def _open_title_suggester(self) -> None:
        """Open the modal that asks the LLM for adjacent job titles."""
        TitleSuggesterDialog(self, self.wizard.data["search_keywords"])


class TitleSuggesterDialog(tk.Toplevel):
    """Modal popup: enter a seed title, get AI-suggested adjacents.

    Calls :data:`auto_applier.llm.prompts.TITLE_EXPANSION` directly via
    ``LLMRouter.complete_json`` on a background thread so the wizard
    UI never blocks. Lifted out of the engine path on purpose — the
    engine's expansion routine couples to ``ResumeManager`` and the
    cycle cache, neither of which the wizard owns.

    Selected suggestions are appended to the existing search-keywords
    StringVar in the wizard's existing comma-separated format.
    """

    def __init__(
        self,
        parent: tk.Misc,
        target_var: tk.StringVar,
    ) -> None:
        super().__init__(parent)
        self._target_var = target_var
        self._suggestions: list[str] = []

        self.title("Suggest related job titles")
        self.configure(bg=BG)
        self.geometry("520x460")
        self.minsize(420, 360)

        self._build_ui()

        # Modal-grab pattern matching JobReviewPanel / AlmostPanel.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        # Focus the input on open so the user can start typing
        # immediately. after_idle ensures the widget exists.
        self.after_idle(self._seed_entry.focus_set)

    def _build_ui(self) -> None:
        ttk.Label(
            self, text="Suggest related job titles",
            style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text=(
                "Enter a job title you're targeting. The AI will "
                "suggest adjacent / lateral titles you'd qualify "
                "for at the same seniority level."
            ),
            style="Small.TLabel",
            wraplength=460, justify="left",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Seed input row
        seed_row = tk.Frame(self, bg=BG)
        seed_row.pack(fill="x", padx=PAD_X, pady=(0, 8))
        tk.Label(
            seed_row, text="Job title:", font=FONT_BODY,
            bg=BG, fg=TEXT,
        ).pack(side="left")
        self._seed_var = tk.StringVar()
        self._seed_entry = ttk.Entry(
            seed_row, textvariable=self._seed_var,
            font=FONT_BODY,
        )
        self._seed_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._generate_btn = ttk.Button(
            seed_row, text="Generate",
            style="Primary.TButton",
            command=self._on_generate,
        )
        self._generate_btn.pack(side="left")

        # Pressing Enter in the entry triggers Generate
        self._seed_entry.bind("<Return>", lambda _e: self._on_generate())
        self.bind("<Escape>", lambda _e: self.destroy())

        # Listbox of suggestions
        list_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        list_card.pack(fill="both", expand=True, padx=PAD_X, pady=(8, 8))

        list_inner = tk.Frame(list_card, bg=BG_CARD)
        list_inner.pack(fill="both", expand=True, padx=8, pady=8)

        self._listbox = tk.Listbox(
            list_inner,
            selectmode="extended",
            font=FONT_BODY,
            bg=BG_CARD, fg=TEXT,
            highlightthickness=0, bd=0,
            activestyle="dotbox",
        )
        sb = ttk.Scrollbar(
            list_inner, orient="vertical",
            command=self._listbox.yview,
        )
        self._listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)

        # Action buttons
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        self._add_btn = ttk.Button(
            btn_row, text="Add selected",
            style="Primary.TButton",
            command=self._on_add_selected,
            state="disabled",
        )
        self._add_btn.pack(side="left")
        ttk.Button(
            btn_row, text="Cancel",
            command=self.destroy,
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Generation (background thread + marshal back via after())
    # ------------------------------------------------------------------

    def _on_generate(self) -> None:
        seed = self._seed_var.get().strip()
        if not seed:
            self._show_status_in_listbox("Enter a job title first.")
            return

        self._generate_btn.configure(state="disabled")
        self._add_btn.configure(state="disabled")
        self._show_status_in_listbox("Generating...")

        threading.Thread(
            target=self._worker, args=(seed,), daemon=True,
        ).start()

    def _worker(self, seed: str) -> None:
        # Lazy import to mirror the established pattern in
        # panels/almost.py — keeps GUI startup snappy.
        from auto_applier.llm.prompts import TITLE_EXPANSION
        from auto_applier.llm.router import LLMRouter

        async def run() -> tuple[bool, list[str]]:
            router = LLMRouter()
            await router.initialize()
            prompt = TITLE_EXPANSION.format(
                seed_title=seed,
                resume_text="(not provided)",
            )
            try:
                result = await router.complete_json(
                    prompt=prompt,
                    system_prompt=TITLE_EXPANSION.system,
                )
            except Exception:
                return (False, [])
            raw = result.get("adjacents") or result.get("titles") or []
            cleaned: list[str] = []
            seen: set[str] = {seed.strip().lower()}
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, str):
                        continue
                    item = item.strip()
                    if not item:
                        continue
                    key = item.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append(item)
            return (True, cleaned[:10])

        try:
            ok, titles = asyncio.run(run())
        except Exception:
            ok, titles = False, []

        self.after(0, lambda: self._on_worker_done(ok, titles))

    def _on_worker_done(self, ok: bool, titles: list[str]) -> None:
        try:
            self._generate_btn.configure(state="normal")
        except tk.TclError:
            return  # window already closed
        if not ok:
            self._show_status_in_listbox(
                "Couldn't get suggestions — is Ollama running? "
                "Check `cli doctor`.",
            )
            return
        if not titles:
            self._show_status_in_listbox(
                "No suggestions returned for that title.",
            )
            return
        self._suggestions = titles
        self._listbox.delete(0, tk.END)
        for t in titles:
            self._listbox.insert(tk.END, t)
        self._add_btn.configure(state="normal")

    def _show_status_in_listbox(self, msg: str) -> None:
        self._suggestions = []
        self._listbox.delete(0, tk.END)
        self._listbox.insert(tk.END, msg)

    # ------------------------------------------------------------------
    # Append selected suggestions to the search-keywords field
    # ------------------------------------------------------------------

    def _on_add_selected(self) -> None:
        selected_indices = self._listbox.curselection()
        if not selected_indices:
            return
        picks = [self._suggestions[i] for i in selected_indices
                 if 0 <= i < len(self._suggestions)]
        if not picks:
            return

        # Merge with whatever's already in the field, deduped, comma-sep.
        existing_raw = self._target_var.get().strip()
        existing = [
            t.strip() for t in existing_raw.split(",") if t.strip()
        ]
        seen_lower = {t.lower() for t in existing}
        for pick in picks:
            if pick.lower() not in seen_lower:
                existing.append(pick)
                seen_lower.add(pick.lower())
        self._target_var.set(", ".join(existing))
        self.destroy()
