"""Step 1: Welcome screen."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, SOIL_BROWN, DRIFTWOOD_GRAY, NOOK_GREEN, LEAF_GOLD, WARM_WHITE,
    HEADING_FONT, BODY_FONT,
)

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class WelcomeStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard

        inner = tk.Frame(self, bg=SANDY_SHORE)
        inner.place(relx=0.5, rely=0.45, anchor="center")

        # Leaf logo
        logo = tk.Canvas(inner, width=68, height=68, bg=SANDY_SHORE, highlightthickness=0)
        logo.create_oval(2, 2, 66, 66, fill=NOOK_GREEN, outline="#2E7D52", width=2)
        logo.create_text(34, 28, text="🍃", font=(BODY_FONT, 18))
        logo.create_text(34, 50, text="A", fill=WARM_WHITE, font=(HEADING_FONT, 14, "bold"))
        logo.pack(pady=(0, 16))

        tk.Label(
            inner,
            text="Welcome to Auto Applier",
            font=(HEADING_FONT, 18, "bold"),
            fg=SOIL_BROWN,
            bg=SANDY_SHORE,
        ).pack(pady=(0, 8))

        tk.Label(
            inner,
            text=(
                "Auto Applier searches job sites for positions matching\n"
                "your criteria and applies automatically. It also tracks\n"
                "what skills employers are asking for to improve your resume."
            ),
            font=(BODY_FONT, 10),
            fg=DRIFTWOOD_GRAY,
            bg=SANDY_SHORE,
            justify="center",
        ).pack(pady=(0, 28))

        ttk.Button(
            inner,
            text="Get Started",
            style="Primary.TButton",
            command=self._get_started,
        ).pack(pady=(0, 8), ipadx=20)

        ttk.Button(
            inner,
            text="Create Dummy Data (Dry Run)",
            style="Secondary.TButton",
            command=self._dummy_run,
        ).pack(ipadx=10)

    def _get_started(self) -> None:
        self.wizard.go_to_step(1)

    def _dummy_run(self) -> None:
        self.wizard.fill_dummy_data()
        self.wizard.go_to_step(len(self.wizard.step_frames) - 1)
