"""Step 5: Job search preferences."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class PreferencesStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard

        content = tk.Frame(self, bg="#F5F7FA")
        content.pack(padx=40, pady=24, fill="both")

        tk.Label(
            content, text="Job Search Preferences",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content, text="Define what jobs to search for on LinkedIn.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 20))

        # Keywords
        tk.Label(
            content, text="Job Titles / Keywords",
            font=("Segoe UI", 10, "bold"), fg="#374151", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 3))

        ttk.Entry(
            content, textvariable=wizard.data["keywords"], width=55,
        ).pack(anchor="w")

        tk.Label(
            content,
            text="Comma-separated, e.g. Software Engineer, Backend Developer",
            font=("Segoe UI", 9), fg="#94A3B8", bg="#F5F7FA",
        ).pack(anchor="w", pady=(2, 2))

        self.kw_error = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"), fg="#EF4444", bg="#F5F7FA",
        )
        self.kw_error.pack(anchor="w", pady=(0, 12))

        # Location
        tk.Label(
            content, text="Preferred Location",
            font=("Segoe UI", 10, "bold"), fg="#374151", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 3))

        ttk.Entry(
            content, textvariable=wizard.data["location"], width=55,
        ).pack(anchor="w")

        tk.Label(
            content, text="Enter a city, region, or 'Remote'",
            font=("Segoe UI", 9), fg="#94A3B8", bg="#F5F7FA",
        ).pack(anchor="w", pady=(2, 2))

        self.loc_error = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"), fg="#EF4444", bg="#F5F7FA",
        )
        self.loc_error.pack(anchor="w", pady=(0, 16))

        # Coming soon box
        coming_soon = tk.LabelFrame(
            content, text="Advanced Filters (coming soon)",
            font=("Segoe UI", 9), fg="#94A3B8", bg="#F5F7FA",
            padx=12, pady=8,
        )
        coming_soon.pack(fill="x", pady=(8, 0))

        tk.Label(
            coming_soon,
            text="Experience level, job type, salary range — coming in a future version.",
            font=("Segoe UI", 9, "italic"), fg="#94A3B8", bg="#F5F7FA",
        ).pack(anchor="w")

        # Clear errors on typing
        wizard.data["keywords"].trace_add("write", lambda *_: self.kw_error.configure(text=""))
        wizard.data["location"].trace_add("write", lambda *_: self.loc_error.configure(text=""))

    def validate(self) -> bool:
        valid = True

        kw = self.wizard.data["keywords"].get().strip()
        if not kw:
            self.kw_error.configure(text="Enter at least one job title or keyword.")
            valid = False

        loc = self.wizard.data["location"].get().strip()
        if not loc:
            self.loc_error.configure(text="Enter a location or 'Remote'.")
            valid = False

        return valid
