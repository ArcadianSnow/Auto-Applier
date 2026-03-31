"""Job review panel -- modal dialog for USER_REVIEW scoring decisions."""
import tkinter as tk
from tkinter import ttk
from typing import Callable

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, ACCENT, DANGER, WARNING,
    TEXT, TEXT_LIGHT, TEXT_MUTED, BORDER,
    STATUS_SUCCESS, STATUS_ERROR,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
    PAD_X, PAD_Y, make_scrollable,
)


class JobReviewPanel(tk.Toplevel):
    """Modal dialog for reviewing a job that scored in the USER_REVIEW range.

    The panel shows job details, score breakdown, skill matches, and
    lets the user decide to apply or skip.

    Parameters:
        parent: Parent window (usually the DashboardWindow).
        job: The job object from the orchestrator.
        score: The JobScore object with scoring details.
        on_decision: Callback receiving ``"apply"`` or ``"skip"``.
    """

    def __init__(
        self,
        parent: tk.Widget,
        job,
        score,
        on_decision: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self._job = job
        self._score = score
        self._on_decision = on_decision
        self._decision_made = False

        self._setup_window()
        self._build_ui()

        # Make modal
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._skip)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.title("Review Job")
        self.configure(bg=BG)
        self.geometry("600x550")
        self.resizable(True, True)
        self.minsize(500, 400)

        # Center on parent
        self.update_idletasks()
        px = self.master.winfo_x()
        py = self.master.winfo_y()
        pw = self.master.winfo_width()
        ph = self.master.winfo_height()
        x = px + (pw - 600) // 2
        y = py + (ph - 550) // 2
        self.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        job = self._job
        score = self._score

        # Extract data safely
        title = getattr(job, "title", "Unknown Position") if job else "Unknown Position"
        company = getattr(job, "company", "Unknown Company") if job else "Unknown Company"
        location = getattr(job, "location", "") if job else ""
        description = getattr(job, "description", "") if job else ""

        score_val = getattr(score, "score", 0) if score else 0
        explanation = getattr(score, "explanation", "") if score else ""
        matched = getattr(score, "matched_skills", []) if score else []
        missing = getattr(score, "missing_skills", []) if score else []
        resume_label = getattr(score, "resume_label", "") if score else ""

        # --- Header ---
        header = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=12)
        header.pack(fill="x")

        tk.Label(
            header, text=title, font=FONT_HEADING,
            fg=TEXT, bg=BG_CARD, wraplength=540, justify="left",
        ).pack(anchor="w")

        tk.Label(
            header, text=company, font=FONT_SUBHEADING,
            fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w", pady=(2, 0))

        if location:
            tk.Label(
                header, text=location, font=FONT_SMALL,
                fg=TEXT_MUTED, bg=BG_CARD,
            ).pack(anchor="w")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Scrollable body ---
        body_container = ttk.Frame(self)
        body_container.pack(fill="both", expand=True)
        _canvas, body = make_scrollable(body_container)

        # Score badge
        score_frame = tk.Frame(body, bg=BG)
        score_frame.pack(fill="x", padx=PAD_X, pady=(PAD_Y, 8))

        score_color = self._score_color(score_val)
        score_card = tk.Frame(
            score_frame, bg=score_color, padx=16, pady=8,
        )
        score_card.pack(side="left")

        tk.Label(
            score_card, text=f"{score_val}/10", font=("Segoe UI", 18, "bold"),
            fg="white", bg=score_color,
        ).pack(side="left")

        tk.Label(
            score_card, text="  Match Score", font=FONT_BODY,
            fg="white", bg=score_color,
        ).pack(side="left")

        if resume_label:
            tk.Label(
                score_frame, text=f"Best resume: {resume_label}",
                font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG,
            ).pack(side="right", pady=8)

        # Explanation
        if explanation:
            exp_card = tk.Frame(
                body, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            exp_card.pack(fill="x", padx=PAD_X, pady=(0, 8))

            tk.Label(
                exp_card, text="AI Assessment", font=FONT_SUBHEADING,
                fg=PRIMARY, bg=BG_CARD,
            ).pack(anchor="w", pady=(0, 4))

            tk.Label(
                exp_card, text=explanation, font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, wraplength=520, justify="left",
            ).pack(anchor="w")

        # Matched skills
        if matched:
            match_card = tk.Frame(
                body, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            match_card.pack(fill="x", padx=PAD_X, pady=(0, 8))

            tk.Label(
                match_card, text=f"Matched Skills ({len(matched)})",
                font=FONT_SUBHEADING, fg=ACCENT, bg=BG_CARD,
            ).pack(anchor="w", pady=(0, 4))

            for skill in matched:
                row = tk.Frame(match_card, bg=BG_CARD)
                row.pack(fill="x", pady=1)
                tk.Label(
                    row, text=f"  +  {skill}", font=FONT_BODY,
                    fg=ACCENT, bg=BG_CARD, anchor="w",
                ).pack(anchor="w")

        # Missing skills
        if missing:
            miss_card = tk.Frame(
                body, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            miss_card.pack(fill="x", padx=PAD_X, pady=(0, 8))

            tk.Label(
                miss_card, text=f"Missing Skills ({len(missing)})",
                font=FONT_SUBHEADING, fg=DANGER, bg=BG_CARD,
            ).pack(anchor="w", pady=(0, 4))

            for skill in missing:
                row = tk.Frame(miss_card, bg=BG_CARD)
                row.pack(fill="x", pady=1)
                tk.Label(
                    row, text=f"  -  {skill}", font=FONT_BODY,
                    fg=DANGER, bg=BG_CARD, anchor="w",
                ).pack(anchor="w")

        # Description preview (truncated)
        if description:
            desc_card = tk.Frame(
                body, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            desc_card.pack(fill="x", padx=PAD_X, pady=(0, 8))

            tk.Label(
                desc_card, text="Job Description (Preview)",
                font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
            ).pack(anchor="w", pady=(0, 4))

            preview = description[:500]
            if len(description) > 500:
                preview += "..."

            desc_text = tk.Text(
                desc_card, font=FONT_SMALL, fg=TEXT, bg=BG_CARD,
                wrap="word", height=6, bd=0, highlightthickness=0,
            )
            desc_text.insert("1.0", preview)
            desc_text.configure(state="disabled")
            desc_text.pack(fill="x")

        # --- Footer buttons ---
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        footer = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=12)
        footer.pack(fill="x")

        ttk.Button(
            footer, text="Apply Anyway", style="Primary.TButton",
            command=self._apply,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            footer, text="Skip", style="Danger.TButton",
            command=self._skip,
        ).pack(side="left")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_color(score: int) -> str:
        """Return a color for the score badge."""
        if score >= 7:
            return ACCENT
        elif score >= 4:
            return WARNING
        else:
            return DANGER

    # ------------------------------------------------------------------
    # Decision handlers
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """User chose to apply."""
        if not self._decision_made:
            self._decision_made = True
            self._on_decision("apply")
        self.destroy()

    def _skip(self) -> None:
        """User chose to skip."""
        if not self._decision_made:
            self._decision_made = True
            self._on_decision("skip")
        self.destroy()
