"""Step 2: Platform selection."""
import tkinter as tk
from tkinter import ttk

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)

# Platform metadata: (key, display_name, description)
PLATFORMS = [
    (
        "linkedin",
        "LinkedIn",
        "The largest professional network. Best for white-collar and tech roles.",
    ),
    (
        "indeed",
        "Indeed",
        "High-volume job board with listings across all industries and levels.",
    ),
    (
        "dice",
        "Dice",
        "Specialized in technology and engineering positions.",
    ),
    (
        "ziprecruiter",
        "ZipRecruiter",
        "AI-powered matching with a broad range of employers.",
    ),
]


class SitesStep(ttk.Frame):
    """Platform selection with checkboxes and descriptions."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Select Job Platforms", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Choose which job sites to search. All are enabled by default.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Platform cards
        for key, name, desc in PLATFORMS:
            card = tk.Frame(
                self, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            card.pack(fill="x", padx=PAD_X, pady=4)

            var = self.wizard.data[f"{key}_enabled"]

            top_row = tk.Frame(card, bg=BG_CARD)
            top_row.pack(fill="x")

            cb = ttk.Checkbutton(
                top_row, text=name, variable=var,
                style="Card.TCheckbutton",
            )
            cb.pack(side="left")

            tk.Label(
                card, text=desc, font=FONT_SMALL,
                fg=TEXT_LIGHT, bg=BG_CARD, anchor="w",
            ).pack(anchor="w", padx=(24, 0), pady=(2, 0))

        # Note
        note_frame = tk.Frame(self, bg=BG)
        note_frame.pack(fill="x", padx=PAD_X, pady=(PAD_Y, 0))

        tk.Label(
            note_frame,
            text="\u2139  You will log in manually in the browser window during runs. "
                 "Credentials are never stored.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG,
            wraplength=700, justify="left",
        ).pack(anchor="w")

    def validate(self) -> bool:
        """At least one platform must be selected."""
        for key, _, _ in PLATFORMS:
            if self.wizard.data[f"{key}_enabled"].get():
                return True
        from tkinter import messagebox
        messagebox.showwarning(
            "No Platforms Selected",
            "Please select at least one job platform.",
            parent=self.wizard,
        )
        return False
