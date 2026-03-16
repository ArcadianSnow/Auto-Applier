"""Step 2: Platform selection and credentials."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp

PLATFORMS = [
    ("linkedin", "LinkedIn", "Easy Apply"),
    ("indeed", "Indeed", "Smart Apply"),
    ("dice", "Dice", "Easy Apply"),
    ("ziprecruiter", "ZipRecruiter", "1-Click Apply"),
]


class SitesStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard
        self.platform_frames: dict[str, tk.Frame] = {}

        # Scrollable content
        canvas = tk.Canvas(self, bg="#F5F7FA", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg="#F5F7FA")

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw", width=620)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=(40, 0), pady=16)
        scrollbar.pack(side="right", fill="y", pady=16)

        # Bind mousewheel
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        content = self.scroll_frame

        tk.Label(
            content, text="Job Platforms",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content,
            text="Choose which sites to apply on. Enter credentials for each enabled platform.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 12))

        # Security notice
        notice = tk.Frame(content, bg="#DBEAFE", padx=12, pady=8)
        notice.pack(fill="x", pady=(0, 16))
        tk.Label(
            notice,
            text="🔒 All credentials are stored locally only — never uploaded anywhere.",
            font=("Segoe UI", 9), fg="#1E40AF", bg="#DBEAFE",
        ).pack(anchor="w")

        # Platform blocks
        for key, name, apply_type in PLATFORMS:
            self._build_platform_block(content, key, name, apply_type)

        # Error label
        self.error_label = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"),
            fg="#EF4444", bg="#F5F7FA",
        )
        self.error_label.pack(anchor="w", pady=(8, 0))

    def _build_platform_block(
        self, parent: tk.Widget, key: str, name: str, apply_type: str,
    ) -> None:
        """Build a toggleable credential block for one platform."""
        # Outer frame with border
        block = tk.Frame(
            parent, bg="#FFFFFF",
            highlightbackground="#E2E8F0", highlightthickness=1,
            padx=16, pady=12,
        )
        block.pack(fill="x", pady=(0, 8))

        # Header row: checkbox + platform name + apply type badge
        header = tk.Frame(block, bg="#FFFFFF")
        header.pack(fill="x")

        enabled_var = self.wizard.data[f"{key}_enabled"]
        cb = ttk.Checkbutton(
            header, variable=enabled_var,
            command=lambda k=key: self._toggle_creds(k),
        )
        cb.pack(side="left")

        tk.Label(
            header, text=name,
            font=("Segoe UI", 11, "bold"), fg="#1E293B", bg="#FFFFFF",
        ).pack(side="left", padx=(4, 8))

        badge = tk.Label(
            header, text=apply_type,
            font=("Segoe UI", 8), fg="#2563EB", bg="#DBEAFE",
            padx=6, pady=2,
        )
        badge.pack(side="left")

        # Credential fields (hidden by default if not enabled)
        creds_frame = tk.Frame(block, bg="#FFFFFF")
        self.platform_frames[key] = creds_frame

        tk.Label(
            creds_frame, text="Email:",
            font=("Segoe UI", 9, "bold"), fg="#374151", bg="#FFFFFF",
        ).grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(
            creds_frame, textvariable=self.wizard.data[f"{key}_email"], width=35,
        ).grid(row=0, column=1, sticky="w", pady=4)

        tk.Label(
            creds_frame, text="Password:",
            font=("Segoe UI", 9, "bold"), fg="#374151", bg="#FFFFFF",
        ).grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)

        pw_frame = tk.Frame(creds_frame, bg="#FFFFFF")
        pw_frame.grid(row=1, column=1, sticky="w", pady=4)

        pw_entry = ttk.Entry(
            pw_frame, textvariable=self.wizard.data[f"{key}_password"],
            width=28, show="•",
        )
        pw_entry.pack(side="left")

        show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pw_frame, text="Show", variable=show_var,
            command=lambda e=pw_entry, v=show_var: e.configure(show="" if v.get() else "•"),
        ).pack(side="left", padx=(6, 0))

        # Show/hide based on initial state
        if enabled_var.get():
            creds_frame.pack(fill="x", pady=(8, 0))

    def _toggle_creds(self, key: str) -> None:
        """Show or hide credential fields when a platform is toggled."""
        enabled = self.wizard.data[f"{key}_enabled"].get()
        frame = self.platform_frames[key]
        if enabled:
            frame.pack(fill="x", pady=(8, 0))
        else:
            frame.pack_forget()

    def validate(self) -> bool:
        """At least one platform must be enabled with credentials."""
        any_enabled = False

        for key, name, _ in PLATFORMS:
            if not self.wizard.data[f"{key}_enabled"].get():
                continue
            any_enabled = True

            email = self.wizard.data[f"{key}_email"].get().strip()
            password = self.wizard.data[f"{key}_password"].get()

            if not email or "@" not in email:
                self.error_label.configure(text=f"{name}: Please enter a valid email.")
                return False
            if not password or len(password) < 6:
                self.error_label.configure(text=f"{name}: Password must be at least 6 characters.")
                return False

        if not any_enabled:
            self.error_label.configure(text="Please enable at least one platform.")
            return False

        self.error_label.configure(text="")
        return True
