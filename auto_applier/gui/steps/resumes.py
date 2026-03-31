"""Step 3: Multi-resume manager."""
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, ACCENT, DANGER, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)


class ResumesStep(ttk.Frame):
    """Multi-resume management step."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Your Resumes", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Add one or more resumes. The AI will pick the best one for each job.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Main card
        card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=16, pady=16,
        )
        card.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 8))

        # Listbox with scrollbar
        list_frame = tk.Frame(card, bg=BG_CARD)
        list_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(
            list_frame,
            font=FONT_BODY,
            bg=BG_CARD,
            fg=TEXT,
            selectbackground=PRIMARY,
            selectforeground="white",
            highlightthickness=1,
            highlightbackground=BORDER,
            bd=0,
            height=10,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.configure(command=self.listbox.yview)

        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Button row
        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(12, 0))

        ttk.Button(
            btn_row, text="Add Resume", style="Primary.TButton",
            command=self._add_resume,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Remove Selected", style="Danger.TButton",
            command=self._remove_resume,
        ).pack(side="left")

        # Note
        ttk.Label(
            self,
            text="Minimum 1 resume required. Supported formats: PDF, DOCX.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(8, PAD_Y))

        # Populate from saved state
        self._refresh_list()

    def on_show(self) -> None:
        """Refresh list when step is shown (may have changed externally)."""
        self._refresh_list()

    def _refresh_list(self) -> None:
        """Sync the listbox with wizard.resume_list."""
        self.listbox.delete(0, tk.END)
        for label, path in self.wizard.resume_list:
            filename = Path(path).name
            self.listbox.insert(tk.END, f"  {label}  --  {filename}")

    def _add_resume(self) -> None:
        """Open file dialog, prompt for label, add to list."""
        path = filedialog.askopenfilename(
            title="Select Resume",
            filetypes=[
                ("Resume files", "*.pdf *.docx"),
                ("PDF files", "*.pdf"),
                ("Word documents", "*.docx"),
                ("All files", "*.*"),
            ],
            parent=self.wizard,
        )
        if not path:
            return

        # Suggest a label from the filename
        stem = Path(path).stem.replace("_", " ").replace("-", " ").title()
        label = simpledialog.askstring(
            "Resume Label",
            "Enter a label for this resume (e.g., 'Data Analyst', 'Backend Dev'):",
            initialvalue=stem,
            parent=self.wizard,
        )
        if not label:
            return

        # Normalize label for use as a key
        label_key = label.strip()
        if not label_key:
            return

        # Check for duplicate labels
        existing_labels = [lbl for lbl, _ in self.wizard.resume_list]
        if label_key in existing_labels:
            messagebox.showwarning(
                "Duplicate Label",
                f"A resume with the label '{label_key}' already exists.\n"
                "Please use a different label.",
                parent=self.wizard,
            )
            return

        self.wizard.resume_list.append((label_key, path))
        self._refresh_list()

    def _remove_resume(self) -> None:
        """Remove the selected resume from the list."""
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showinfo(
                "No Selection",
                "Please select a resume to remove.",
                parent=self.wizard,
            )
            return

        index = selection[0]
        label, _ = self.wizard.resume_list[index]
        confirm = messagebox.askyesno(
            "Remove Resume",
            f"Remove '{label}' from the list?",
            parent=self.wizard,
        )
        if confirm:
            self.wizard.resume_list.pop(index)
            self._refresh_list()

    def validate(self) -> bool:
        """At least one resume is required."""
        if not self.wizard.resume_list:
            messagebox.showwarning(
                "No Resumes",
                "Please add at least one resume before continuing.",
                parent=self.wizard,
            )
            return False
        return True
