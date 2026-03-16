"""Step 2: LinkedIn credentials."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class CredentialsStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard

        content = tk.Frame(self, bg="#F5F7FA")
        content.pack(padx=40, pady=24, fill="both")

        # Heading
        tk.Label(
            content, text="LinkedIn Credentials",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content, text="Your credentials are stored locally and never shared.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 12))

        # Security notice
        notice = tk.Frame(content, bg="#DBEAFE", padx=12, pady=8)
        notice.pack(fill="x", pady=(0, 20))
        tk.Label(
            notice,
            text="🔒 Credentials are saved to a local config file only — never uploaded anywhere.",
            font=("Segoe UI", 9), fg="#1E40AF", bg="#DBEAFE",
        ).pack(anchor="w")

        # Email
        tk.Label(
            content, text="Email Address",
            font=("Segoe UI", 10, "bold"), fg="#374151", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 3))

        self.email_entry = ttk.Entry(content, textvariable=wizard.data["email"], width=45)
        self.email_entry.pack(anchor="w", pady=(0, 2))

        self.email_error = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"), fg="#EF4444", bg="#F5F7FA",
        )
        self.email_error.pack(anchor="w", pady=(0, 10))

        # Password
        tk.Label(
            content, text="Password",
            font=("Segoe UI", 10, "bold"), fg="#374151", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 3))

        pw_frame = tk.Frame(content, bg="#F5F7FA")
        pw_frame.pack(anchor="w", pady=(0, 2))

        self.password_entry = ttk.Entry(
            pw_frame, textvariable=wizard.data["password"], width=35, show="•",
        )
        self.password_entry.pack(side="left")

        self.show_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pw_frame, text="Show", variable=self.show_pw,
            command=self._toggle_password,
        ).pack(side="left", padx=(8, 0))

        self.pw_error = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"), fg="#EF4444", bg="#F5F7FA",
        )
        self.pw_error.pack(anchor="w", pady=(0, 10))

        # Clear errors on typing
        wizard.data["email"].trace_add("write", lambda *_: self.email_error.configure(text=""))
        wizard.data["password"].trace_add("write", lambda *_: self.pw_error.configure(text=""))

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_pw.get() else "•")

    def validate(self) -> bool:
        valid = True
        email = self.wizard.data["email"].get().strip()
        password = self.wizard.data["password"].get()

        if not email or "@" not in email:
            self.email_error.configure(text="Please enter a valid email address.")
            valid = False

        if not password or len(password) < 6:
            self.pw_error.configure(text="Password must be at least 6 characters.")
            valid = False

        return valid
