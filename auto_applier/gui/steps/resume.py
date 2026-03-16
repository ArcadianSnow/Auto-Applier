"""Step 3: Resume upload."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class ResumeStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard

        content = tk.Frame(self, bg="#F5F7FA")
        content.pack(padx=40, pady=24, fill="both")

        tk.Label(
            content, text="Upload Your Resume",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content,
            text="Supported formats: PDF, DOCX. Your resume is used to fill application forms.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 20))

        # Browse row
        browse_row = tk.Frame(content, bg="#F5F7FA")
        browse_row.pack(anchor="w", pady=(0, 8))

        ttk.Button(
            browse_row, text="Browse Files...",
            style="Secondary.TButton", command=self._browse,
        ).pack(side="left")

        self.file_label = tk.Label(
            browse_row, text="No file selected",
            font=("Segoe UI", 10, "italic"), fg="#94A3B8", bg="#F5F7FA",
        )
        self.file_label.pack(side="left", padx=(12, 0))

        # File preview card (hidden initially)
        self.preview_card = tk.Frame(
            content, bg="#FFFFFF", highlightbackground="#E2E8F0",
            highlightthickness=1, padx=16, pady=10,
        )

        self.preview_name = tk.Label(
            self.preview_card, text="", font=("Segoe UI", 10, "bold"),
            fg="#1E293B", bg="#FFFFFF",
        )
        self.preview_name.pack(anchor="w")

        self.preview_size = tk.Label(
            self.preview_card, text="", font=("Segoe UI", 9),
            fg="#64748B", bg="#FFFFFF",
        )
        self.preview_size.pack(anchor="w")

        remove_row = tk.Frame(self.preview_card, bg="#FFFFFF")
        remove_row.pack(anchor="e")
        ttk.Button(
            remove_row, text="Remove", style="Ghost.TButton",
            command=self._remove_file,
        ).pack()

        # Error
        self.error_label = tk.Label(
            content, text="", font=("Segoe UI", 9, "italic"),
            fg="#EF4444", bg="#F5F7FA",
        )
        self.error_label.pack(anchor="w", pady=(8, 0))

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Resume",
            filetypes=[("Resume Files", "*.pdf *.docx"), ("All Files", "*.*")],
        )
        if path:
            self.wizard.data["resume_path"].set(path)
            self._show_preview(path)
            self.error_label.configure(text="")

    def _show_preview(self, path: str) -> None:
        name = os.path.basename(path)
        display = name if len(name) <= 45 else name[:42] + "..."
        self.file_label.configure(text=display, fg="#1E293B", font=("Segoe UI", 10, "bold"))

        try:
            size_bytes = os.path.getsize(path)
            if size_bytes < 1024:
                size_str = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        except OSError:
            size_str = "Unknown size"

        self.preview_name.configure(text=name)
        self.preview_size.configure(text=size_str)
        self.preview_card.pack(anchor="w", fill="x", pady=(4, 0))

    def _remove_file(self) -> None:
        self.wizard.data["resume_path"].set("")
        self.file_label.configure(
            text="No file selected", fg="#94A3B8", font=("Segoe UI", 10, "italic"),
        )
        self.preview_card.pack_forget()

    def validate(self) -> bool:
        path = self.wizard.data["resume_path"].get().strip()

        # Allow dummy data to pass
        if path.startswith("(dummy)"):
            return True

        if not path:
            self.error_label.configure(text="Please select a resume file.")
            return False

        ext = Path(path).suffix.lower()
        if ext not in (".pdf", ".docx"):
            self.error_label.configure(text="Only PDF and DOCX files are supported.")
            return False

        if not os.path.exists(path):
            self.error_label.configure(text="File not found. Please select again.")
            return False

        return True
