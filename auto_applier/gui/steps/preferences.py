"""Step 5: Job search preferences."""
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
        "The most applications Auto Applier is allowed to actually "
        "submit in one day. When it hits this number, it stops — "
        "even if it finds more good jobs.\n\n"
        "Start small (3–5) while you're trying it out. That way if "
        "something surprises you, only a handful of applications went "
        "out before you noticed.\n\n"
        "When you trust it, you can turn this up to 10 or 15."
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
        ttk.Entry(
            kw_row, textvariable=self.wizard.data["search_keywords"],
            font=FONT_BODY, width=60,
        ).pack(fill="x", pady=(4, 0))

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
            ("Most applications per day", "max_applications_per_day", 1, 50, HELP["max_apps"]),
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
