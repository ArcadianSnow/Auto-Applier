"""Step 4: Personal information."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class PersonalInfoStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard
        self.errors: dict[str, tk.Label] = {}

        content = tk.Frame(self, bg="#F5F7FA")
        content.pack(padx=40, pady=24, fill="both")

        tk.Label(
            content, text="Personal Information",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content, text="Used to fill out application forms automatically.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 18))

        # Name row (two columns)
        name_row = tk.Frame(content, bg="#F5F7FA")
        name_row.pack(fill="x", pady=(0, 4))

        left = tk.Frame(name_row, bg="#F5F7FA")
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._add_field(left, "First Name", "first_name", width=25, required=True)

        right = tk.Frame(name_row, bg="#F5F7FA")
        right.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._add_field(right, "Last Name", "last_name", width=25, required=True)

        # Single column fields
        self._add_field(content, "Phone Number", "phone", width=30, required=True,
                        hint="+1 (555) 000-0000")
        self._add_field(content, "City / Location", "city", width=30, required=True,
                        hint="e.g. San Francisco, CA")
        self._add_field(content, "LinkedIn Profile URL", "linkedin", width=45, required=True,
                        hint="https://linkedin.com/in/yourname")
        self._add_field(content, "Website / Portfolio (optional)", "website", width=45,
                        required=False, hint="https://yoursite.com")

    def _add_field(
        self, parent: tk.Widget, label: str, key: str,
        width: int = 40, required: bool = True, hint: str = "",
    ) -> None:
        tk.Label(
            parent, text=label,
            font=("Segoe UI", 10, "bold"), fg="#374151", bg="#F5F7FA",
        ).pack(anchor="w", pady=(6, 2))

        entry = ttk.Entry(parent, textvariable=self.wizard.data[key], width=width)
        entry.pack(anchor="w")

        if hint:
            tk.Label(
                parent, text=hint,
                font=("Segoe UI", 9), fg="#94A3B8", bg="#F5F7FA",
            ).pack(anchor="w")

        err = tk.Label(
            parent, text="", font=("Segoe UI", 9, "italic"), fg="#EF4444", bg="#F5F7FA",
        )
        err.pack(anchor="w")
        if required:
            self.errors[key] = err
            self.wizard.data[key].trace_add("write", lambda *_, k=key: self.errors[k].configure(text=""))

    def validate(self) -> bool:
        valid = True
        for key, err_label in self.errors.items():
            value = self.wizard.data[key].get().strip()
            if not value:
                label_name = key.replace("_", " ").title()
                err_label.configure(text=f"{label_name} is required.")
                valid = False
        return valid
