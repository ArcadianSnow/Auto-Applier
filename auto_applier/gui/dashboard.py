"""Live dashboard window — shows real-time progress while applying to jobs."""

from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime


class DashboardWindow:
    """Real-time monitoring dashboard for the application run loop."""

    PLATFORM_NAMES = {
        "linkedin": "LinkedIn",
        "indeed": "Indeed",
        "dice": "Dice",
        "ziprecruiter": "ZipRecruiter",
    }

    def __init__(self, root: tk.Tk, enabled_platforms: list[str], dry_run: bool = False) -> None:
        self.window = tk.Toplevel(root)
        self.window.title("Auto Applier — Dashboard")
        self.window.geometry("820x620")
        self.window.configure(bg="#0F172A")
        self.window.resizable(False, False)

        self.dry_run = dry_run
        self.enabled_platforms = enabled_platforms
        self._running = False
        self._thread: threading.Thread | None = None

        # Stats
        self.stats = {
            "applied": 0, "failed": 0, "skipped": 0, "gaps": 0, "total_found": 0,
        }
        self.platform_stats: dict[str, dict] = {}
        for key in enabled_platforms:
            self.platform_stats[key] = {
                "status": "waiting",  # waiting, logging_in, searching, applying, done, error
                "applied": 0, "failed": 0, "skipped": 0,
            }

        self._build_ui()

    # ── UI Construction ──────────────────────────────────────────

    def _build_ui(self) -> None:
        w = self.window

        # ── Top bar ──────────────────────────────────────────────
        top = tk.Frame(w, bg="#1E293B", height=50)
        top.pack(fill="x")
        top.pack_propagate(False)

        mode_text = "DRY RUN" if self.dry_run else "LIVE"
        mode_color = "#3B82F6" if self.dry_run else "#10B981"

        tk.Label(
            top, text="Auto Applier", font=("Segoe UI", 14, "bold"),
            fg="#F8FAFC", bg="#1E293B",
        ).pack(side="left", padx=16, pady=10)

        self.mode_badge = tk.Label(
            top, text=mode_text, font=("Segoe UI", 9, "bold"),
            fg="white", bg=mode_color, padx=8, pady=2,
        )
        self.mode_badge.pack(side="left", padx=(0, 16), pady=14)

        self.elapsed_label = tk.Label(
            top, text="Elapsed: 0:00", font=("Consolas", 10),
            fg="#94A3B8", bg="#1E293B",
        )
        self.elapsed_label.pack(side="right", padx=16, pady=14)

        self.status_label = tk.Label(
            top, text="Initializing...", font=("Segoe UI", 10),
            fg="#94A3B8", bg="#1E293B",
        )
        self.status_label.pack(side="right", padx=16, pady=14)

        # ── Stats row ────────────────────────────────────────────
        stats_frame = tk.Frame(w, bg="#0F172A")
        stats_frame.pack(fill="x", padx=16, pady=(12, 8))

        self.stat_labels: dict[str, tk.Label] = {}
        stat_defs = [
            ("applied", "Applied", "#10B981"),
            ("failed", "Failed", "#EF4444"),
            ("skipped", "Skipped", "#F59E0B"),
            ("gaps", "Gaps Found", "#8B5CF6"),
            ("total_found", "Jobs Found", "#3B82F6"),
        ]

        for key, label, color in stat_defs:
            card = tk.Frame(stats_frame, bg="#1E293B", padx=16, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=4)

            val = tk.Label(
                card, text="0", font=("Segoe UI", 20, "bold"),
                fg=color, bg="#1E293B",
            )
            val.pack()
            tk.Label(
                card, text=label, font=("Segoe UI", 9),
                fg="#94A3B8", bg="#1E293B",
            ).pack()
            self.stat_labels[key] = val

        # ── Platform cards ───────────────────────────────────────
        platforms_frame = tk.Frame(w, bg="#0F172A")
        platforms_frame.pack(fill="x", padx=16, pady=(4, 8))

        self.platform_cards: dict[str, dict] = {}

        for key in self.enabled_platforms:
            name = self.PLATFORM_NAMES.get(key, key)
            card = tk.Frame(platforms_frame, bg="#1E293B", padx=12, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=4)

            header = tk.Frame(card, bg="#1E293B")
            header.pack(fill="x")

            status_dot = tk.Canvas(header, width=10, height=10, bg="#1E293B", highlightthickness=0)
            status_dot.create_oval(1, 1, 9, 9, fill="#475569", outline="", tags="dot")
            status_dot.pack(side="left", padx=(0, 6))

            tk.Label(
                header, text=name, font=("Segoe UI", 10, "bold"),
                fg="#F8FAFC", bg="#1E293B",
            ).pack(side="left")

            status_text = tk.Label(
                card, text="Waiting", font=("Segoe UI", 8),
                fg="#64748B", bg="#1E293B",
            )
            status_text.pack(anchor="w", pady=(4, 2))

            counts_text = tk.Label(
                card, text="✓ 0   ✗ 0   ⊘ 0", font=("Consolas", 8),
                fg="#94A3B8", bg="#1E293B",
            )
            counts_text.pack(anchor="w")

            self.platform_cards[key] = {
                "dot": status_dot,
                "status": status_text,
                "counts": counts_text,
            }

        # ── Activity log ─────────────────────────────────────────
        log_label = tk.Label(
            w, text="Activity Log", font=("Segoe UI", 10, "bold"),
            fg="#94A3B8", bg="#0F172A", anchor="w",
        )
        log_label.pack(fill="x", padx=20, pady=(4, 2))

        log_frame = tk.Frame(w, bg="#1E293B")
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        self.log_text = tk.Text(
            log_frame, bg="#1E293B", fg="#CBD5E1", font=("Consolas", 9),
            wrap="word", borderwidth=0, highlightthickness=0,
            insertbackground="#CBD5E1", state="disabled",
            padx=12, pady=8,
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Configure log text colors
        self.log_text.tag_configure("time", foreground="#64748B")
        self.log_text.tag_configure("success", foreground="#10B981")
        self.log_text.tag_configure("error", foreground="#EF4444")
        self.log_text.tag_configure("warning", foreground="#F59E0B")
        self.log_text.tag_configure("info", foreground="#3B82F6")
        self.log_text.tag_configure("platform", foreground="#C084FC", font=("Consolas", 9, "bold"))

        # ── Bottom bar ───────────────────────────────────────────
        bottom = tk.Frame(w, bg="#1E293B", height=40)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)

        self.stop_btn = ttk.Button(
            bottom, text="Stop", style="Danger.TButton",
            command=self._on_stop,
        )
        self.stop_btn.pack(side="right", padx=16, pady=6)

        self.close_btn = ttk.Button(
            bottom, text="Close", style="Ghost.TButton",
            command=self.window.destroy,
        )
        self.close_btn.pack(side="right", padx=(0, 8), pady=6)
        self.close_btn.pack_forget()  # Hidden until run finishes

    # ── Public API (called from the run loop) ────────────────────

    def log(self, message: str, tag: str = "info") -> None:
        """Add a timestamped message to the activity log."""
        def _update():
            self.log_text.configure(state="normal")
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{timestamp}] ", "time")
            self.log_text.insert("end", f"{message}\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.window.after(0, _update)

    def set_status(self, text: str) -> None:
        """Update the top-bar status text."""
        self.window.after(0, lambda: self.status_label.configure(text=text))

    def update_platform(self, key: str, status: str) -> None:
        """Update a platform card's status and dot color."""
        status_map = {
            "waiting": ("#475569", "Waiting"),
            "logging_in": ("#F59E0B", "Logging in..."),
            "searching": ("#3B82F6", "Searching..."),
            "applying": ("#8B5CF6", "Applying..."),
            "done": ("#10B981", "Done"),
            "error": ("#EF4444", "Error"),
            "skipped": ("#64748B", "Skipped"),
        }
        color, label = status_map.get(status, ("#475569", status))

        def _update():
            if key not in self.platform_cards:
                return
            card = self.platform_cards[key]
            card["dot"].delete("dot")
            card["dot"].create_oval(1, 1, 9, 9, fill=color, outline="", tags="dot")
            card["status"].configure(text=label, fg=color)
            self._refresh_platform_counts(key)

        self.platform_stats[key]["status"] = status
        self.window.after(0, _update)

    def record_application(self, platform_key: str, status: str, gaps_count: int = 0) -> None:
        """Record an application result and update all counters."""
        p = self.platform_stats[platform_key]

        if status in ("applied", "dry_run"):
            self.stats["applied"] += 1
            p["applied"] += 1
        elif status == "failed":
            self.stats["failed"] += 1
            p["failed"] += 1
        elif status == "skipped":
            self.stats["skipped"] += 1
            p["skipped"] += 1

        self.stats["gaps"] += gaps_count

        self.window.after(0, self._refresh_stats)
        self.window.after(0, lambda: self._refresh_platform_counts(platform_key))

    def add_jobs_found(self, count: int) -> None:
        """Update the total jobs found counter."""
        self.stats["total_found"] += count
        self.window.after(0, self._refresh_stats)

    def mark_finished(self) -> None:
        """Mark the run as complete."""
        self._running = False
        self.set_status("Finished")
        self.log("Run complete.", "success")

        def _update():
            self.stop_btn.pack_forget()
            self.close_btn.pack(side="right", padx=16, pady=6)
        self.window.after(0, _update)

    def mark_error(self, error: str) -> None:
        """Mark the run as errored."""
        self._running = False
        self.set_status("Error")
        self.log(f"Error: {error}", "error")

        def _update():
            self.stop_btn.pack_forget()
            self.close_btn.pack(side="right", padx=16, pady=6)
        self.window.after(0, _update)

    # ── Run management ───────────────────────────────────────────

    def start_run(self, config: dict, dry_run: bool) -> None:
        """Start the application loop in a background thread."""
        self._running = True
        self._start_time = datetime.now()
        self._tick_elapsed()

        self.log(
            f"Starting {'dry run' if dry_run else 'live run'} on "
            f"{len(self.enabled_platforms)} platform(s)...",
            "info",
        )

        self._thread = threading.Thread(
            target=self._run_in_thread, args=(config, dry_run), daemon=True,
        )
        self._thread.start()

    def _run_in_thread(self, config: dict, dry_run: bool) -> None:
        """Run the async application loop in a new event loop on this thread."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run_applications(config, dry_run))
        except Exception as e:
            self.mark_error(str(e))
        finally:
            self.mark_finished()

    async def _run_applications(self, config: dict, dry_run: bool) -> None:
        """Core run loop — mirrors main.py but reports to the dashboard."""
        from auto_applier.analysis.gap_tracker import record_gaps, record_skills_gaps_from_description
        from auto_applier.browser.anti_detect import random_delay
        from auto_applier.browser.platforms import PLATFORM_REGISTRY
        from auto_applier.browser.session import BrowserSession
        from auto_applier.config import (
            MAX_APPLICATIONS_PER_DAY,
            MIN_DELAY_BETWEEN_APPLICATIONS,
            MAX_DELAY_BETWEEN_APPLICATIONS,
        )
        from auto_applier.main import _build_platform_config
        from auto_applier.resume.parser import extract_text
        from auto_applier.resume.skills import extract_skills
        from auto_applier.storage import repository
        from auto_applier.storage.models import Application
        from pathlib import Path

        merged = _build_platform_config(config)

        # Parse resume
        resume_text = extract_text(Path(config["resume_path"]))
        resume_skills = extract_skills(resume_text)
        self.log(f"Parsed resume: {len(resume_skills)} skills found.", "info")

        # Daily limit
        todays_count = repository.get_todays_application_count()
        max_today = MAX_APPLICATIONS_PER_DAY
        remaining = max_today - todays_count

        if remaining <= 0:
            self.log(f"Daily limit reached ({todays_count}/{max_today}). Try tomorrow.", "warning")
            return

        self.log(f"Daily budget: {remaining} remaining ({todays_count} done today).", "info")
        self.set_status("Running")

        session = BrowserSession()
        try:
            context = await session.start()
            applied_count = 0

            for platform_key in self.enabled_platforms:
                if not self._running or applied_count >= remaining:
                    break

                PlatformClass = PLATFORM_REGISTRY.get(platform_key)
                if not PlatformClass:
                    self.update_platform(platform_key, "error")
                    self.log(f"Unknown platform: {platform_key}", "error")
                    continue

                platform = PlatformClass(context, merged)
                name = platform.name

                # Login
                self.update_platform(platform_key, "logging_in")
                self.log(f"Logging into {name}...", "platform")

                if not await platform.ensure_logged_in():
                    self.update_platform(platform_key, "error")
                    self.log(f"{name}: login failed. Skipping.", "error")
                    continue

                self.log(f"{name}: logged in.", "success")

                for keyword in config.get("search_keywords", []):
                    if not self._running or applied_count >= remaining:
                        break

                    # Search
                    self.update_platform(platform_key, "searching")
                    self.log(f"{name}: searching for \"{keyword}\"...", "info")

                    jobs = await platform.search_jobs(keyword, config.get("location", ""))
                    self.add_jobs_found(len(jobs))
                    self.log(f"{name}: found {len(jobs)} listings.", "info")

                    # Apply
                    self.update_platform(platform_key, "applying")

                    for job in jobs:
                        if not self._running or applied_count >= remaining:
                            break
                        if repository.job_already_applied(job.job_id, platform.source_id):
                            continue

                        self.log(f"{name}: {job.title} at {job.company}", "info")

                        description = await platform.get_job_description(job)
                        job.description = description
                        repository.save(job)

                        record_skills_gaps_from_description(
                            job.job_id, description, resume_skills,
                        )

                        success, form_gaps = await platform.apply_to_job(job, dry_run=dry_run)

                        if form_gaps:
                            record_gaps(job.job_id, form_gaps)

                        status = "dry_run" if dry_run else ("applied" if success else "failed")
                        repository.save(Application(
                            job_id=job.job_id,
                            status=status,
                            source=platform.source_id,
                            failure_reason="" if success else "Application failed",
                        ))

                        tag = "success" if success else "error"
                        self.log(f"  → {status.upper()}", tag)
                        self.record_application(platform_key, status, len(form_gaps))

                        applied_count += 1
                        self.set_status(f"Applied: {applied_count}/{remaining}")

                        await random_delay(
                            MIN_DELAY_BETWEEN_APPLICATIONS,
                            MAX_DELAY_BETWEEN_APPLICATIONS,
                        )

                self.update_platform(platform_key, "done")

            self.log(
                f"Session complete: {applied_count} applications across "
                f"{len(self.enabled_platforms)} platform(s).",
                "success",
            )

        finally:
            await session.close()

    def _tick_elapsed(self) -> None:
        """Update the elapsed time label every second."""
        if not hasattr(self, '_start_time'):
            return
        elapsed = datetime.now() - self._start_time
        minutes = int(elapsed.total_seconds()) // 60
        seconds = int(elapsed.total_seconds()) % 60
        self.elapsed_label.configure(text=f"Elapsed: {minutes}:{seconds:02d}")

        if self._running:
            self.window.after(1000, self._tick_elapsed)

    def _on_stop(self) -> None:
        """Gracefully stop the run."""
        self._running = False
        self.set_status("Stopping...")
        self.log("Stop requested — finishing current action...", "warning")

    def _refresh_stats(self) -> None:
        """Refresh the stat counters in the UI."""
        for key, label in self.stat_labels.items():
            label.configure(text=str(self.stats[key]))

    def _refresh_platform_counts(self, key: str) -> None:
        """Refresh a platform card's count text."""
        if key not in self.platform_cards:
            return
        p = self.platform_stats[key]
        text = f"✓ {p['applied']}   ✗ {p['failed']}   ⊘ {p['skipped']}"
        self.platform_cards[key]["counts"].configure(text=text)
