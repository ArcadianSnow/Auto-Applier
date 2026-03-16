"""Step 3: Resume upload — AC theme."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, CREAM, WARM_WHITE, SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY, FOGGY,
    BORDER_LIGHT, ERROR_RED, HEADING_FONT, BODY_FONT,
)

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class ResumeStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard

        content = tk.Frame(self, bg=SANDY_SHORE)
        content.pack(padx=40, pady=24, fill="both")

        tk.Label(
            content, text="Upload Your Resume",
            font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=SANDY_SHORE,
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            content, text="Supported formats: PDF, DOCX. Used to fill application forms.",
            font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE,
        ).pack(anchor="w", pady=(0, 20))

        browse_row = tk.Frame(content, bg=SANDY_SHORE)
        browse_row.pack(anchor="w", pady=(0, 8))
        ttk.Button(
            browse_row, text="Browse Files...", style="Secondary.TButton", command=self._browse,
        ).pack(side="left")
        self.file_label = tk.Label(
            browse_row, text="No file selected",
            font=(BODY_FONT, 10, "italic"), fg=FOGGY, bg=SANDY_SHORE,
        )
        self.file_label.pack(side="left", padx=(12, 0))

        self.preview_card = tk.Frame(
            content, bg=CREAM, highlightbackground=BORDER_LIGHT,
            highlightthickness=1, padx=16, pady=10,
        )
        self.preview_name = tk.Label(
            self.preview_card, text="", font=(BODY_FONT, 10, "bold"), fg=SOIL_BROWN, bg=CREAM,
        )
        self.preview_name.pack(anchor="w")
        self.preview_size = tk.Label(
            self.preview_card, text="", font=(BODY_FONT, 9), fg=DRIFTWOOD_GRAY, bg=CREAM,
        )
        self.preview_size.pack(anchor="w")
        remove_row = tk.Frame(self.preview_card, bg=CREAM)
        remove_row.pack(anchor="e")
        ttk.Button(remove_row, text="Remove", style="Ghost.TButton", command=self._remove).pack()

        self.error_label = tk.Label(
            content, text="", font=(BODY_FONT, 9, "italic"), fg=ERROR_RED, bg=SANDY_SHORE,
        )
        self.error_label.pack(anchor="w", pady=(8, 0))

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select Resume",
            filetypes=[("Resume Files", "*.pdf *.docx"), ("All Files", "*.*")],
        )
        if path:
            self.wizard.data["resume_path"].set(path)
            self._show_preview(path)
            self.error_label.configure(text="")

    def _show_preview(self, path):
        name = os.path.basename(path)
        self.file_label.configure(text=name[:45] + "..." if len(name) > 45 else name, fg=SOIL_BROWN, font=(BODY_FONT, 10, "bold"))
        try:
            size = os.path.getsize(path)
            size_str = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024*1024):.1f} MB"
        except OSError:
            size_str = "Unknown size"
        self.preview_name.configure(text=name)
        self.preview_size.configure(text=size_str)
        self.preview_card.pack(anchor="w", fill="x", pady=(4, 0))

    def _remove(self):
        self.wizard.data["resume_path"].set("")
        self.file_label.configure(text="No file selected", fg=FOGGY, font=(BODY_FONT, 10, "italic"))
        self.preview_card.pack_forget()

    def validate(self):
        path = self.wizard.data["resume_path"].get().strip()
        if path.startswith("(dummy)"):
            return True
        if not path:
            self.error_label.configure(text="Please select a resume file.")
            return False
        if Path(path).suffix.lower() not in (".pdf", ".docx"):
            self.error_label.configure(text="Only PDF and DOCX files are supported.")
            return False
        if not os.path.exists(path):
            self.error_label.configure(text="File not found. Please select again.")
            return False
        return True
