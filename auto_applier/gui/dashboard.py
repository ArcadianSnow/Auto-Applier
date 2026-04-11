"""Live dashboard -- real-time monitoring during application runs."""
import asyncio
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, PRIMARY_LIGHT, ACCENT, DANGER, WARNING,
    TEXT, TEXT_LIGHT, TEXT_MUTED, BORDER,
    STATUS_IDLE, STATUS_RUNNING, STATUS_SUCCESS, STATUS_ERROR,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
    FONT_BUTTON,
    PAD_X, PAD_Y,
)
from auto_applier.orchestrator.events import (
    EventEmitter,
    RUN_STARTED,
    PLATFORM_STARTED,
    PLATFORM_LOGIN_NEEDED,
    PLATFORM_LOGIN_FAILED,
    SEARCH_STARTED,
    JOBS_FOUND,
    JOB_SCORED,
    USER_REVIEW_NEEDED,
    REVIEW_QUEUE_READY,
    APPLICATION_STARTED,
    APPLICATION_COMPLETE,
    PLATFORM_ERROR,
    PLATFORM_FINISHED,
    EVOLUTION_TRIGGERS,
    RUN_FINISHED,
    CAPTCHA_DETECTED,
)


class DashboardWindow(tk.Toplevel):
    """Live monitoring window displayed during application runs."""

    def __init__(self, parent: tk.Tk, config: dict) -> None:
        super().__init__(parent)
        self.parent_wizard = parent
        self.config = config
        self.events = EventEmitter()
        self._running = False
        self._engine = None  # set when a run starts, cleared on finish
        self._start_time: float | None = None
        self._timer_id: str | None = None

        # Stats
        self._applied = 0
        self._failed = 0
        self._skipped = 0
        self._score_sum = 0
        self._score_count = 0
        self._gaps_found = 0

        # Platform states
        self._platform_cards: dict[str, dict] = {}

        self._setup_window()
        self._build_ui()
        self._subscribe_events()

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.title("Auto Applier -- Running")
        self.configure(bg=BG)
        self.geometry("900x650")
        self.resizable(True, True)
        self.minsize(700, 500)

        # Center on screen
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - 900) // 2
        y = (sh - 650) // 2
        self.geometry(f"+{x}+{y}")

        # Handle close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Header ---
        header = tk.Frame(self, bg=BG_CARD, height=50)
        header.pack(fill="x")
        header.pack_propagate(False)

        self._title_label = tk.Label(
            header, text="Auto Applier -- Running",
            font=FONT_HEADING, fg=PRIMARY, bg=BG_CARD,
        )
        self._title_label.pack(side="left", padx=PAD_X, pady=8)

        self._timer_label = tk.Label(
            header, text="00:00", font=FONT_MONO,
            fg=TEXT_LIGHT, bg=BG_CARD,
        )
        self._timer_label.pack(side="right", padx=PAD_X, pady=8)

        dry_run = self.config.get("dry_run", False)
        if dry_run:
            tk.Label(
                header, text="DRY RUN", font=FONT_BUTTON,
                fg=WARNING, bg=BG_CARD,
            ).pack(side="right", padx=(0, 12))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Stats row ---
        stats_frame = tk.Frame(self, bg=BG)
        stats_frame.pack(fill="x", padx=PAD_X, pady=(12, 8))

        self._stat_labels: dict[str, tk.Label] = {}
        stats = [
            ("Applied", "applied", ACCENT),
            ("Failed", "failed", DANGER),
            ("Skipped", "skipped", TEXT_MUTED),
            ("Avg Score", "score_avg", PRIMARY),
            ("Gaps Found", "gaps", WARNING),
        ]

        for label_text, key, color in stats:
            col = tk.Frame(stats_frame, bg=BG_CARD, highlightbackground=BORDER,
                           highlightthickness=1, padx=16, pady=8)
            col.pack(side="left", expand=True, fill="x", padx=4)

            tk.Label(
                col, text="0", font=("Segoe UI", 20, "bold"),
                fg=color, bg=BG_CARD,
            ).pack()
            self._stat_labels[key] = col.winfo_children()[0]  # the number label

            tk.Label(
                col, text=label_text, font=FONT_SMALL,
                fg=TEXT_LIGHT, bg=BG_CARD,
            ).pack()

        # --- Platform cards ---
        platforms_frame = tk.Frame(self, bg=BG)
        platforms_frame.pack(fill="x", padx=PAD_X, pady=(4, 8))

        enabled = self.config.get("enabled_platforms", [])
        for key in enabled:
            card = tk.Frame(
                platforms_frame, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=12, pady=8,
            )
            card.pack(side="left", expand=True, fill="x", padx=4)

            dot = tk.Canvas(
                card, width=10, height=10, bg=BG_CARD,
                highlightthickness=0, bd=0,
            )
            dot.pack(side="left", padx=(0, 6))
            dot.create_oval(1, 1, 9, 9, fill=STATUS_IDLE, outline=STATUS_IDLE)

            name_lbl = tk.Label(
                card, text=key.title(), font=FONT_BODY,
                fg=TEXT, bg=BG_CARD,
            )
            name_lbl.pack(side="left")

            status_lbl = tk.Label(
                card, text="Waiting", font=FONT_SMALL,
                fg=TEXT_MUTED, bg=BG_CARD,
            )
            status_lbl.pack(side="right")

            self._platform_cards[key] = {
                "dot": dot,
                "name": name_lbl,
                "status": status_lbl,
            }

        # --- Activity log ---
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=PAD_X, pady=(4, 8))

        tk.Label(
            log_frame, text="Activity Log", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG,
        ).pack(anchor="w", pady=(0, 4))

        text_frame = tk.Frame(log_frame, bg=BG)
        text_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        self._log_text = tk.Text(
            text_frame,
            font=FONT_MONO,
            bg=BG_CARD,
            fg=TEXT,
            wrap="word",
            state="disabled",
            height=12,
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.configure(command=self._log_text.yview)
        self._log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Configure text tags for colored log entries
        self._log_text.tag_configure("info", foreground=TEXT)
        self._log_text.tag_configure("success", foreground=ACCENT)
        self._log_text.tag_configure("warning", foreground=WARNING)
        self._log_text.tag_configure("error", foreground=DANGER)
        self._log_text.tag_configure("score", foreground=PRIMARY)
        self._log_text.tag_configure("timestamp", foreground=TEXT_MUTED)

        # --- Footer buttons ---
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=PAD_X, pady=(0, 12))

        self._stop_btn = ttk.Button(
            footer, text="Stop", style="Danger.TButton",
            command=self._on_stop,
        )
        self._stop_btn.pack(side="left")

        # Developer aid: one-click access to the debug log file for
        # the current run. Paste the path / attach the file when
        # asking for help with a failing run.
        ttk.Button(
            footer, text="Open Log",
            command=self._open_log_file,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            footer, text="Open Logs Folder",
            command=self._open_logs_folder,
        ).pack(side="left", padx=(8, 0))

        self._close_btn = ttk.Button(
            footer, text="Close",
            command=self._on_close, state="disabled",
        )
        self._close_btn.pack(side="right")

    def _open_log_file(self) -> None:
        """Open the current run's debug log file in the OS default app."""
        from auto_applier.log_setup import current_log_path
        path = current_log_path()
        if path is None or not path.exists():
            self.log("No log file for this session yet.", "warning")
            return
        self._open_path(path)

    def _open_logs_folder(self) -> None:
        """Open the data/logs directory in the OS file explorer."""
        from auto_applier.config import LOGS_DIR
        self._open_path(LOGS_DIR)

    @staticmethod
    def _open_path(path: Path) -> None:
        """Cross-platform open-with-default-app helper."""
        import os
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Event subscriptions
    # ------------------------------------------------------------------

    def _subscribe_events(self) -> None:
        """Wire up event handlers."""
        self.events.on(RUN_STARTED, self._on_run_started)
        self.events.on(PLATFORM_STARTED, self._on_platform_started)
        self.events.on(PLATFORM_LOGIN_NEEDED, self._on_platform_login)
        self.events.on(PLATFORM_LOGIN_FAILED, self._on_platform_login_failed)
        self.events.on(SEARCH_STARTED, self._on_search_started)
        self.events.on(JOBS_FOUND, self._on_jobs_found)
        self.events.on(JOB_SCORED, self._on_job_scored)
        self.events.on(USER_REVIEW_NEEDED, self._on_user_review)
        self.events.on(REVIEW_QUEUE_READY, self._on_review_queue_ready)
        self.events.on(APPLICATION_STARTED, self._on_application_started)
        self.events.on(APPLICATION_COMPLETE, self._on_application_complete)
        self.events.on(PLATFORM_ERROR, self._on_platform_error)
        self.events.on(PLATFORM_FINISHED, self._on_platform_finished)
        self.events.on(CAPTCHA_DETECTED, self._on_captcha)
        self.events.on(EVOLUTION_TRIGGERS, self._on_evolution_triggers)
        self.events.on(RUN_FINISHED, self._on_run_finished_event)

    # ------------------------------------------------------------------
    # Event handlers (called from worker thread -- must use self.after)
    # ------------------------------------------------------------------

    def _on_run_started(self, **kw):
        dry = kw.get("dry_run", False)
        mode = "DRY RUN" if dry else "LIVE"
        self.after(0, lambda: self.log(f"Run started ({mode})", "info"))

    def _on_platform_started(self, **kw):
        key = kw.get("platform", "")
        self.after(0, lambda: self._update_platform(key, "Running", STATUS_RUNNING))
        self.after(0, lambda: self.log(f"[{key.title()}] Starting...", "info"))

    def _on_platform_login(self, **kw):
        key = kw.get("platform", "")
        self.after(0, lambda: self.log(
            f"[{key.title()}] Checking login -- please log in if browser opens.", "warning"
        ))

    def _on_platform_login_failed(self, **kw):
        key = kw.get("platform", "")
        self.after(0, lambda: self._update_platform(key, "Login failed", STATUS_ERROR))
        self.after(0, lambda: self.log(f"[{key.title()}] Login failed -- skipping.", "error"))

    def _on_search_started(self, **kw):
        key = kw.get("platform", "")
        keyword = kw.get("keyword", "")
        self.after(0, lambda: self.log(
            f"[{key.title()}] Searching: {keyword}", "info"
        ))

    def _on_jobs_found(self, **kw):
        key = kw.get("platform", "")
        count = kw.get("count", 0)
        keyword = kw.get("keyword", "")
        self.after(0, lambda: self.log(
            f"[{key.title()}] Found {count} jobs for '{keyword}'", "success"
        ))

    def _on_job_scored(self, **kw):
        job = kw.get("job")
        score = kw.get("score")
        if job and score:
            title = getattr(job, "title", "Unknown")
            s = getattr(score, "score", 0)
            decision = getattr(score, "decision", None)
            d_name = decision.value if decision else "?"
            self.after(0, lambda: self.log(
                f"  Scored: {title} -- {s}/10 ({d_name})", "score"
            ))
            self.after(0, lambda: self._update_score(s))

    def _on_user_review(self, **kw):
        """Increment the pending-review counter and update the log.

        The engine now QUEUES borderline jobs for a batch review at
        the end of the run instead of blocking the pipeline on each
        one. This handler just displays a running count so users
        see how many jobs are waiting for them.
        """
        job = kw.get("job")
        title = getattr(job, "title", "Unknown") if job else "Unknown"
        self.after(0, lambda t=title: self.log(
            f"  Queued for review: {t}", "info",
        ))

    def _on_review_queue_ready(self, **kw):
        """Open the batch review panel when all platforms finish."""
        queue = kw.get("queue", [])
        if not queue:
            return
        self.after(0, lambda q=list(queue): self._open_batch_review(q))

    def _on_application_started(self, **kw):
        job = kw.get("job")
        resume = kw.get("resume", "")
        title = getattr(job, "title", "Unknown") if job else "Unknown"
        self.after(0, lambda: self.log(
            f"  Applying: {title} (resume: {resume})", "info"
        ))

    def _on_application_complete(self, **kw):
        app = kw.get("application")
        job = kw.get("job")
        if app:
            status = getattr(app, "status", "unknown")
            title = getattr(job, "title", "Unknown") if job else "Unknown"
            if status in ("applied", "dry_run"):
                self.after(0, lambda: self._increment_applied())
                self.after(0, lambda: self.log(
                    f"  Applied: {title} ({status})", "success"
                ))
            else:
                self.after(0, lambda: self._increment_failed())
                self.after(0, lambda: self.log(
                    f"  Failed: {title} ({status})", "error"
                ))

    def _on_platform_error(self, **kw):
        key = kw.get("platform", "")
        error = kw.get("error", "Unknown error")
        self.after(0, lambda: self._update_platform(key, "Error", STATUS_ERROR))
        self.after(0, lambda: self.log(f"[{key.title()}] Error: {error}", "error"))

    def _on_platform_finished(self, **kw):
        key = kw.get("platform", "")
        self.after(0, lambda: self._update_platform(key, "Done", STATUS_SUCCESS))
        self.after(0, lambda: self.log(f"[{key.title()}] Finished.", "info"))

    def _on_captcha(self, **kw):
        key = kw.get("platform", "")
        self.after(0, lambda: self.log(
            f"[{key.title()}] CAPTCHA detected -- stopping platform.", "error"
        ))

    def _on_evolution_triggers(self, **kw):
        triggers = kw.get("triggers", [])
        count = len(triggers) if isinstance(triggers, list) else 0
        self._gaps_found += count
        self.after(0, lambda: self._update_stat("gaps", str(self._gaps_found)))
        self.after(0, lambda: self.log(
            f"Evolution: {count} skill gap trigger(s) detected.", "warning"
        ))

    def _on_run_finished_event(self, **kw):
        reason = kw.get("reason", "Complete")
        applied = kw.get("applied", self._applied)
        self.after(0, lambda: self.log(
            f"Run finished: {reason} ({applied} applied)", "info"
        ))
        self.after(0, self._on_run_finished)

    # ------------------------------------------------------------------
    # UI update helpers (must be called on main thread)
    # ------------------------------------------------------------------

    def log(self, message: str, tag: str = "info") -> None:
        """Append a timestamped message to the activity log."""
        self._log_text.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.insert("end", f"[{ts}] ", "timestamp")
        self._log_text.insert("end", f"{message}\n", tag)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _update_platform(self, key: str, status: str, color: str) -> None:
        """Update a platform card's status dot and label."""
        card = self._platform_cards.get(key)
        if not card:
            return
        dot: tk.Canvas = card["dot"]
        dot.delete("all")
        dot.create_oval(1, 1, 9, 9, fill=color, outline=color)
        card["status"].configure(text=status, fg=color)

    def _update_stat(self, key: str, value: str) -> None:
        """Update a stat counter label."""
        lbl = self._stat_labels.get(key)
        if lbl:
            lbl.configure(text=value)

    def _update_score(self, score: int) -> None:
        """Update the running average score."""
        self._score_sum += score
        self._score_count += 1
        avg = self._score_sum / self._score_count
        self._update_stat("score_avg", f"{avg:.1f}")

    def _increment_applied(self) -> None:
        self._applied += 1
        self._update_stat("applied", str(self._applied))

    def _increment_failed(self) -> None:
        self._failed += 1
        self._update_stat("failed", str(self._failed))

    def _increment_skipped(self) -> None:
        self._skipped += 1
        self._update_stat("skipped", str(self._skipped))

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _start_timer(self) -> None:
        """Start the elapsed time display."""
        self._start_time = time.monotonic()
        self._tick()

    def _tick(self) -> None:
        """Update the timer label every second."""
        if self._start_time is None:
            return
        elapsed = int(time.monotonic() - self._start_time)
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            self._timer_label.configure(text=f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        else:
            self._timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
        self._timer_id = self.after(1000, self._tick)

    def _stop_timer(self) -> None:
        """Stop the elapsed time display."""
        if self._timer_id:
            self.after_cancel(self._timer_id)
            self._timer_id = None

    # ------------------------------------------------------------------
    # Run management
    # ------------------------------------------------------------------

    def start_run(self, config: dict, dry_run: bool) -> None:
        """Begin the application run in a background thread."""
        if self._running:
            return
        self._running = True
        config["dry_run"] = dry_run
        self._start_timer()
        self.log(f"Preparing to run ({'dry run' if dry_run else 'live'})...", "info")

        # Kick on the debug log file FIRST so we capture everything
        # including the resume preprocessing step that happens before
        # the orchestrator even starts.
        self._start_file_logging()

        thread = threading.Thread(
            target=self._run_in_thread,
            args=(config, dry_run),
            daemon=True,
        )
        thread.start()

    def _run_in_thread(self, config: dict, dry_run: bool) -> None:
        """Worker thread: create event loop and run the engine."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Process resumes first
            self.after(0, lambda: self.log("Processing resumes...", "info"))
            loop.run_until_complete(self._process_resumes(config))

            # Run the engine. Store a reference on the dashboard
            # so _on_stop can request a cooperative stop via the
            # engine's own flag — the old 'dashboard._running = False'
            # did nothing because the engine never read it.
            from auto_applier.orchestrator.engine import ApplicationEngine
            engine = ApplicationEngine(config, self.events, cli_mode=False)
            self._engine = engine
            loop.run_until_complete(engine.run())
        except Exception as e:
            self.after(0, lambda: self.log(f"Error: {e}", "error"))
        finally:
            loop.close()
            self.after(0, self._on_run_finished)

    def _start_file_logging(self) -> Path | None:
        """Turn on the debug log file for this run and log its path."""
        try:
            from auto_applier.log_setup import start_run_logging
            path = start_run_logging()
            self.after(0, lambda p=path: self.log(
                f"Debug log: {p}", "info",
            ))
            self.after(0, lambda p=path: self.log(
                f"  (paste this path to your helper if something looks wrong)",
                "info",
            ))
            return path
        except Exception as exc:
            self.after(0, lambda e=exc: self.log(
                f"Could not start debug log: {e}", "warning",
            ))
            return None

    async def _process_resumes(self, config: dict) -> None:
        """Add resumes to the ResumeManager (requires LLM for skill extraction).

        Emits periodic 'still working' heartbeats while each resume
        is being processed so a slow LLM call on CPU doesn't look
        like a hang. A minimal profile is always saved first, so
        even a hard timeout leaves the user with a usable resume.
        """
        import asyncio as _asyncio
        from auto_applier.llm.router import LLMRouter
        from auto_applier.resume.manager import ResumeManager

        router = LLMRouter()
        await router.initialize()
        manager = ResumeManager(router)

        for resume_info in config.get("resumes", []):
            label = resume_info["label"]
            path = resume_info["path"]
            self.after(0, lambda l=label: self.log(
                f"Processing resume: {l}... (this can take 30–60 seconds "
                f"on CPU while the AI reads it)",
                "info",
            ))

            # Heartbeat task: log a reassurance line every 10 seconds
            # while add_resume is in flight so users know we're alive.
            stop_event = _asyncio.Event()

            async def _heartbeat(label=label, stop=stop_event):
                elapsed = 0
                while not stop.is_set():
                    try:
                        await _asyncio.wait_for(stop.wait(), timeout=10.0)
                    except _asyncio.TimeoutError:
                        elapsed += 10
                        self.after(0, lambda e=elapsed, l=label: self.log(
                            f"  ... still reading '{l}' ({e}s elapsed)",
                            "info",
                        ))

            hb = _asyncio.create_task(_heartbeat())
            try:
                await manager.add_resume(path, label)
                self.after(0, lambda l=label: self.log(
                    f"Resume '{l}' processed successfully.", "success"
                ))
            except Exception as e:
                err_msg = str(e)
                self.after(0, lambda l=label, msg=err_msg: self.log(
                    f"Failed to process resume '{l}': {msg}", "error"
                ))
            finally:
                stop_event.set()
                try:
                    await hb
                except Exception:
                    pass

    def _on_run_finished(self) -> None:
        """Called when the run completes (on main thread)."""
        self._running = False
        self._stop_timer()
        self._title_label.configure(text="Auto Applier -- Complete")
        self.title("Auto Applier -- Complete")
        self._stop_btn.configure(state="disabled")
        self._close_btn.configure(state="normal")
        self.log("Run complete.", "info")

    # ------------------------------------------------------------------
    # Review panel bridge
    # ------------------------------------------------------------------

    def _open_review_panel(self, job, score) -> None:
        """Open the job review panel and resolve the event with the decision.

        NOTE: This method is kept for backward compatibility but is
        no longer wired to USER_REVIEW_NEEDED. The engine queues
        borderline jobs and opens _open_batch_review at the end of
        the run instead.
        """
        from auto_applier.gui.panels.job_review import JobReviewPanel

        def on_decision(decision: str):
            if decision == "skip":
                self._increment_skipped()
            self.events.resolve_event(USER_REVIEW_NEEDED, decision)

        JobReviewPanel(self, job, score, on_decision)

    def _open_batch_review(self, queue: list[dict]) -> None:
        """Walk the user through all queued review jobs in one sitting.

        Shows one JobReviewPanel at a time, collects apply/skip
        decisions into a dict keyed by job_id, and resolves the
        REVIEW_QUEUE_READY event with the full dict once the user
        finishes the last one (or closes the panel).
        """
        from auto_applier.gui.panels.job_review import JobReviewPanel

        if not queue:
            self.events.resolve_event(REVIEW_QUEUE_READY, {})
            return

        decisions: dict[str, str] = {}
        state = {"index": 0}
        total = len(queue)

        self.log(
            f"Review queue: {total} job(s) to review — "
            f"a panel will open for each one.",
            "info",
        )

        def show_next():
            idx = state["index"]
            if idx >= total:
                # All done — resolve the event with the full decision map
                self.log(
                    f"Review complete: {sum(1 for v in decisions.values() if v == 'apply')} "
                    f"approved, {sum(1 for v in decisions.values() if v == 'skip')} skipped.",
                    "info",
                )
                self.events.resolve_event(REVIEW_QUEUE_READY, decisions)
                return

            item = queue[idx]
            job = item["job"]
            score = item["score"]

            # Log progress so the dashboard shows "reviewing 2 of 5"
            self.log(
                f"  Reviewing {idx + 1}/{total}: {getattr(job, 'title', 'Unknown')}",
                "info",
            )

            def on_decision(decision: str):
                decisions[job.job_id] = decision
                state["index"] += 1
                # Small delay then open the next one so the user sees
                # the old panel close cleanly before the next opens.
                self.after(150, show_next)

            JobReviewPanel(self, job, score, on_decision)

        show_next()

    # ------------------------------------------------------------------
    # Close / Stop
    # ------------------------------------------------------------------

    def _on_stop(self) -> None:
        """Request the run to stop cooperatively.

        Sets a flag on the engine that gets checked between jobs
        and between platforms. The CURRENT application finishes
        (you can't cleanly abort a mid-form fill) but nothing new
        starts after it.
        """
        if self._running:
            self.log(
                "Stop requested — will stop after the current application finishes.",
                "warning",
            )
            if self._engine is not None:
                try:
                    self._engine.request_stop()
                except Exception as exc:
                    self.log(f"  stop request error: {exc}", "error")
            self._stop_btn.configure(state="disabled")

    def _on_close(self) -> None:
        """Close the dashboard window."""
        self._stop_timer()
        self.destroy()
