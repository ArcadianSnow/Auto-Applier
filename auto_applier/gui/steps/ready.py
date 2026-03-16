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

        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # ── Header row ──────────────────────────────────────────
        header_row = tk.Frame(self, bg="#F5F7FA")
        header_row.grid(row=0, column=0, sticky="ew", padx=40, pady=(16, 4))

        tk.Label(
            header_row, text="You're All Set!",
            font=("Segoe UI", 14, "bold"), fg="#1E293B", bg="#F5F7FA",
        ).pack(side="left")

        ttk.Button(
            header_row, text="← Edit Settings",
            style="Ghost.TButton", command=lambda: wizard.go_to_step(1),
        ).pack(side="right")

        tk.Label(
            self,
            text="Review your configuration below, then choose how to proceed.",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
            anchor="w",
        ).grid(row=1, column=0, sticky="nw", padx=40, pady=(0, 6))

        # ── Summary card ────────────────────────────────────────
        self.card = tk.Frame(
            self, bg="#FFFFFF",
            highlightbackground="#E2E8F0", highlightthickness=1,
            padx=16, pady=8,
        )
        self.card.grid(row=2, column=0, sticky="ew", padx=40, pady=(0, 10))

        self.summary_labels: list[tuple[tk.Label, tk.Label]] = []
        fields = [
            "Platforms", "Resume", "Name", "Phone",
            "City", "LinkedIn", "Keywords", "Job Location",
        ]
        for i, field in enumerate(fields):
            key_lbl = tk.Label(
                self.card, text=field + ":",
                font=("Segoe UI", 9, "bold"), fg="#374151", bg="#FFFFFF",
                anchor="e", width=12,
            )
            key_lbl.grid(row=i, column=0, sticky="e", padx=(0, 8), pady=1)

            val_lbl = tk.Label(
                self.card, text="—",
                font=("Segoe UI", 9), fg="#1E293B", bg="#FFFFFF",
                anchor="w", wraplength=380,
            )
            val_lbl.grid(row=i, column=1, sticky="w", pady=1)

            self.summary_labels.append((key_lbl, val_lbl))

        # ── Separator ───────────────────────────────────────────
        ttk.Separator(self, orient="horizontal").grid(
            row=3, column=0, sticky="ew", padx=40, pady=(0, 10),
        )

        # ── Action buttons ──────────────────────────────────────
        btn_row = tk.Frame(self, bg="#F5F7FA")
        btn_row.grid(row=4, column=0, pady=(0, 4))

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

        # ── Status label ────────────────────────────────────────
        self.status_label = tk.Label(
            self, text="",
            font=("Segoe UI", 10), fg="#64748B", bg="#F5F7FA",
        )
        self.status_label.grid(row=5, column=0, pady=(4, 8))

    def on_show(self) -> None:
        """Refresh summary values from wizard data."""
        d = self.wizard.data

        # Build platform list
        platform_names = {
            "linkedin": "LinkedIn", "indeed": "Indeed",
            "dice": "Dice", "ziprecruiter": "ZipRecruiter",
        }
        enabled = self.wizard.get_enabled_platforms()
        platforms_str = ", ".join(platform_names.get(k, k) for k in enabled) or "None"

        resume = os.path.basename(d["resume_path"].get()) or "—"
        name = f"{d['first_name'].get()} {d['last_name'].get()}".strip() or "—"

        values = [
            platforms_str,
            resume,
            name,
            d["phone"].get() or "—",
            d["city"].get() or "—",
            d["linkedin"].get() or "—",
            d["keywords"].get() or "—",
            d["location"].get() or "—",
        ]

        for (_, val_lbl), val in zip(self.summary_labels, values):
            display = val if len(val) <= 55 else val[:52] + "..."
            val_lbl.configure(text=display)

    def _save_config(self) -> None:
        """Save all wizard data to disk."""
        from auto_applier.config import PROJECT_ROOT, RESUMES_DIR, USER_CONFIG_FILE

        d = self.wizard.data
        enabled = self.wizard.get_enabled_platforms()

        # Save credentials to .env (one pair per enabled platform)
        env_path = PROJECT_ROOT / ".env"
        with open(env_path, "w") as f:
            for key in enabled:
                email = d[f"{key}_email"].get()
                password = d[f"{key}_password"].get()
                prefix = key.upper()
                f.write(f"{prefix}_EMAIL={email}\n")
                f.write(f"{prefix}_PASSWORD={password}\n")

        # Copy resume to data/resumes/ if it's not already there
        resume_src = d["resume_path"].get()
        config = {}
        if resume_src and os.path.exists(resume_src):
            src_path = Path(resume_src).resolve()
            dest = RESUMES_DIR / src_path.name
            if src_path != dest.resolve():
                shutil.copy2(resume_src, dest)
            config["resume_path"] = str(dest)
        else:
            config["resume_path"] = resume_src

        # Build platform-specific config (emails only, passwords in .env)
        platforms_config = {}
        for key in enabled:
            platforms_config[key] = {"email": d[f"{key}_email"].get()}

        config.update({
            "enabled_platforms": enabled,
            "platforms": platforms_config,
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
        """Launch the main application loop in a new console that stays open."""
        import subprocess
        import sys

        py = sys.executable
        run_cmd = f'"{py}" -m auto_applier --cli run'
        if dry_run:
            run_cmd += " --dry-run"

        if os.name == "nt":
            subprocess.Popen(
                f'cmd /k "{run_cmd}"',
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=str(Path(__file__).parent.parent.parent.parent),
            )
        else:
            subprocess.Popen(
                ["bash", "-c", f'{run_cmd}; echo "\\nPress Enter to close..."; read'],
                cwd=str(Path(__file__).parent.parent.parent.parent),
            )

    def _exit(self) -> None:
        self.wizard.root.destroy()
