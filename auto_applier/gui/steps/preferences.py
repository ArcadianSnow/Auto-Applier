"""Step 5: Job search preferences."""
import tkinter as tk
from tkinter import ttk, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)


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
        tk.Label(
            kw_row, text="Search Keywords", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(anchor="w")
        tk.Label(
            kw_row, text="Comma-separated, e.g.: Data Analyst, Python Developer",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w")
        ttk.Entry(
            kw_row, textvariable=self.wizard.data["search_keywords"],
            font=FONT_BODY, width=60,
        ).pack(fill="x", pady=(4, 0))

        # Location
        loc_row = tk.Frame(search_card, bg=BG_CARD)
        loc_row.pack(fill="x", pady=(0, 0))
        tk.Label(
            loc_row, text="Location", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(anchor="w")
        tk.Label(
            loc_row, text="e.g.: New York, NY  or  Remote",
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
            ("Max Applications Per Day", "max_applications_per_day", 1, 50),
            ("Auto-Apply Score Threshold (1-10)", "auto_apply_min", 1, 10),
            ("CLI Auto-Apply Threshold (1-10)", "cli_auto_apply_min", 1, 10),
            ("Review Minimum Score (1-10)", "review_min", 1, 10),
        ]

        for i, (label, key, from_val, to_val) in enumerate(spinbox_fields):
            row = tk.Frame(grid, bg=BG_CARD)
            row.pack(fill="x", pady=(0, 10))

            tk.Label(
                row, text=label, font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, anchor="w",
            ).pack(side="left")

            spin = ttk.Spinbox(
                row,
                textvariable=self.wizard.data[key],
                from_=from_val,
                to=to_val,
                width=6,
                font=FONT_BODY,
            )
            spin.pack(side="right")

        # Explanation
        tk.Label(
            threshold_card,
            text=(
                "Jobs scoring above the auto-apply threshold are applied to automatically.\n"
                "Jobs between review minimum and auto-apply are queued for your review.\n"
                "Jobs below the review minimum are skipped."
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
