"""Step 1: Welcome screen."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class WelcomeStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard

        # Center everything
        inner = tk.Frame(self, bg="#F5F7FA")
        inner.place(relx=0.5, rely=0.45, anchor="center")

        # Logo circle
        logo = tk.Canvas(inner, width=64, height=64, bg="#F5F7FA", highlightthickness=0)
        logo.create_oval(2, 2, 62, 62, fill="#2563EB", outline="")
        logo.create_text(32, 32, text="A", fill="white", font=("Segoe UI", 24, "bold"))
        logo.pack(pady=(0, 16))

        # Title
        tk.Label(
            inner,
            text="Welcome to Auto Applier",
            font=("Segoe UI", 16, "bold"),
            fg="#1E293B",
            bg="#F5F7FA",
        ).pack(pady=(0, 8))

        # Description
        tk.Label(
            inner,
            text=(
                "Auto Applier searches LinkedIn for jobs matching your criteria\n"
                "and automatically applies using Easy Apply. It also tracks what\n"
                "skills and info employers are asking for to help improve your resume."
            ),
            font=("Segoe UI", 10),
            fg="#64748B",
            bg="#F5F7FA",
            justify="center",
        ).pack(pady=(0, 28))

        # Buttons
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
