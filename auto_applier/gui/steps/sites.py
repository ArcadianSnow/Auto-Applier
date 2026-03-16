"""Step 2: Platform selection and credentials — AC theme."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, CREAM, WARM_WHITE, DRIFTWOOD, MORNING_SKY,
    SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY, FOGGY,
    BORDER_LIGHT, BORDER_MED, NOOK_GREEN, INFO_BLUE, ERROR_RED,
    HEADING_FONT, BODY_FONT,
)

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
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard
        self.platform_frames: dict[str, tk.Frame] = {}

        canvas = tk.Canvas(self, bg=SANDY_SHORE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg=SANDY_SHORE)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw", width=620)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(40, 0), pady=16)
        scrollbar.pack(side="right", fill="y", pady=16)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        content = self.scroll_frame

        tk.Label(
            content, text="Job Platforms",
            font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=SANDY_SHORE,
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content,
            text="Choose which sites to apply on. Enter credentials for each.",
            font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE,
        ).pack(anchor="w", pady=(0, 12))

        notice = tk.Frame(content, bg=MORNING_SKY, padx=12, pady=8)
        notice.pack(fill="x", pady=(0, 16))
        tk.Label(
            notice,
            text="🍃 All credentials are stored locally — never uploaded anywhere.",
            font=(BODY_FONT, 9), fg=INFO_BLUE, bg=MORNING_SKY,
        ).pack(anchor="w")

        for key, name, apply_type in PLATFORMS:
            self._build_platform_block(content, key, name, apply_type)

        self.error_label = tk.Label(
            content, text="", font=(BODY_FONT, 9, "italic"),
            fg=ERROR_RED, bg=SANDY_SHORE,
        )
        self.error_label.pack(anchor="w", pady=(8, 0))

    def _build_platform_block(self, parent, key, name, apply_type):
        block = tk.Frame(
            parent, bg=CREAM,
            highlightbackground=BORDER_LIGHT, highlightthickness=1,
            padx=16, pady=12,
        )
        block.pack(fill="x", pady=(0, 8))

        header = tk.Frame(block, bg=CREAM)
        header.pack(fill="x")

        enabled_var = self.wizard.data[f"{key}_enabled"]
        cb = ttk.Checkbutton(header, variable=enabled_var, command=lambda k=key: self._toggle(k))
        cb.pack(side="left")

        tk.Label(
            header, text=name,
            font=(HEADING_FONT, 11, "bold"), fg=SOIL_BROWN, bg=CREAM,
        ).pack(side="left", padx=(4, 8))

        tk.Label(
            header, text=apply_type,
            font=(BODY_FONT, 8), fg=NOOK_GREEN, bg=MORNING_SKY, padx=6, pady=2,
        ).pack(side="left")

        creds = tk.Frame(block, bg=CREAM)
        self.platform_frames[key] = creds

        tk.Label(
            creds, text="Email:", font=(BODY_FONT, 9, "bold"), fg=BARK_BROWN, bg=CREAM,
        ).grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(creds, textvariable=self.wizard.data[f"{key}_email"], width=35).grid(
            row=0, column=1, sticky="w", pady=4,
        )

        tk.Label(
            creds, text="Password:", font=(BODY_FONT, 9, "bold"), fg=BARK_BROWN, bg=CREAM,
        ).grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)

        pw_frame = tk.Frame(creds, bg=CREAM)
        pw_frame.grid(row=1, column=1, sticky="w", pady=4)
        pw_entry = ttk.Entry(
            pw_frame, textvariable=self.wizard.data[f"{key}_password"], width=28, show="•",
        )
        pw_entry.pack(side="left")
        show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pw_frame, text="Show", variable=show_var,
            command=lambda e=pw_entry, v=show_var: e.configure(show="" if v.get() else "•"),
        ).pack(side="left", padx=(6, 0))

        if enabled_var.get():
            creds.pack(fill="x", pady=(8, 0))

    def _toggle(self, key):
        if self.wizard.data[f"{key}_enabled"].get():
            self.platform_frames[key].pack(fill="x", pady=(8, 0))
        else:
            self.platform_frames[key].pack_forget()

    def validate(self):
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
