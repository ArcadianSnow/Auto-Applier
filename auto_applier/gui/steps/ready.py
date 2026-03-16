"""Step 6: Ready to run — summary and action buttons."""

from __future__ import annotations

import json
import os
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class ReadyStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg="#F5F7FA")
        self.wizard = wizard

        content = tk.Frame(self, bg="#F5F7FA")
        content.pack(padx=40, pady=20, fill="both", expand=True)

        # Header row with edit button
        header_row = tk.Frame(content, bg="#F5F7FA")
        header_row.pack(fill="x", pady=(0, 4))

        tk.Label(
            header_row, text="You're All Set!",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(side="left")

        ttk.Button(
            header_row, text="← Edit Settings",
            style="Ghost.TButton", command=lambda: wizard.go_to_step(1),
        ).pack(side="right")

        tk.Label(
            content,
            text="Review your configuration below, then choose how to proceed.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        ).pack(anchor="w", pady=(0, 12))

        # Summary card
        self.card = tk.Frame(
            content, bg="#FFFFFF",
            highlightbackground="#E2E8F0", highlightthickness=1,
            padx=20, pady=14,
        )
        self.card.pack(fill="x", pady=(0, 16))

        # Will be populated in on_show()
        self.summary_labels: list[tuple[tk.Label, tk.Label]] = []
        fields = [
            "Email", "Resume", "Name", "Phone",
            "City", "LinkedIn", "Keywords", "Job Location",
        ]
        for i, field in enumerate(fields):
            key_lbl = tk.Label(
                self.card, text=field + ":",
                font=("Segoe UI", 10, "bold"), fg="#374151", bg="#FFFFFF",
                anchor="e", width=14,
            )
            key_lbl.grid(row=i, column=0, sticky="e", padx=(0, 10), pady=2)

            val_lbl = tk.Label(
                self.card, text="—",
                font=("Segoe UI", 10), fg="#1E293B", bg="#FFFFFF",
                anchor="w", wraplength=360,
            )
            val_lbl.grid(row=i, column=1, sticky="w", pady=2)

            self.summary_labels.append((key_lbl, val_lbl))

        # Separator
        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=(0, 16))

        # Action buttons
        btn_row = tk.Frame(content, bg="#F5F7FA")
        btn_row.pack()

        ttk.Button(
            btn_row, text="Run (Apply to Jobs)",
            style="Primary.TButton", command=self._run_live,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Dry Run",
            style="Secondary.TButton", command=self._run_dry,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Exit",
            style="Danger.TButton", command=self._exit,
        ).pack(side="left")

        # Status label
        self.status_label = tk.Label(
            content, text="",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        )
        self.status_label.pack(pady=(12, 0))

    def on_show(self) -> None:
        """Refresh summary values from wizard data."""
        d = self.wizard.data

        email = d["email"].get()
        if "@" in email:
            parts = email.split("@")
            masked = parts[0][:3] + "***@" + parts[1]
        else:
            masked = email

        resume = os.path.basename(d["resume_path"].get()) or "—"
        name = f"{d['first_name'].get()} {d['last_name'].get()}".strip() or "—"

        values = [
            masked,
            resume,
            name,
            d["phone"].get() or "—",
            d["city"].get() or "—",
            d["linkedin"].get() or "—",
            d["keywords"].get() or "—",
            d["location"].get() or "—",
        ]

        for (_, val_lbl), val in zip(self.summary_labels, values):
            display = val if len(val) <= 50 else val[:47] + "..."
            val_lbl.configure(text=display)

    def _save_config(self) -> None:
        """Save all wizard data to disk."""
        from auto_applier.config import DATA_DIR, PROJECT_ROOT, RESUMES_DIR, USER_CONFIG_FILE

        d = self.wizard.data

        # Save .env
        env_path = PROJECT_ROOT / ".env"
        with open(env_path, "w") as f:
            f.write(f"LINKEDIN_EMAIL={d['email'].get()}\n")
            f.write(f"LINKEDIN_PASSWORD={d['password'].get()}\n")

        # Copy resume if it's a real path
        resume_src = d["resume_path"].get()
        config = {}
        if resume_src and not resume_src.startswith("(dummy)") and os.path.exists(resume_src):
            dest = RESUMES_DIR / os.path.basename(resume_src)
            shutil.copy2(resume_src, dest)
            config["resume_path"] = str(dest)
        else:
            config["resume_path"] = resume_src

        # Build config
        config.update({
            "email": d["email"].get(),
            "first_name": d["first_name"].get(),
            "last_name": d["last_name"].get(),
            "phone": d["phone"].get(),
            "city": d["city"].get(),
            "linkedin": d["linkedin"].get(),
            "website": d["website"].get(),
            "search_keywords": [k.strip() for k in d["keywords"].get().split(",") if k.strip()],
            "location": d["location"].get(),
        })

        with open(USER_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    def _run_live(self) -> None:
        self._save_config()
        self.status_label.configure(
            text="Configuration saved! Starting job applications...",
            fg="#10B981",
        )
        self.wizard.root.after(500, self._launch_run, False)

    def _run_dry(self) -> None:
        self._save_config()
        self.status_label.configure(
            text="Configuration saved! Starting dry run...",
            fg="#2563EB",
        )
        self.wizard.root.after(500, self._launch_run, True)

    def _launch_run(self, dry_run: bool) -> None:
        """Launch the main application loop."""
        import subprocess
        import sys

        cmd = [sys.executable, "-m", "auto_applier", "run"]
        if dry_run:
            cmd.append("--dry-run")

        # Open in a new console window so user can see progress
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
            cwd=str(Path(__file__).parent.parent.parent.parent),
        )

    def _exit(self) -> None:
        self.wizard.root.destroy()
