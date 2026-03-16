"""Live dashboard — Animal Crossing 'Nook Miles' inspired theme."""

from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from auto_applier.gui.styles import (
    SANDY_SHORE, CREAM, DRIFTWOOD, WARM_WHITE, MORNING_SKY,
    SOIL_BROWN, BARK_BROWN, DRIFTWOOD_GRAY, FOGGY,
    NOOK_GREEN, NOOK_GREEN_DARK, NOOK_TAN, LEAF_GOLD,
    BLUEBELL, PEACH, LAVENDER, ERROR_RED, ERROR_DOT,
    BORDER_LIGHT, BORDER_MED, INFO_BLUE, WARNING_GOLD,
    HEADING_FONT, BODY_FONT, MONO_FONT,
)


class DashboardWindow:
    """Real-time monitoring dashboard — warm AC theme."""

    PLATFORM_NAMES = {
        "linkedin": "LinkedIn", "indeed": "Indeed",
        "dice": "Dice", "ziprecruiter": "ZipRecruiter",
    }

    def __init__(self, root: tk.Tk, enabled_platforms: list[str], dry_run: bool = False) -> None:
        self.window = tk.Toplevel(root)
        self.window.title("Auto Applier — Dashboard")
        self.window.geometry("820x640")
        self.window.configure(bg=SANDY_SHORE)
        self.window.resizable(False, False)

        self.dry_run = dry_run
        self.enabled_platforms = enabled_platforms
        self._running = False
        self._thread: threading.Thread | None = None

        self.stats = {"applied": 0, "failed": 0, "skipped": 0, "gaps": 0, "total_found": 0}
        self.platform_stats: dict[str, dict] = {}
        for key in enabled_platforms:
            self.platform_stats[key] = {"status": "waiting", "applied": 0, "failed": 0, "skipped": 0}

        self._build_ui()

    def _build_ui(self) -> None:
        w = self.window

        # ── Accent strip ─────────────────────────────────────────
        accent = tk.Frame(w, bg=DRIFTWOOD, height=4)
        accent.pack(fill="x")
        accent.pack_propagate(False)
        for i, color in enumerate([NOOK_GREEN, BLUEBELL, LEAF_GOLD]):
            tk.Frame(accent, bg=color, height=4).place(relx=i/3, rely=0, relwidth=1/3, relheight=1)

        # ── Top bar ──────────────────────────────────────────────
        top = tk.Frame(w, bg=DRIFTWOOD, height=50)
        top.pack(fill="x")
        top.pack_propagate(False)

        mode_text = "DRY RUN" if self.dry_run else "LIVE"
        mode_color = BLUEBELL if self.dry_run else NOOK_GREEN

        tk.Label(top, text="🍃 Auto Applier", font=(HEADING_FONT, 14, "bold"), fg=SOIL_BROWN, bg=DRIFTWOOD).pack(side="left", padx=16, pady=10)

        tk.Label(top, text=mode_text, font=(HEADING_FONT, 9, "bold"), fg=WARM_WHITE, bg=mode_color, padx=8, pady=2).pack(side="left", padx=(0, 16), pady=14)

        self.elapsed_label = tk.Label(top, text="Elapsed: 0:00", font=(MONO_FONT, 10), fg=DRIFTWOOD_GRAY, bg=DRIFTWOOD)
        self.elapsed_label.pack(side="right", padx=16, pady=14)

        self.status_label = tk.Label(top, text="Initializing...", font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=DRIFTWOOD)
        self.status_label.pack(side="right", padx=16, pady=14)

        tk.Frame(w, bg=NOOK_TAN, height=2).pack(fill="x")

        # ── Stats row ────────────────────────────────────────────
        stats_frame = tk.Frame(w, bg=SANDY_SHORE)
        stats_frame.pack(fill="x", padx=16, pady=(12, 8))

        self.stat_labels: dict[str, tk.Label] = {}
        stat_defs = [
            ("applied", "Applied", NOOK_GREEN),
            ("failed", "Failed", ERROR_DOT),
            ("skipped", "Skipped", LEAF_GOLD),
            ("gaps", "Gaps Found", LAVENDER),
            ("total_found", "Jobs Found", BLUEBELL),
        ]

        for key, label, color in stat_defs:
            card = tk.Frame(stats_frame, bg=CREAM, highlightbackground=BORDER_LIGHT, highlightthickness=1, padx=14, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=4)
            val = tk.Label(card, text="0", font=(HEADING_FONT, 20, "bold"), fg=color, bg=CREAM)
            val.pack()
            tk.Label(card, text=label, font=(BODY_FONT, 9), fg=DRIFTWOOD_GRAY, bg=CREAM).pack()
            self.stat_labels[key] = val

        # ── Platform cards ───────────────────────────────────────
        plat_frame = tk.Frame(w, bg=SANDY_SHORE)
        plat_frame.pack(fill="x", padx=16, pady=(4, 8))

        self.platform_cards: dict[str, dict] = {}
        for key in self.enabled_platforms:
            name = self.PLATFORM_NAMES.get(key, key)
            card = tk.Frame(plat_frame, bg=CREAM, highlightbackground=BORDER_LIGHT, highlightthickness=1, padx=12, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=4)

            header = tk.Frame(card, bg=CREAM)
            header.pack(fill="x")
            dot = tk.Canvas(header, width=10, height=10, bg=CREAM, highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=FOGGY, outline="", tags="dot")
            dot.pack(side="left", padx=(0, 6))
            tk.Label(header, text=name, font=(HEADING_FONT, 10, "bold"), fg=SOIL_BROWN, bg=CREAM).pack(side="left")

            status_text = tk.Label(card, text="Waiting", font=(BODY_FONT, 8), fg=FOGGY, bg=CREAM)
            status_text.pack(anchor="w", pady=(4, 2))
            counts_text = tk.Label(card, text="✓ 0   ✗ 0   ⊘ 0", font=(MONO_FONT, 8), fg=DRIFTWOOD_GRAY, bg=CREAM)
            counts_text.pack(anchor="w")

            self.platform_cards[key] = {"dot": dot, "status": status_text, "counts": counts_text}

        # ── Activity log ─────────────────────────────────────────
        tk.Label(w, text="Activity Log", font=(HEADING_FONT, 10, "bold"), fg=BARK_BROWN, bg=SANDY_SHORE, anchor="w").pack(fill="x", padx=20, pady=(4, 2))

        log_frame = tk.Frame(w, bg=CREAM, highlightbackground=BORDER_LIGHT, highlightthickness=1)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self.log_text = tk.Text(
            log_frame, bg=CREAM, fg=BARK_BROWN, font=(MONO_FONT, 9),
            wrap="word", borderwidth=0, highlightthickness=0,
            insertbackground=BARK_BROWN, state="disabled", padx=12, pady=8,
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_text.tag_configure("time", foreground=FOGGY)
        self.log_text.tag_configure("success", foreground=NOOK_GREEN_DARK)
        self.log_text.tag_configure("error", foreground=ERROR_RED)
        self.log_text.tag_configure("warning", foreground=WARNING_GOLD)
        self.log_text.tag_configure("info", foreground=INFO_BLUE)
        self.log_text.tag_configure("platform", foreground="#8B5DAD", font=(MONO_FONT, 9, "bold"))

        # ── Bottom bar ───────────────────────────────────────────
        tk.Frame(w, bg=NOOK_TAN, height=1).pack(fill="x")
        bottom = tk.Frame(w, bg=DRIFTWOOD, height=40)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)

        self.stop_btn = ttk.Button(bottom, text="Stop", style="Danger.TButton", command=self._on_stop)
        self.stop_btn.pack(side="right", padx=16, pady=6)
        self.close_btn = ttk.Button(bottom, text="Close", style="Ghost.TButton", command=self.window.destroy)
        self.close_btn.pack(side="right", padx=(0, 8), pady=6)
        self.close_btn.pack_forget()

    # ── Public API ───────────────────────────────────────────────

    def log(self, message: str, tag: str = "info") -> None:
        def _u():
            self.log_text.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{ts}] ", "time")
            self.log_text.insert("end", f"{message}\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.window.after(0, _u)

    def set_status(self, text: str) -> None:
        self.window.after(0, lambda: self.status_label.configure(text=text))

    def update_platform(self, key: str, status: str) -> None:
        status_map = {
            "waiting": (FOGGY, "Waiting"),
            "logging_in": (LEAF_GOLD, "Logging in..."),
            "searching": (BLUEBELL, "Searching..."),
            "applying": (LAVENDER, "Applying..."),
            "done": (NOOK_GREEN, "Done"),
            "error": (ERROR_DOT, "Error"),
            "skipped": (FOGGY, "Skipped"),
        }
        color, label = status_map.get(status, (FOGGY, status))

        def _u():
            if key not in self.platform_cards:
                return
            c = self.platform_cards[key]
            c["dot"].delete("dot")
            c["dot"].create_oval(1, 1, 9, 9, fill=color, outline="", tags="dot")
            c["status"].configure(text=label, fg=color)
            self._refresh_platform_counts(key)
        self.platform_stats[key]["status"] = status
        self.window.after(0, _u)

    def record_application(self, platform_key: str, status: str, gaps_count: int = 0) -> None:
        p = self.platform_stats[platform_key]
        if status in ("applied", "dry_run"):
            self.stats["applied"] += 1; p["applied"] += 1
        elif status == "failed":
            self.stats["failed"] += 1; p["failed"] += 1
        elif status == "skipped":
            self.stats["skipped"] += 1; p["skipped"] += 1
        self.stats["gaps"] += gaps_count
        self.window.after(0, self._refresh_stats)
        self.window.after(0, lambda: self._refresh_platform_counts(platform_key))

    def add_jobs_found(self, count: int) -> None:
        self.stats["total_found"] += count
        self.window.after(0, self._refresh_stats)

    def mark_finished(self) -> None:
        self._running = False
        self.set_status("Finished")
        self.log("Run complete.", "success")
        def _u():
            self.stop_btn.pack_forget()
            self.close_btn.pack(side="right", padx=16, pady=6)
        self.window.after(0, _u)

    def mark_error(self, error: str) -> None:
        self._running = False
        self.set_status("Error")
        self.log(f"Error: {error}", "error")
        def _u():
            self.stop_btn.pack_forget()
            self.close_btn.pack(side="right", padx=16, pady=6)
        self.window.after(0, _u)

    # ── Run management ───────────────────────────────────────────

    def start_run(self, config: dict, dry_run: bool) -> None:
        self._running = True
        self._start_time = datetime.now()
        self._tick_elapsed()
        self.log(f"Starting {'dry run' if dry_run else 'live run'} on {len(self.enabled_platforms)} platform(s)...", "info")
        self._thread = threading.Thread(target=self._run_in_thread, args=(config, dry_run), daemon=True)
        self._thread.start()

    def _run_in_thread(self, config, dry_run):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run_applications(config, dry_run))
        except Exception as e:
            self.mark_error(str(e))
        finally:
            self.mark_finished()

    async def _run_applications(self, config, dry_run):
        from auto_applier.analysis.gap_tracker import record_gaps, record_skills_gaps_from_description
        from auto_applier.browser.anti_detect import random_delay
        from auto_applier.browser.platforms import PLATFORM_REGISTRY
        from auto_applier.browser.session import BrowserSession
        from auto_applier.config import MAX_APPLICATIONS_PER_DAY, MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS
        from auto_applier.main import _build_platform_config
        from auto_applier.resume.parser import extract_text
        from auto_applier.resume.skills import extract_skills
        from auto_applier.storage import repository
        from auto_applier.storage.models import Application
        from pathlib import Path

        merged = _build_platform_config(config)
        resume_text = extract_text(Path(config["resume_path"]))
        resume_skills = extract_skills(resume_text)
        self.log(f"Parsed resume: {len(resume_skills)} skills found.", "info")

        todays_count = repository.get_todays_application_count()
        remaining = MAX_APPLICATIONS_PER_DAY - todays_count
        if remaining <= 0:
            self.log(f"Daily limit reached ({todays_count}/{MAX_APPLICATIONS_PER_DAY}).", "warning")
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
                    continue

                platform = PlatformClass(context, merged)

                self.update_platform(platform_key, "logging_in")
                self.log(f"Logging into {platform.name}...", "platform")
                if not await platform.ensure_logged_in():
                    self.update_platform(platform_key, "error")
                    self.log(f"{platform.name}: login failed.", "error")
                    continue
                self.log(f"{platform.name}: logged in.", "success")

                for keyword in config.get("search_keywords", []):
                    if not self._running or applied_count >= remaining:
                        break
                    self.update_platform(platform_key, "searching")
                    self.log(f'{platform.name}: searching for "{keyword}"...', "info")
                    jobs = await platform.search_jobs(keyword, config.get("location", ""))
                    self.add_jobs_found(len(jobs))
                    self.log(f"{platform.name}: found {len(jobs)} listings.", "info")

                    self.update_platform(platform_key, "applying")
                    for job in jobs:
                        if not self._running or applied_count >= remaining:
                            break
                        if repository.job_already_applied(job.job_id, platform.source_id):
                            continue
                        self.log(f"{platform.name}: {job.title} at {job.company}", "info")
                        description = await platform.get_job_description(job)
                        job.description = description
                        repository.save(job)
                        record_skills_gaps_from_description(job.job_id, description, resume_skills)
                        success, form_gaps = await platform.apply_to_job(job, dry_run=dry_run)
                        if form_gaps:
                            record_gaps(job.job_id, form_gaps)
                        status = "dry_run" if dry_run else ("applied" if success else "failed")
                        repository.save(Application(job_id=job.job_id, status=status, source=platform.source_id, failure_reason="" if success else "Failed"))
                        self.log(f"  → {status.upper()}", "success" if success else "error")
                        self.record_application(platform_key, status, len(form_gaps))
                        applied_count += 1
                        self.set_status(f"Applied: {applied_count}/{remaining}")
                        await random_delay(MIN_DELAY_BETWEEN_APPLICATIONS, MAX_DELAY_BETWEEN_APPLICATIONS)

                self.update_platform(platform_key, "done")
            self.log(f"Session complete: {applied_count} applications.", "success")
        finally:
            await session.close()

    def _tick_elapsed(self):
        if not hasattr(self, '_start_time'):
            return
        elapsed = datetime.now() - self._start_time
        m, s = int(elapsed.total_seconds()) // 60, int(elapsed.total_seconds()) % 60
        self.elapsed_label.configure(text=f"Elapsed: {m}:{s:02d}")
        if self._running:
            self.window.after(1000, self._tick_elapsed)

    def _on_stop(self):
        self._running = False
        self.set_status("Stopping...")
        self.log("Stop requested — finishing current action...", "warning")

    def _refresh_stats(self):
        for key, label in self.stat_labels.items():
            label.configure(text=str(self.stats[key]))

    def _refresh_platform_counts(self, key):
        if key not in self.platform_cards:
            return
        p = self.platform_stats[key]
        self.platform_cards[key]["counts"].configure(text=f"✓ {p['applied']}   ✗ {p['failed']}   ⊘ {p['skipped']}")
