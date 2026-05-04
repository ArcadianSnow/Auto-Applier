"""'Almost' panel — surface high-score jobs that need manual application.

Mirrors the `cli almost` command: lists jobs that scored well during
a run but were skipped because they require applying on the company's
own website. Each entry shows the URL, recommended resume, and a
button to generate a tailored cover letter.

Designed as a Toplevel popup so it doesn't compete with the dashboard
or the wizard for screen real estate.
"""
from __future__ import annotations

import asyncio
import threading
import tkinter as tk
import webbrowser
from collections import defaultdict
from pathlib import Path
from tkinter import ttk, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, BORDER, PRIMARY, TEXT, TEXT_LIGHT, TEXT_MUTED,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_BUTTON,
    FONT_SMALL, FONT_MONO,
    PAD_X, PAD_Y, make_scrollable,
)


class AlmostPanel(tk.Toplevel):
    """Window listing high-score skipped jobs grouped by recommended resume.

    Reads from applications.csv (skipped status with score >= min_score)
    and joins to jobs.csv for titles/companies/URLs. Lazy-loads —
    constructor returns immediately, data fetched on idle.
    """

    DEFAULT_MIN_SCORE = 8

    def __init__(self, parent: tk.Misc, min_score: int = DEFAULT_MIN_SCORE) -> None:
        super().__init__(parent)
        self.min_score = min_score
        self._job_lookup: dict[str, dict] = {}  # job_id -> Job dict snapshot
        # Show-all toggle: panel renders top 50 by default for speed,
        # full list on demand. Reset to False on each fresh load.
        self._show_all: bool = False

        self.title("Jobs to apply manually")
        self.configure(bg=BG)
        self.geometry("780x620")
        self.resizable(True, True)
        self.minsize(560, 420)

        self._build_ui()
        # Defer load until window is shown so the panel paints quickly
        self.after(50, self._load_data)

        # Modal behavior — without this, Windows happily hides the
        # popup behind the dashboard the moment the user clicks it.
        # Mirror the pattern used by JobReviewPanel.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Header
        header = tk.Frame(self, bg=BG_CARD, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)

        title_lbl = ttk.Label(
            header,
            text="Good jobs you should apply to manually",
            style="CardHeading.TLabel",
        )
        title_lbl.pack(anchor="w", padx=PAD_X, pady=(12, 0))

        sub = ttk.Label(
            header,
            text=(
                f"Score >= {self.min_score}, but the company wants you to "
                "apply on their own website."
            ),
            style="CardSmall.TLabel",
        )
        sub.pack(anchor="w", padx=PAD_X, pady=(2, 12))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        # Filter row
        filter_row = tk.Frame(self, bg=BG)
        filter_row.pack(fill="x", padx=PAD_X, pady=(PAD_Y, 0))

        ttk.Label(filter_row, text="Min score:", style="TLabel").pack(side="left")
        self._min_score_var = tk.IntVar(value=self.min_score)
        spin = ttk.Spinbox(
            filter_row,
            from_=1, to=10, width=4,
            textvariable=self._min_score_var,
            command=self._on_min_score_change,
        )
        spin.pack(side="left", padx=(8, 16))

        ttk.Button(
            filter_row, text="Refresh",
            command=self._load_data,
        ).pack(side="left")

        ttk.Button(
            filter_row, text="Open cover letters folder",
            command=self._open_cover_letters_folder,
        ).pack(side="right")

        # Body — scrollable list
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        self._list_canvas, self._list_inner = make_scrollable(body)

        # Footer status
        self._status_label = ttk.Label(
            self, text="Loading...", style="Muted.TLabel",
        )
        self._status_label.pack(side="bottom", anchor="w", padx=PAD_X, pady=(0, 8))

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _on_min_score_change(self) -> None:
        try:
            self.min_score = int(self._min_score_var.get())
        except (ValueError, tk.TclError):
            self.min_score = self.DEFAULT_MIN_SCORE
        self._load_data()

    def _load_data(self) -> None:
        """Refresh the job list from storage."""
        from auto_applier.storage.models import Application, Job
        from auto_applier.storage.repository import load_all

        # Clear previous rows
        for child in self._list_inner.winfo_children():
            child.destroy()

        apps = load_all(Application)
        jobs = {j.job_id: j for j in load_all(Job)}
        self._job_lookup = {jid: j for jid, j in jobs.items()}

        raw_candidates = [
            a for a in apps
            if a.status == "skipped"
            and a.score >= self.min_score
            and a.resume_used
        ]
        # Dedupe by job_id — keep the most recent Application per
        # job. The same job_id can have multiple skipped rows from
        # repeated runs, and rendering 9 copies of one MongoDB job
        # is both wrong AND slow (each row spawns ~12 widgets, so
        # 800 dupes = 10k widget creations and 5+ seconds of Tk lag).
        raw_candidates.sort(key=lambda a: a.applied_at or "", reverse=True)
        _seen: set[str] = set()
        candidates: list = []
        for a in raw_candidates:
            if a.job_id in _seen:
                continue
            _seen.add(a.job_id)
            candidates.append(a)

        if not candidates:
            ttk.Label(
                self._list_inner,
                text=(
                    f"No jobs scoring {self.min_score}+ have been skipped "
                    "yet.\n\nRun some applications first, then come back."
                ),
                style="Small.TLabel",
                justify="left",
            ).pack(anchor="w", pady=PAD_Y)
            self._status_label.configure(text="0 jobs")
            return

        # Sort by score desc, then by company
        candidates.sort(key=lambda a: (
            -a.score,
            (jobs.get(a.job_id).company if a.job_id in jobs else "").lower(),
        ))

        # Group by recommended resume
        # Cap rendered rows. Each row creates ~12 Tk widgets; an
        # uncapped run with 200+ unique candidates spawned 2400+
        # widgets and blocked the UI thread for 5+ seconds. The
        # cap keeps the panel snappy; a "Show all" button reveals
        # the rest on demand.
        DEFAULT_ROW_CAP = 50
        total_count = len(candidates)
        showing = candidates if self._show_all else candidates[:DEFAULT_ROW_CAP]

        by_resume: dict[str, list] = defaultdict(list)
        for app in showing:
            by_resume[app.resume_used].append(app)

        for resume_label in sorted(by_resume):
            group_header = tk.Frame(self._list_inner, bg=BG)
            group_header.pack(fill="x", pady=(PAD_Y, 4))
            ttk.Label(
                group_header,
                text=f"Use resume: {resume_label}",
                style="Subheading.TLabel",
            ).pack(anchor="w")

            for app in by_resume[resume_label]:
                self._render_job_card(self._list_inner, app, jobs.get(app.job_id))

        # If we capped, show a button to load the rest. Helps on
        # systems with hundreds of high-score skipped jobs without
        # blocking the panel open for users with normal counts.
        if not self._show_all and total_count > DEFAULT_ROW_CAP:
            more_btn_row = tk.Frame(self._list_inner, bg=BG)
            more_btn_row.pack(fill="x", pady=(PAD_Y, 0))
            remaining = total_count - DEFAULT_ROW_CAP
            ttk.Button(
                more_btn_row,
                text=f"Show all ({remaining} more)",
                command=self._load_all_rows,
            ).pack(anchor="w")

        if self._show_all or total_count <= DEFAULT_ROW_CAP:
            self._status_label.configure(
                text=f"{total_count} job(s) shown across {len(by_resume)} resume(s)",
            )
        else:
            self._status_label.configure(
                text=(
                    f"Showing top {len(showing)} of {total_count} job(s) "
                    f"across {len(by_resume)} resume(s) — click \"Show all\" "
                    f"below to render the rest."
                ),
            )

    def _load_all_rows(self) -> None:
        """Re-render the panel with the full candidate list (no
        50-row cap). Used by the "Show all (N more)" button."""
        self._show_all = True
        self._load_data()

    def _render_job_card(self, parent: tk.Widget, app, job) -> None:
        """Render a single job entry with action buttons."""
        card = tk.Frame(parent, bg=BG_CARD, bd=1, relief="solid",
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=4, padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=12, pady=10)

        # Score badge + title
        top_row = tk.Frame(inner, bg=BG_CARD)
        top_row.pack(fill="x")

        score_lbl = tk.Label(
            top_row,
            text=f" {app.score} ",
            bg=PRIMARY, fg="white",
            font=FONT_BUTTON, padx=4,
        )
        score_lbl.pack(side="left")

        title_text = (job.title if job and job.title else app.job_id)[:80]
        ttk.Label(
            top_row,
            text=title_text,
            style="CardSubheading.TLabel",
        ).pack(side="left", padx=(8, 0))

        company = (job.company if job and job.company else "(unknown company)")[:60]
        ttk.Label(
            inner,
            text=f"@ {company}",
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        if app.failure_reason:
            ttk.Label(
                inner,
                text=app.failure_reason,
                style="CardSmall.TLabel",
            ).pack(anchor="w", pady=(2, 0))

        # Action buttons
        btn_row = tk.Frame(inner, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(8, 0))

        url = job.url if job else ""
        if url:
            ttk.Button(
                btn_row, text="Open job page",
                command=lambda u=url: webbrowser.open(u),
            ).pack(side="left")

        ttk.Button(
            btn_row, text="Generate cover letter",
            command=lambda jid=app.job_id, rl=app.resume_used:
                self._generate_cover_letter(jid, rl),
        ).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _generate_cover_letter(self, job_id: str, resume_label: str) -> None:
        """Kick off cover letter generation in a background thread.

        The LLM call takes 10-30 seconds on local Ollama, so we don't
        want to block the UI thread. Disable button while running,
        re-enable on completion (or failure).
        """
        self._status_label.configure(
            text=f"Generating cover letter for {job_id}...",
        )

        def _worker() -> None:
            from auto_applier.llm.router import LLMRouter
            from auto_applier.resume.cover_letter_service import (
                generate_cover_letter,
            )
            from auto_applier.resume.manager import ResumeManager

            async def run() -> tuple[bool, str, str]:
                router = LLMRouter()
                await router.initialize()
                rm = ResumeManager(router)
                result = await generate_cover_letter(
                    job_id=job_id,
                    router=router,
                    resume_manager=rm,
                    preferred_resume=resume_label,
                )
                if result is None:
                    return (False, "Job not found in storage.", "")
                if not result.letter:
                    return (False, "AI couldn't generate a letter (LLM down?).", "")
                return (True, str(result.file_path or "(not saved)"), result.letter)

            try:
                ok, info, letter_text = asyncio.run(run())
            except Exception as exc:
                ok, info, letter_text = False, f"Error: {exc}", ""

            # Marshal back to UI thread
            self.after(
                0,
                lambda: self._on_cover_letter_done(ok, info, letter_text),
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cover_letter_done(self, ok: bool, info: str, letter_text: str) -> None:
        if not ok:
            self._status_label.configure(text="Cover letter failed.")
            messagebox.showwarning(
                "Cover letter generation failed",
                info,
            )
            return

        self._status_label.configure(text=f"Cover letter saved: {info}")
        # Show preview in a small popup so the user can copy directly
        self._show_letter_preview(info, letter_text)

    def _show_letter_preview(self, file_path: str, letter_text: str) -> None:
        win = tk.Toplevel(self)
        win.title("Cover letter preview")
        win.configure(bg=BG)
        win.geometry("700x520")
        win.resizable(True, True)
        win.minsize(500, 400)

        ttk.Label(
            win, text="Cover letter (saved to disk, also shown below)",
            style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            win, text=file_path,
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X)

        text_frame = tk.Frame(win, bg=BG)
        text_frame.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        text = tk.Text(
            text_frame, wrap="word", font=FONT_BODY,
            bg=BG_CARD, fg=TEXT, bd=1, relief="solid", padx=12, pady=12,
            highlightbackground=BORDER,
        )
        sb = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", letter_text)
        text.configure(state="disabled")  # read-only

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        ttk.Button(
            btns, text="Copy to clipboard",
            command=lambda: self._copy_to_clipboard(letter_text),
        ).pack(side="left")
        ttk.Button(
            btns, text="Close",
            command=win.destroy,
        ).pack(side="right")

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # required for clipboard to stick on Windows
            self._status_label.configure(text="Cover letter copied to clipboard.")
        except Exception as exc:
            self._status_label.configure(text=f"Copy failed: {exc}")

    def _open_cover_letters_folder(self) -> None:
        """Open the data/cover_letters folder in the system file browser."""
        from auto_applier.config import COVER_LETTERS_DIR
        try:
            COVER_LETTERS_DIR.mkdir(parents=True, exist_ok=True)
            import os, sys, subprocess
            if sys.platform == "win32":
                os.startfile(str(COVER_LETTERS_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(COVER_LETTERS_DIR)], check=False)
            else:
                subprocess.run(["xdg-open", str(COVER_LETTERS_DIR)], check=False)
        except Exception as exc:
            messagebox.showwarning(
                "Could not open folder", f"{COVER_LETTERS_DIR}\n\n{exc}",
            )
