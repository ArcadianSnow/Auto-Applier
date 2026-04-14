"""Refine panel — interactive resume improvement chat.

GUI version of `cli refine`. For each top skill gap surfaced by
collect_refine_candidates(), the user picks one of:
  - I have experience -> describe -> LLM drafts bullets -> approve
  - Currently learning -> goes to learning_goals
  - Not interested -> goes to learning_goals + prompted_skills
  - Skip for now -> no state change

If the user has cross-archetype scoring mismatch, the panel surfaces
a resume suggestion banner at the top.

Hallucination guard: bullet generation uses RESUME_BULLET prompt
which is hardened against inventing numbers/employers/scope.
"""
from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from auto_applier.gui.styles import (
    BG, BG_CARD, BORDER, PRIMARY, ACCENT, WARNING, TEXT, TEXT_LIGHT,
    TEXT_MUTED, FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)


class RefinePanel(tk.Toplevel):
    """Interactive resume refinement window.

    Lazy-loads the candidate list on idle so the window paints fast.
    Holds a dict of per-skill widget state so we can disable/enable
    buttons during async LLM calls.
    """

    def __init__(self, parent: tk.Misc, max_skills: int = 5) -> None:
        super().__init__(parent)
        self._max_skills = max_skills

        self.title("Refine your resume")
        self.configure(bg=BG)
        self.geometry("780x680")
        self.minsize(640, 520)

        self._cards: dict[str, dict] = {}  # skill -> widget dict
        self._added_count = 0
        self._learning_count = 0
        self._dismissed_count = 0

        self._build_ui()
        self.after(50, self._load)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=BG_CARD, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)

        ttk.Label(
            header, text="Refine your resume",
            style="CardHeading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(12, 0))

        ttk.Label(
            header, text=(
                "For each missing skill, tell us if you have experience. "
                "The AI will only use facts you provide — no invented details."
            ),
            style="CardSmall.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(2, 12))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        self._canvas, self._inner = make_scrollable(body)

        self._status_label = ttk.Label(
            self, text="Loading...", style="Muted.TLabel",
        )
        self._status_label.pack(side="bottom", anchor="w", padx=PAD_X, pady=(0, 8))

    # ------------------------------------------------------------------
    # Data load
    # ------------------------------------------------------------------

    def _load(self) -> None:
        from auto_applier.resume.refine import (
            check_resume_suggestion, collect_refine_candidates,
        )

        for child in self._inner.winfo_children():
            child.destroy()

        # Resume suggestion banner (if any)
        suggestions = check_resume_suggestion()
        for sug in suggestions:
            self._render_suggestion(sug)

        candidates = collect_refine_candidates()[: self._max_skills]
        if not candidates:
            ttk.Label(
                self._inner,
                text=(
                    "No skills are ready to review yet.\n\n"
                    "Apply to more jobs (or wait for more gap data to "
                    "accumulate) and come back."
                ),
                style="Small.TLabel",
                justify="left",
            ).pack(anchor="w", pady=PAD_Y)
            self._status_label.configure(text="No candidates")
            return

        for c in candidates:
            self._render_candidate(c)

        self._status_label.configure(
            text=f"{len(candidates)} skill(s) to review",
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_suggestion(self, sug) -> None:
        card = tk.Frame(self._inner, bg=BG_CARD, bd=1, relief="solid",
                        highlightbackground=WARNING, highlightthickness=1)
        card.pack(fill="x", pady=(0, PAD_Y), padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=PAD_X, pady=12)

        ttk.Label(
            inner,
            text=f"Suggestion: create a {sug.target_archetype}-focused resume",
            style="CardSubheading.TLabel",
        ).pack(anchor="w")

        ttk.Label(
            inner,
            text=(
                f"You've applied to {sug.evidence_count} {sug.target_archetype}-type "
                f"jobs using your '{sug.existing_resume}' resume.\n"
                f"Average match score was {sug.avg_score:.1f}/10 — a tailored "
                "resume could likely score higher."
            ),
            style="CardSmall.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        if sug.example_titles:
            ttk.Label(
                inner,
                text="Example jobs: " + ", ".join(sug.example_titles[:3]),
                style="CardSmall.TLabel",
            ).pack(anchor="w", pady=(2, 0))

        ttk.Label(
            inner,
            text=(
                "(Title-focused resume generation isn't automated yet. "
                "For now, create a new resume file and add it via "
                "the wizard.)"
            ),
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _render_candidate(self, cand) -> None:
        card = tk.Frame(self._inner, bg=BG_CARD, bd=1, relief="solid",
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=4, padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=12, pady=10)

        # Header row
        head_row = tk.Frame(inner, bg=BG_CARD)
        head_row.pack(fill="x")

        count_lbl = tk.Label(
            head_row, text=f" {cand.count} ", bg=PRIMARY, fg="white",
            font=FONT_SUBHEADING, padx=4,
        )
        count_lbl.pack(side="left")

        ttk.Label(
            head_row, text=cand.skill,
            style="CardSubheading.TLabel",
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            inner,
            text=(
                f"Used your '{cand.resume_label}' resume on jobs in the "
                f"{cand.archetype} track."
            ),
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        if cand.sample_companies:
            ttk.Label(
                inner,
                text="Examples: " + ", ".join(cand.sample_companies[:3]),
                style="CardSmall.TLabel",
            ).pack(anchor="w")

        # Action buttons
        btns = tk.Frame(inner, bg=BG_CARD)
        btns.pack(fill="x", pady=(8, 0))

        # State container: where the experience-input widgets / result
        # message replaces the buttons after the user picks an option.
        state_frame = tk.Frame(inner, bg=BG_CARD)
        state_frame.pack(fill="x")

        self._cards[cand.skill] = {
            "buttons": btns,
            "state_frame": state_frame,
            "candidate": cand,
        }

        ttk.Button(
            btns, text="I have experience",
            style="Primary.TButton",
            command=lambda c=cand: self._show_experience_form(c),
        ).pack(side="left")
        ttk.Button(
            btns, text="I'm learning",
            command=lambda c=cand: self._mark_state(c, "learning"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            btns, text="Not interested",
            command=lambda c=cand: self._mark_state(c, "not_interested"),
        ).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _mark_state(self, cand, state: str) -> None:
        from auto_applier.analysis import learning_goals
        try:
            learning_goals.set_state(cand.skill, state)
        except ValueError as exc:
            self._status_label.configure(text=f"Error: {exc}")
            return

        # Also mark prompted in EvolutionEngine for not_interested
        if state == "not_interested":
            from auto_applier.resume.evolution import EvolutionEngine
            EvolutionEngine().mark_prompted(cand.skill)
            self._dismissed_count += 1
        elif state == "learning":
            self._learning_count += 1

        self._collapse_card(cand.skill, f"Marked as {state}.")

    def _show_experience_form(self, cand) -> None:
        """Replace buttons with level + description input."""
        info = self._cards[cand.skill]
        # Hide the buttons row
        info["buttons"].pack_forget()

        form = info["state_frame"]
        for w in form.winfo_children():
            w.destroy()

        ttk.Label(
            form, text="Your level with this skill:",
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(8, 2))

        level_var = tk.StringVar(value="intermediate")
        level_combo = ttk.Combobox(
            form, textvariable=level_var,
            values=["beginner", "intermediate", "advanced", "expert"],
            state="readonly", width=20,
        )
        level_combo.pack(anchor="w")

        ttk.Label(
            form,
            text="Briefly describe a project or role (1-2 sentences, just the facts):",
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(8, 2))

        desc_text = tk.Text(
            form, height=3, wrap="word", font=FONT_BODY,
            bg=BG, fg=TEXT, bd=1, relief="solid",
            highlightbackground=BORDER,
        )
        desc_text.pack(fill="x")

        controls = tk.Frame(form, bg=BG_CARD)
        controls.pack(fill="x", pady=(8, 0))

        gen_btn = ttk.Button(
            controls, text="Generate bullets",
            style="Primary.TButton",
        )
        gen_btn.pack(side="left")
        ttk.Button(
            controls, text="Cancel",
            command=lambda: self._reset_card(cand),
        ).pack(side="left", padx=(8, 0))

        gen_btn.configure(
            command=lambda c=cand, l=level_var, t=desc_text, b=gen_btn:
                self._on_generate(c, l.get(), t.get("1.0", "end").strip(), b)
        )

    def _on_generate(self, cand, level: str, description: str, button) -> None:
        if not description:
            messagebox.showinfo(
                "Need more detail",
                "Please write a sentence or two describing your experience.",
            )
            return

        button.configure(state="disabled", text="Generating...")
        self._status_label.configure(
            text=f"Generating bullets for '{cand.skill}'...",
        )

        def _worker():
            from auto_applier.llm.router import LLMRouter
            from auto_applier.resume.manager import ResumeManager
            from auto_applier.resume.refine import (
                generate_bullets, save_confirmed_skill,
            )

            async def run():
                router = LLMRouter()
                await router.initialize()
                rm = ResumeManager(router)
                resume_text = rm.get_resume_text(cand.resume_label)
                bullets = await generate_bullets(
                    skill=cand.skill,
                    user_description=description,
                    resume_label=cand.resume_label,
                    resume_text=resume_text,
                    router=router,
                    level=level,
                )
                return bullets, rm

            try:
                bullets, rm = asyncio.run(run())
            except Exception as exc:
                self.after(0, lambda: self._on_bullets_failed(
                    cand, button, f"Error: {exc}",
                ))
                return

            if not bullets:
                self.after(0, lambda: self._on_bullets_failed(
                    cand, button,
                    "AI couldn't generate solid bullets. Try again with "
                    "more specifics (what, where, a concrete outcome).",
                ))
                return

            self.after(0, lambda b=bullets, r=rm: self._on_bullets_ready(
                cand, level, b, r,
            ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_bullets_failed(self, cand, button, message: str) -> None:
        button.configure(state="normal", text="Generate bullets")
        self._status_label.configure(text="Bullet generation failed.")
        messagebox.showwarning("No bullets", message)

    def _on_bullets_ready(self, cand, level: str, bullets: list, rm) -> None:
        info = self._cards[cand.skill]
        form = info["state_frame"]
        for w in form.winfo_children():
            w.destroy()

        ttk.Label(
            form, text="Proposed bullets (using only what you described):",
            style="CardSmall.TLabel",
        ).pack(anchor="w", pady=(8, 2))

        for b in bullets:
            row = tk.Frame(form, bg=BG_CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="*", bg=BG_CARD, fg=PRIMARY,
                     font=FONT_SUBHEADING).pack(side="left", padx=(0, 6))
            ttk.Label(
                row, text=b, style="Card.TLabel",
                wraplength=560, justify="left",
            ).pack(side="left", anchor="w")

        controls = tk.Frame(form, bg=BG_CARD)
        controls.pack(fill="x", pady=(8, 0))

        ttk.Button(
            controls, text="Add to resume",
            style="Primary.TButton",
            command=lambda b=bullets, r=rm:
                self._save_bullets(cand, level, b, r),
        ).pack(side="left")
        ttk.Button(
            controls, text="Discard",
            command=lambda: self._reset_card(cand),
        ).pack(side="left", padx=(8, 0))

    def _save_bullets(self, cand, level: str, bullets: list, rm) -> None:
        from auto_applier.resume.evolution import EvolutionEngine
        from auto_applier.resume.refine import save_confirmed_skill

        ok = save_confirmed_skill(
            resume_label=cand.resume_label,
            skill=cand.skill,
            level=level,
            bullets=bullets,
            resume_manager=rm,
        )
        if ok:
            EvolutionEngine().mark_prompted(cand.skill)
            self._added_count += 1
            self._collapse_card(
                cand.skill,
                f"Added '{cand.skill}' to '{cand.resume_label}' resume.",
            )
        else:
            self._status_label.configure(
                text=f"Could not save '{cand.skill}' — resume profile missing.",
            )

    def _reset_card(self, cand) -> None:
        """Restore the original action buttons after user cancels."""
        info = self._cards[cand.skill]
        for w in info["state_frame"].winfo_children():
            w.destroy()
        # Re-pack buttons row
        info["buttons"].pack(fill="x", pady=(8, 0))

    def _collapse_card(self, skill: str, message: str) -> None:
        """Replace the action area with a final-state message."""
        info = self._cards.get(skill)
        if not info:
            return
        info["buttons"].pack_forget()
        for w in info["state_frame"].winfo_children():
            w.destroy()
        ttk.Label(
            info["state_frame"], text=message,
            style="Success.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        # Update status with running totals
        self._status_label.configure(
            text=(
                f"Added: {self._added_count}  |  "
                f"Learning: {self._learning_count}  |  "
                f"Dismissed: {self._dismissed_count}"
            ),
        )
