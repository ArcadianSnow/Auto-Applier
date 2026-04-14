"""Application outcome tracker panel.

GUI version of `cli respond` and `cli auto-ghost`. Shows applied
jobs with their current outcome state and lets the user update via
buttons (interview / rejected / offer / etc.) rather than typing
job IDs at a terminal.

Includes an Auto-mark-old-as-ghosted button that runs the same
30-day stale check from CLI.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, BORDER, PRIMARY, ACCENT, WARNING, DANGER,
    TEXT, TEXT_LIGHT, TEXT_MUTED,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)


_OUTCOME_DISPLAY = {
    "pending":      ("Pending",      TEXT_MUTED),
    "acknowledged": ("Acknowledged", TEXT_LIGHT),
    "interview":    ("Interview",    PRIMARY),
    "offer":        ("Offer",        ACCENT),
    "rejected":     ("Rejected",     DANGER),
    "ghosted":      ("Ghosted",      TEXT_MUTED),
    "withdrawn":    ("Withdrawn",    TEXT_LIGHT),
}


class OutcomeTrackerPanel(tk.Toplevel):
    """List submitted applications with buttons to record outcomes."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.title("Track application outcomes")
        self.configure(bg=BG)
        self.geometry("820x680")
        self.minsize(640, 480)

        self._filter_var = tk.StringVar(value="all")
        self._build_ui()
        self.after(50, self._reload)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=BG_CARD, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)

        ttk.Label(
            header, text="Track application outcomes",
            style="CardHeading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(12, 0))

        ttk.Label(
            header, text=(
                "Update what happened after each application. Old pending "
                "ones can be auto-marked as ghosted."
            ),
            style="CardSmall.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(2, 12))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        ctrl_row = tk.Frame(self, bg=BG)
        ctrl_row.pack(fill="x", padx=PAD_X, pady=(PAD_Y, 0))

        ttk.Label(ctrl_row, text="Filter:", style="TLabel").pack(side="left")
        filter_combo = ttk.Combobox(
            ctrl_row, textvariable=self._filter_var,
            values=[
                "all", "pending", "acknowledged", "interview", "offer",
                "rejected", "ghosted", "withdrawn",
            ],
            state="readonly", width=14,
        )
        filter_combo.pack(side="left", padx=(8, 16))
        filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload())

        ttk.Button(
            ctrl_row, text="Refresh", command=self._reload,
        ).pack(side="left")

        ttk.Button(
            ctrl_row, text="Auto-mark stale as ghosted (30+ days)",
            command=self._auto_ghost,
        ).pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        self._canvas, self._inner = make_scrollable(body)

        self._status_label = ttk.Label(
            self, text="Loading...", style="Muted.TLabel",
        )
        self._status_label.pack(side="bottom", anchor="w", padx=PAD_X, pady=(0, 8))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        from auto_applier.storage.models import Application, Job
        from auto_applier.storage.repository import load_all

        for child in self._inner.winfo_children():
            child.destroy()

        apps = load_all(Application)
        jobs = {j.job_id: j for j in load_all(Job)}

        # Only submitted rows have meaningful outcomes
        submitted = [a for a in apps if a.status in ("applied", "dry_run")]

        f = self._filter_var.get()
        if f != "all":
            submitted = [a for a in submitted if (a.outcome or "pending") == f]

        if not submitted:
            ttk.Label(
                self._inner,
                text=(
                    "No submitted applications match this filter.\n\n"
                    "Apply to some jobs first, then come back to track "
                    "responses."
                ),
                style="Small.TLabel",
                justify="left",
            ).pack(anchor="w", pady=PAD_Y)
            self._status_label.configure(text="0 applications")
            return

        # Sort by applied_at descending (most recent first)
        submitted.sort(
            key=lambda a: a.applied_at or "", reverse=True,
        )

        for app in submitted:
            job = jobs.get(app.job_id)
            self._render_row(app, job)

        # Outcome breakdown summary
        from auto_applier.analysis.outcome import outcome_summary
        summary = outcome_summary()
        breakdown = "  ".join(
            f"{k}={v}" for k, v in summary.items() if v
        ) or "no outcomes recorded yet"
        self._status_label.configure(
            text=f"{len(submitted)} shown  |  {breakdown}",
        )

    def _render_row(self, app, job) -> None:
        outcome = app.outcome or "pending"
        display_name, color = _OUTCOME_DISPLAY.get(
            outcome, (outcome.title(), TEXT_LIGHT),
        )

        card = tk.Frame(self._inner, bg=BG_CARD, bd=1, relief="solid",
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=4, padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=12, pady=10)

        # Top row: outcome badge + title + date
        top = tk.Frame(inner, bg=BG_CARD)
        top.pack(fill="x")

        badge = tk.Label(
            top, text=f" {display_name} ", bg=color, fg="white",
            font=FONT_BUTTON, padx=4,
        )
        badge.pack(side="left")

        title_text = (job.title if job and job.title else app.job_id)[:75]
        ttk.Label(
            top, text=title_text, style="CardSubheading.TLabel",
        ).pack(side="left", padx=(8, 0))

        company = (job.company if job and job.company else "(unknown)")[:50]
        applied_at = (app.applied_at or "")[:10]
        ttk.Label(
            inner, text=f"@ {company}   applied {applied_at}",
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        if app.outcome_note:
            ttk.Label(
                inner, text=f"Note: {app.outcome_note}",
                style="CardSmall.TLabel",
            ).pack(anchor="w", pady=(2, 0))

        # Buttons
        btns = tk.Frame(inner, bg=BG_CARD)
        btns.pack(fill="x", pady=(8, 0))

        if job and job.url:
            ttk.Button(
                btns, text="Open job",
                command=lambda u=job.url: webbrowser.open(u),
            ).pack(side="left")

        ttk.Button(
            btns, text="Interview",
            style="Primary.TButton",
            command=lambda a=app: self._set_outcome(a, "interview"),
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            btns, text="Rejected",
            command=lambda a=app: self._set_outcome(a, "rejected"),
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            btns, text="Offer",
            style="Accent.TButton",
            command=lambda a=app: self._set_outcome(a, "offer"),
        ).pack(side="left", padx=(8, 0))

        # Other states behind a "More" submenu-style row
        more_row = tk.Frame(inner, bg=BG_CARD)
        more_row.pack(fill="x", pady=(4, 0))
        for state in ("acknowledged", "ghosted", "withdrawn", "pending"):
            ttk.Button(
                more_row, text=state.title(),
                command=lambda a=app, s=state: self._set_outcome(a, s),
            ).pack(side="left", padx=(0, 4))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _set_outcome(self, app, state: str) -> None:
        from auto_applier.analysis.outcome import set_outcome
        try:
            result = set_outcome(
                job_id=app.job_id,
                outcome=state,
                source=app.source,
            )
        except ValueError as exc:
            messagebox.showwarning("Invalid outcome", str(exc))
            return

        if result is None:
            messagebox.showwarning(
                "Not found",
                f"Couldn't update outcome for job {app.job_id}.",
            )
            return

        self._status_label.configure(
            text=f"Set '{state}' for {app.job_id}",
        )
        self._reload()

    def _auto_ghost(self) -> None:
        from auto_applier.analysis.outcome import auto_mark_ghosted
        try:
            count = auto_mark_ghosted()
        except Exception as exc:
            messagebox.showwarning("Auto-ghost failed", str(exc))
            return

        if count:
            messagebox.showinfo(
                "Auto-marked as ghosted",
                f"Marked {count} application(s) as ghosted "
                "(no response in 30+ days).",
            )
        else:
            messagebox.showinfo(
                "Nothing to ghost",
                "No applications are old enough to mark as ghosted yet.",
            )
        self._reload()


# Re-export FONT_BUTTON for convenience (tk.Label uses font tuples)
from auto_applier.gui.styles import FONT_BUTTON  # noqa: E402
