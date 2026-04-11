"""Step 1: Welcome screen."""
import tkinter as tk
from tkinter import ttk

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)


class WelcomeStep(ttk.Frame):
    """Welcome screen with feature overview."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Center container
        center = tk.Frame(self, bg=BG)
        center.place(relx=0.5, rely=0.45, anchor="center")

        # Title
        tk.Label(
            center, text="Auto Applier v2", font=("Segoe UI", 24, "bold"),
            fg=PRIMARY, bg=BG,
        ).pack(pady=(0, 8))

        # Subtitle
        tk.Label(
            center,
            text=(
                "A helper that searches job sites for you, figures out\n"
                "which postings actually match your resume, and fills out\n"
                "the applications automatically. Free, runs on your own\n"
                "computer, and you stay in control the whole time."
            ),
            font=FONT_BODY, fg=TEXT_LIGHT, bg=BG, justify="center",
        ).pack(pady=(0, 32))

        # Feature cards
        features = [
            (
                "Searches multiple sites",
                "Looks at LinkedIn, Indeed, Dice, and ZipRecruiter so you "
                "don't have to check them one by one.",
            ),
            (
                "Only picks good matches",
                "Reads each job posting and compares it to your resume. "
                "Skips the stuff that doesn't fit, applies to the jobs "
                "that do.",
            ),
            (
                "Remembers what's asked",
                "Notices when job forms keep asking about skills you "
                "don't have listed, so you can add them to your resume "
                "later.",
            ),
            (
                "Writes cover letters",
                "Generates a fresh, specific cover letter for each job "
                "instead of copy-pasting the same one everywhere.",
            ),
        ]

        grid = tk.Frame(center, bg=BG)
        grid.pack(pady=(0, 24))

        for i, (title, desc) in enumerate(features):
            row, col = divmod(i, 2)
            card = tk.Frame(
                grid, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            grid.columnconfigure(col, weight=1, minsize=300)

            tk.Label(
                card, text=title, font=FONT_SUBHEADING,
                fg=PRIMARY, bg=BG_CARD, anchor="w",
            ).pack(anchor="w")
            tk.Label(
                card, text=desc, font=FONT_SMALL,
                fg=TEXT_LIGHT, bg=BG_CARD, anchor="w",
                wraplength=260, justify="left",
            ).pack(anchor="w", pady=(4, 0))

        # Get started hint
        tk.Label(
            center, text="Click Next to get started.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG,
        ).pack()
