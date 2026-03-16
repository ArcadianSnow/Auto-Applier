"""Step 5: Job search preferences — AC theme."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY, FOGGY, ERROR_RED,
    BORDER_LIGHT, HEADING_FONT, BODY_FONT,
)

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class PreferencesStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard

        content = tk.Frame(self, bg=SANDY_SHORE)
        content.pack(padx=40, pady=24, fill="both")

        tk.Label(content, text="Job Search Preferences", font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 4))
        tk.Label(content, text="Define what jobs to search for across all platforms.", font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 20))

        tk.Label(content, text="Job Titles / Keywords", font=(BODY_FONT, 10, "bold"), fg=BARK_BROWN, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 3))
        ttk.Entry(content, textvariable=wizard.data["keywords"], width=55).pack(anchor="w")
        tk.Label(content, text="Comma-separated, e.g. Software Engineer, Backend Developer", font=(BODY_FONT, 9), fg=FOGGY, bg=SANDY_SHORE).pack(anchor="w", pady=(2, 2))
        self.kw_error = tk.Label(content, text="", font=(BODY_FONT, 9, "italic"), fg=ERROR_RED, bg=SANDY_SHORE)
        self.kw_error.pack(anchor="w", pady=(0, 12))

        tk.Label(content, text="Preferred Location", font=(BODY_FONT, 10, "bold"), fg=BARK_BROWN, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 3))
        ttk.Entry(content, textvariable=wizard.data["location"], width=55).pack(anchor="w")
        tk.Label(content, text="Enter a city, region, or 'Remote'", font=(BODY_FONT, 9), fg=FOGGY, bg=SANDY_SHORE).pack(anchor="w", pady=(2, 2))
        self.loc_error = tk.Label(content, text="", font=(BODY_FONT, 9, "italic"), fg=ERROR_RED, bg=SANDY_SHORE)
        self.loc_error.pack(anchor="w", pady=(0, 16))

        coming = tk.LabelFrame(content, text="Advanced Filters (coming soon)", font=(BODY_FONT, 9), fg=FOGGY, bg=SANDY_SHORE, padx=12, pady=8)
        coming.pack(fill="x", pady=(8, 0))
        tk.Label(coming, text="Experience level, job type, salary range — coming in a future version.", font=(BODY_FONT, 9, "italic"), fg=FOGGY, bg=SANDY_SHORE).pack(anchor="w")

        wizard.data["keywords"].trace_add("write", lambda *_: self.kw_error.configure(text=""))
        wizard.data["location"].trace_add("write", lambda *_: self.loc_error.configure(text=""))

    def validate(self):
        valid = True
        if not self.wizard.data["keywords"].get().strip():
            self.kw_error.configure(text="Enter at least one job title or keyword.")
            valid = False
        if not self.wizard.data["location"].get().strip():
            self.loc_error.configure(text="Enter a location or 'Remote'.")
            valid = False
        return valid
