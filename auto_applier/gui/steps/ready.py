"""Step 6: Ready to run — AC theme."""

from __future__ import annotations

import json
import os
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from auto_applier.gui.styles import (
    SANDY_SHORE, CREAM, DRIFTWOOD, WARM_WHITE,
    SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY,
    BORDER_LIGHT, NOOK_GREEN_DARK, INFO_BLUE, ERROR_RED,
    HEADING_FONT, BODY_FONT,
)

if TYPE_CHECKING:
    from auto_applier.gui.wizard import WizardApp


class ReadyStep(tk.Frame):
    def __init__(self, parent: tk.Widget, wizard: WizardApp) -> None:
        super().__init__(parent, bg=SANDY_SHORE)
        self.wizard = wizard

        self.grid_columnconfigure(0, weight=1)

        header_row = tk.Frame(self, bg=SANDY_SHORE)
        header_row.grid(row=0, column=0, sticky="ew", padx=40, pady=(16, 4))
        tk.Label(header_row, text="You're All Set!", font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=SANDY_SHORE).pack(side="left")
        ttk.Button(header_row, text="← Edit Settings", style="Ghost.TButton", command=lambda: wizard.go_to_step(1)).pack(side="right")

        tk.Label(self, text="Review your configuration below, then choose how to proceed.", font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE, anchor="w").grid(row=1, column=0, sticky="nw", padx=40, pady=(0, 6))

        self.card = tk.Frame(self, bg=CREAM, highlightbackground=BORDER_LIGHT, highlightthickness=1, padx=16, pady=8)
        self.card.grid(row=2, column=0, sticky="ew", padx=40, pady=(0, 10))

        self.summary_labels: list[tuple[tk.Label, tk.Label]] = []
        for i, field in enumerate(["Platforms", "Resume", "Name", "Phone", "City", "LinkedIn", "Keywords", "Job Location"]):
            k = tk.Label(self.card, text=field + ":", font=(BODY_FONT, 9, "bold"), fg=BARK_BROWN, bg=CREAM, anchor="e", width=12)
            k.grid(row=i, column=0, sticky="e", padx=(0, 8), pady=1)
            v = tk.Label(self.card, text="—", font=(BODY_FONT, 9), fg=SOIL_BROWN, bg=CREAM, anchor="w", wraplength=380)
            v.grid(row=i, column=1, sticky="w", pady=1)
            self.summary_labels.append((k, v))

        ttk.Separator(self, orient="horizontal").grid(row=3, column=0, sticky="ew", padx=40, pady=(0, 10))

        btn_row = tk.Frame(self, bg=SANDY_SHORE)
        btn_row.grid(row=4, column=0, pady=(0, 4))
        ttk.Button(btn_row, text="Run (Apply to Jobs)", style="Primary.TButton", command=self._run_live).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Dry Run", style="Secondary.TButton", command=self._run_dry).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Exit", style="Danger.TButton", command=self._exit).pack(side="left")

        self.status_label = tk.Label(self, text="", font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=SANDY_SHORE)
        self.status_label.grid(row=5, column=0, pady=(4, 8))

    def on_show(self):
        d = self.wizard.data
        names = {"linkedin": "LinkedIn", "indeed": "Indeed", "dice": "Dice", "ziprecruiter": "ZipRecruiter"}
        enabled = self.wizard.get_enabled_platforms()
        vals = [
            ", ".join(names.get(k, k) for k in enabled) or "None",
            os.path.basename(d["resume_path"].get()) or "—",
            f"{d['first_name'].get()} {d['last_name'].get()}".strip() or "—",
            d["phone"].get() or "—", d["city"].get() or "—",
            d["linkedin"].get() or "—", d["keywords"].get() or "—", d["location"].get() or "—",
        ]
        for (_, v_lbl), val in zip(self.summary_labels, vals):
            v_lbl.configure(text=val[:55] + "..." if len(val) > 55 else val)

    def _save_config(self):
        from auto_applier.config import PROJECT_ROOT, RESUMES_DIR, USER_CONFIG_FILE
        d = self.wizard.data
        enabled = self.wizard.get_enabled_platforms()

        with open(PROJECT_ROOT / ".env", "w") as f:
            for key in enabled:
                prefix = key.upper()
                f.write(f"{prefix}_EMAIL={d[f'{key}_email'].get()}\n")
                f.write(f"{prefix}_PASSWORD={d[f'{key}_password'].get()}\n")

        resume_src = d["resume_path"].get()
        config = {}
        if resume_src and os.path.exists(resume_src):
            src = Path(resume_src).resolve()
            dest = RESUMES_DIR / src.name
            if src != dest.resolve():
                shutil.copy2(resume_src, dest)
            config["resume_path"] = str(dest)
        else:
            config["resume_path"] = resume_src

        platforms_config = {key: {"email": d[f"{key}_email"].get()} for key in enabled}
        config.update({
            "enabled_platforms": enabled, "platforms": platforms_config,
            "first_name": d["first_name"].get(), "last_name": d["last_name"].get(),
            "phone": d["phone"].get(), "city": d["city"].get(),
            "linkedin": d["linkedin"].get(), "website": d["website"].get(),
            "search_keywords": [k.strip() for k in d["keywords"].get().split(",") if k.strip()],
            "location": d["location"].get(),
        })
        with open(USER_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    def _run_live(self):
        self._save_config()
        self.status_label.configure(text="Configuration saved! Launching dashboard...", fg=NOOK_GREEN_DARK)
        self.wizard.root.after(300, self._launch_dashboard, False)

    def _run_dry(self):
        self._save_config()
        self.status_label.configure(text="Configuration saved! Launching dashboard...", fg=INFO_BLUE)
        self.wizard.root.after(300, self._launch_dashboard, True)

    def _launch_dashboard(self, dry_run):
        from auto_applier.gui.dashboard import DashboardWindow
        from auto_applier.main import load_user_config
        config = load_user_config()
        enabled = config.get("enabled_platforms", ["linkedin"])
        dashboard = DashboardWindow(self.wizard.root, enabled, dry_run=dry_run)
        dashboard.start_run(config, dry_run)

    def _exit(self):
        self.wizard.root.destroy()
