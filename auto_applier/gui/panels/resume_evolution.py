"""Resume evolution panel -- confirm skills and approve resume updates."""

import asyncio
import json
import logging
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Optional

from auto_applier.gui.styles import (
    PRIMARY, PRIMARY_LIGHT, ACCENT, ACCENT_DARK,
    WARNING, DANGER,
    BG, BG_CARD, TEXT, TEXT_LIGHT, TEXT_MUTED, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)
from auto_applier.resume.evolution import EvolutionTrigger

logger = logging.getLogger(__name__)

# Category display colors
_CATEGORY_COLORS = {
    "skill": PRIMARY,
    "certification": WARNING,
    "experience": ACCENT,
    "tool": "#8B5CF6",  # purple
    "other": TEXT_LIGHT,
}


class ResumeEvolutionPanel(tk.Toplevel):
    """Panel for confirming skills and approving resume bullet updates.

    Shown after a run completes when skill gaps have triggered evolution.
    Each triggered skill is displayed as a card.  The user can confirm
    they have the skill, provide context, and generate AI-powered resume
    bullet points -- or dismiss skills they do not possess.

    Parameters:
        parent: Parent window.
        triggers: List of :class:`EvolutionTrigger` from
            :meth:`EvolutionEngine.check_triggers`.
        router: :class:`LLMRouter` instance for bullet generation.
        resume_manager: :class:`ResumeManager` instance for profile I/O.
    """

    def __init__(
        self,
        parent: tk.Widget,
        triggers: list[EvolutionTrigger],
        router,
        resume_manager,
    ) -> None:
        super().__init__(parent)
        self._triggers = triggers
        self._router = router
        self._resume_manager = resume_manager

        # Track per-card state: maps skill_name -> dict of widgets/state
        self._cards: dict[str, dict] = {}

        self._setup_window()
        self._build_ui()

        # Make modal
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._done)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.title("Resume Evolution")
        self.configure(bg=BG)
        self.geometry("700x600")
        self.resizable(True, True)
        self.minsize(560, 450)

        # Center on parent
        self.update_idletasks()
        px = self.master.winfo_x()
        py = self.master.winfo_y()
        pw = self.master.winfo_width()
        ph = self.master.winfo_height()
        x = px + (pw - 700) // 2
        y = py + (ph - 600) // 2
        self.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Header ---
        header = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=14)
        header.pack(fill="x")

        tk.Label(
            header, text="Skills Evolution",
            font=FONT_HEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "These skills appeared frequently in your applications. "
                "Confirm which ones you have to strengthen your resume."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            wraplength=640, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Scrollable card list ---
        body_container = ttk.Frame(self)
        body_container.pack(fill="both", expand=True)
        _canvas, body = make_scrollable(body_container)
        self._body = body

        if not self._triggers:
            tk.Label(
                body,
                text="No skill evolution triggers at this time.",
                font=FONT_BODY, fg=TEXT_MUTED, bg=BG,
            ).pack(pady=40)
        else:
            for trigger in self._triggers:
                self._build_skill_card(body, trigger)

        # --- Footer ---
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        footer = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=12)
        footer.pack(fill="x")

        ttk.Button(
            footer, text="Done", style="Primary.TButton",
            command=self._done,
        ).pack(side="right")

        self._status_label = tk.Label(
            footer, text="", font=FONT_SMALL,
            fg=TEXT_LIGHT, bg=BG_CARD,
        )
        self._status_label.pack(side="left")

    def _build_skill_card(
        self, parent: tk.Widget, trigger: EvolutionTrigger,
    ) -> None:
        """Build a single skill card with action buttons."""
        key = trigger.skill_name

        card = tk.Frame(
            parent, bg=BG_CARD,
            highlightbackground=BORDER, highlightthickness=1,
            padx=16, pady=12,
        )
        card.pack(fill="x", padx=PAD_X, pady=(PAD_Y // 2, 0))

        # --- Top row: skill name + badges ---
        top_row = tk.Frame(card, bg=BG_CARD)
        top_row.pack(fill="x")

        tk.Label(
            top_row, text=trigger.skill_name.title(),
            font=FONT_SUBHEADING, fg=TEXT, bg=BG_CARD,
        ).pack(side="left")

        # "Seen N times" badge
        seen_badge = tk.Label(
            top_row,
            text=f"  Seen {trigger.times_seen} times  ",
            font=("Segoe UI", 8),
            fg="white", bg=WARNING,
            padx=4, pady=1,
        )
        seen_badge.pack(side="left", padx=(8, 0))

        # Category tag
        cat_color = _CATEGORY_COLORS.get(trigger.category, TEXT_LIGHT)
        cat_badge = tk.Label(
            top_row,
            text=f"  {trigger.category}  ",
            font=("Segoe UI", 8),
            fg="white", bg=cat_color,
            padx=4, pady=1,
        )
        cat_badge.pack(side="left", padx=(6, 0))

        # Resume label
        if trigger.resume_label:
            tk.Label(
                top_row,
                text=f"Resume: {trigger.resume_label}",
                font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
            ).pack(side="right")

        # --- Button row ---
        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(8, 0))

        have_btn = ttk.Button(
            btn_row, text="I Have This", style="Accent.TButton",
            command=lambda k=key, c=card, t=trigger: self._on_have(k, c, t),
        )
        have_btn.pack(side="left", padx=(0, 8))

        dont_btn = ttk.Button(
            btn_row, text="I Don't", style="TButton",
            command=lambda k=key, c=card, t=trigger: self._on_dont(k, c, t),
        )
        dont_btn.pack(side="left")

        # --- Expandable detail area (hidden initially) ---
        detail_frame = tk.Frame(card, bg=BG_CARD)
        # detail_frame is packed later when "I Have This" is clicked

        # --- Bullet results area (hidden initially) ---
        bullets_frame = tk.Frame(card, bg=BG_CARD)

        self._cards[key] = {
            "card": card,
            "trigger": trigger,
            "have_btn": have_btn,
            "dont_btn": dont_btn,
            "btn_row": btn_row,
            "detail_frame": detail_frame,
            "bullets_frame": bullets_frame,
            "confirmed": False,
            "dismissed": False,
        }

    # ------------------------------------------------------------------
    # "I Have This" flow
    # ------------------------------------------------------------------

    def _on_have(self, key: str, card: tk.Frame, trigger: EvolutionTrigger) -> None:
        """Expand the card to collect skill details."""
        state = self._cards[key]
        if state["confirmed"] or state["dismissed"]:
            return

        # Hide action buttons
        state["have_btn"].configure(state="disabled")
        state["dont_btn"].configure(state="disabled")

        detail = state["detail_frame"]
        detail.pack(fill="x", pady=(10, 0))

        # Level dropdown
        level_row = tk.Frame(detail, bg=BG_CARD)
        level_row.pack(fill="x", pady=(0, 6))

        tk.Label(
            level_row, text="Proficiency:", font=FONT_BODY,
            fg=TEXT, bg=BG_CARD,
        ).pack(side="left")

        level_var = tk.StringVar(value="Intermediate")
        level_combo = ttk.Combobox(
            level_row, textvariable=level_var,
            values=["Beginner", "Intermediate", "Advanced", "Expert"],
            state="readonly", width=16, font=FONT_BODY,
        )
        level_combo.pack(side="left", padx=(8, 0))
        state["level_var"] = level_var

        # Notes entry
        tk.Label(
            detail, text="Briefly describe your experience:",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w", pady=(2, 2))

        notes_entry = tk.Entry(
            detail, font=FONT_BODY,
            bg="white", fg=TEXT, relief="solid", bd=1,
        )
        notes_entry.pack(fill="x", ipady=4)
        state["notes_entry"] = notes_entry

        # Generate button
        gen_btn = ttk.Button(
            detail, text="Generate Bullets", style="Primary.TButton",
            command=lambda: self._generate_bullets(key),
        )
        gen_btn.pack(anchor="w", pady=(8, 0))
        state["gen_btn"] = gen_btn

    # ------------------------------------------------------------------
    # "I Don't" flow
    # ------------------------------------------------------------------

    def _on_dont(self, key: str, card: tk.Frame, trigger: EvolutionTrigger) -> None:
        """Mark the skill as dismissed and gray out the card."""
        state = self._cards[key]
        if state["confirmed"] or state["dismissed"]:
            return

        state["dismissed"] = True

        # Mark as prompted so it won't reappear
        from auto_applier.resume.evolution import EvolutionEngine

        engine = EvolutionEngine()
        engine.mark_prompted(trigger.skill_name)

        # Gray out card
        card.configure(highlightbackground=TEXT_MUTED)
        for child in card.winfo_children():
            self._gray_out(child)

        # Replace buttons with "Dismissed" label
        state["have_btn"].pack_forget()
        state["dont_btn"].pack_forget()

        tk.Label(
            state["btn_row"], text="Dismissed",
            font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
        ).pack(side="left")

        self._status_label.configure(
            text=f"Dismissed '{trigger.skill_name.title()}'",
        )

    @staticmethod
    def _gray_out(widget: tk.Widget) -> None:
        """Recursively set text color to muted for visual graying."""
        try:
            if isinstance(widget, tk.Label):
                widget.configure(fg=TEXT_MUTED)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            ResumeEvolutionPanel._gray_out(child)

    # ------------------------------------------------------------------
    # Bullet generation via LLM
    # ------------------------------------------------------------------

    def _generate_bullets(self, key: str) -> None:
        """Kick off bullet generation in a background thread."""
        state = self._cards[key]
        trigger = state["trigger"]
        skill_name = trigger.skill_name
        level = state["level_var"].get()
        notes = state["notes_entry"].get().strip()
        resume_label = trigger.resume_label

        if not notes:
            notes = f"I have {level.lower()}-level experience with {skill_name}."

        # Disable generate button and show progress
        state["gen_btn"].configure(state="disabled")
        state["gen_btn"].configure(text="Generating...")

        def _run():
            async def _async():
                from auto_applier.llm.prompts import RESUME_BULLET

                resume_text = self._resume_manager.get_resume_text(resume_label)
                prompt = RESUME_BULLET.format(
                    skill_name=skill_name,
                    skill_level=level,
                    user_context=notes,
                    resume_excerpt=resume_text[:2000],
                )
                result = await self._router.complete(
                    prompt, RESUME_BULLET.system, temperature=0.4,
                )
                return result.text

            try:
                result = asyncio.run(_async())
            except Exception as exc:
                logger.warning("Bullet generation failed for '%s': %s", skill_name, exc)
                result = None

            self.after(0, lambda: self._show_bullets(key, result))

        threading.Thread(target=_run, daemon=True).start()

    def _show_bullets(self, key: str, raw_result: Optional[str]) -> None:
        """Display generated bullets for approval."""
        state = self._cards[key]

        # Hide the generate button
        state["gen_btn"].pack_forget()

        bullets_frame = state["bullets_frame"]
        bullets_frame.pack(fill="x", pady=(10, 0))

        # Parse bullets from LLM response
        bullets = self._parse_bullets(raw_result)

        if not bullets:
            tk.Label(
                bullets_frame,
                text="Could not generate bullets. Try again or edit manually.",
                font=FONT_SMALL, fg=DANGER, bg=BG_CARD,
            ).pack(anchor="w")

            retry_btn = ttk.Button(
                bullets_frame, text="Retry", style="Primary.TButton",
                command=lambda: self._retry_generate(key, bullets_frame),
            )
            retry_btn.pack(anchor="w", pady=(4, 0))
            return

        tk.Label(
            bullets_frame, text="Generated Bullet Points:",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 6))

        state["bullet_widgets"] = []

        for i, bullet in enumerate(bullets):
            brow = tk.Frame(bullets_frame, bg=BG_CARD)
            brow.pack(fill="x", pady=2)

            # Editable text
            text_var = tk.StringVar(value=bullet)
            entry = tk.Entry(
                brow, textvariable=text_var, font=FONT_BODY,
                bg="white", fg=TEXT, relief="solid", bd=1,
            )
            entry.pack(side="left", fill="x", expand=True, ipady=3)

            # Approve / reject per bullet
            approve_btn = ttk.Button(
                brow, text="Keep",
                command=lambda e=entry, b=brow: self._approve_bullet(e, b),
            )
            approve_btn.pack(side="right", padx=(4, 0))

            reject_btn = ttk.Button(
                brow, text="Drop",
                command=lambda b=brow: self._reject_bullet(b),
            )
            reject_btn.pack(side="right", padx=(4, 0))

            state["bullet_widgets"].append({
                "frame": brow,
                "entry": entry,
                "text_var": text_var,
                "approved": False,
                "rejected": False,
            })

        # "Save All Approved" button
        save_btn = ttk.Button(
            bullets_frame, text="Save Approved Bullets", style="Accent.TButton",
            command=lambda: self._save_confirmed(key),
        )
        save_btn.pack(anchor="w", pady=(10, 0))
        state["save_btn"] = save_btn

    def _retry_generate(self, key: str, bullets_frame: tk.Frame) -> None:
        """Clear failed bullets and retry generation."""
        for child in bullets_frame.winfo_children():
            child.destroy()
        bullets_frame.pack_forget()

        state = self._cards[key]
        # Reshow generate button
        state["gen_btn"] = ttk.Button(
            state["detail_frame"], text="Generate Bullets",
            style="Primary.TButton",
            command=lambda: self._generate_bullets(key),
        )
        state["gen_btn"].pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _approve_bullet(entry: tk.Entry, row: tk.Frame) -> None:
        """Visually mark a bullet as approved."""
        entry.configure(bg=PRIMARY_LIGHT, state="disabled")
        # Disable buttons in the row
        for child in row.winfo_children():
            if isinstance(child, ttk.Button):
                child.configure(state="disabled")

    @staticmethod
    def _reject_bullet(row: tk.Frame) -> None:
        """Visually strike through and gray out a rejected bullet."""
        for child in row.winfo_children():
            if isinstance(child, tk.Entry):
                child.configure(fg=TEXT_MUTED, state="disabled")
            if isinstance(child, ttk.Button):
                child.configure(state="disabled")

    @staticmethod
    def _parse_bullets(raw: Optional[str]) -> list[str]:
        """Extract bullet strings from LLM output.

        The prompt asks for a JSON array, but the model might return
        markdown or plain text.  Try JSON first, then fall back to
        line-splitting.
        """
        if not raw:
            return []

        # Try JSON array parse
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(b).strip() for b in parsed if str(b).strip()]
        except (json.JSONDecodeError, TypeError):
            pass

        # Try extracting a JSON array from within the text
        import re

        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return [str(b).strip() for b in parsed if str(b).strip()]
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: split by lines, strip bullets/dashes
        lines = []
        for line in raw.strip().splitlines():
            cleaned = line.strip().lstrip("-*").strip()
            if cleaned and len(cleaned) > 10:
                lines.append(cleaned)
        return lines[:3]

    # ------------------------------------------------------------------
    # Save confirmed skill to profile
    # ------------------------------------------------------------------

    def _save_confirmed(self, key: str) -> None:
        """Save approved bullets to the resume profile."""
        state = self._cards[key]
        trigger = state["trigger"]

        # Collect approved (non-rejected, non-empty) bullets
        approved_bullets = []
        for bw in state.get("bullet_widgets", []):
            entry = bw["entry"]
            # Check if the entry is not grayed out (rejected)
            try:
                fg = entry.cget("fg")
            except tk.TclError:
                fg = TEXT
            if fg != TEXT_MUTED:
                text = bw["text_var"].get().strip()
                if text:
                    approved_bullets.append(text)

        if not approved_bullets:
            self._status_label.configure(
                text="No bullets approved. Check at least one.",
                fg=DANGER,
            )
            return

        # Build confirmed skill entry
        level = state.get("level_var", tk.StringVar(value="Intermediate")).get()
        notes = state.get("notes_entry")
        notes_text = notes.get().strip() if notes else ""

        confirmed_entry = {
            "name": trigger.skill_name,
            "level": level,
            "notes": notes_text,
            "bullets": approved_bullets,
        }

        # Load profile, append, save
        resume_label = trigger.resume_label
        if resume_label:
            profile = self._resume_manager.get_profile(resume_label)
            if profile:
                confirmed_list = profile.get("confirmed_skills", [])
                # Remove existing entry for this skill if any
                confirmed_list = [
                    s for s in confirmed_list
                    if s.get("name", "").lower() != trigger.skill_name.lower()
                ]
                confirmed_list.append(confirmed_entry)
                profile["confirmed_skills"] = confirmed_list
                self._resume_manager.save_profile(resume_label, profile)
                logger.info(
                    "Saved confirmed skill '%s' to profile '%s' with %d bullets",
                    trigger.skill_name, resume_label, len(approved_bullets),
                )

        # Mark as prompted
        from auto_applier.resume.evolution import EvolutionEngine

        engine = EvolutionEngine()
        engine.mark_prompted(trigger.skill_name)

        state["confirmed"] = True

        # Update card visual
        card = state["card"]
        card.configure(highlightbackground=ACCENT)

        # Disable save button
        if "save_btn" in state:
            state["save_btn"].configure(state="disabled")
            state["save_btn"].configure(text="Saved")

        self._status_label.configure(
            text=f"Saved '{trigger.skill_name.title()}' with {len(approved_bullets)} bullets.",
            fg=ACCENT,
        )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _done(self) -> None:
        """Close the panel."""
        self.destroy()
