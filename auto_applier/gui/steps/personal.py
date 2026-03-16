"""Step 4: Personal information — AC theme."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY, FOGGY, ERROR_RED,
    HEADING_FONT, BODY_FONT,
)

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class PersonalInfoStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard
        self.errors: dict[str, tk.Label] = {}

        canvas = tk.Canvas(self, bg=SANDY_SHORE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg=SANDY_SHORE)
        self.scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw", width=600)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(40, 0), pady=16)
        scrollbar.pack(side="right", fill="y", pady=16)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        content = self.scroll_frame

        tk.Label(content, text="Personal Information", font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 4))
        tk.Label(content, text="Used to fill out application forms automatically.", font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE).pack(anchor="w", pady=(0, 14))

        name_row = tk.Frame(content, bg=SANDY_SHORE)
        name_row.pack(fill="x", pady=(0, 2))
        left = tk.Frame(name_row, bg=SANDY_SHORE)
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._add_field(left, "First Name", "first_name", width=25, required=True)
        right = tk.Frame(name_row, bg=SANDY_SHORE)
        right.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._add_field(right, "Last Name", "last_name", width=25, required=True)

        self._add_field(content, "Phone Number", "phone", width=30, required=True, hint="+1 (555) 000-0000")
        self._add_field(content, "City / Location", "city", width=30, required=True, hint="e.g. San Francisco, CA")
        self._add_field(content, "LinkedIn Profile URL", "linkedin", width=45, required=True, hint="https://linkedin.com/in/yourname")
        self._add_field(content, "Website / Portfolio (optional)", "website", width=45, required=False, hint="https://yoursite.com")

    def _add_field(self, parent, label, key, width=40, required=True, hint=""):
        tk.Label(parent, text=label, font=(BODY_FONT, 10, "bold"), fg=BARK_BROWN, bg=SANDY_SHORE).pack(anchor="w", pady=(4, 2))
        ttk.Entry(parent, textvariable=self.wizard.data[key], width=width).pack(anchor="w")
        if hint:
            tk.Label(parent, text=hint, font=(BODY_FONT, 9), fg=FOGGY, bg=SANDY_SHORE).pack(anchor="w")
        err = tk.Label(parent, text="", font=(BODY_FONT, 9, "italic"), fg=ERROR_RED, bg=SANDY_SHORE)
        err.pack(anchor="w")
        if required:
            self.errors[key] = err
            self.wizard.data[key].trace_add("write", lambda *_, k=key: self.errors[k].configure(text=""))

    def validate(self):
        valid = True
        for key, err in self.errors.items():
            if not self.wizard.data[key].get().strip():
                err.configure(text=f"{key.replace('_', ' ').title()} is required.")
                valid = False
        return valid
