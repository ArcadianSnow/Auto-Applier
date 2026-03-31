"""Chat panel for discussing missing skills with AI."""

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from auto_applier.gui.styles import (
    PRIMARY, PRIMARY_LIGHT, ACCENT, ACCENT_DARK, DANGER,
    BG, BG_CARD, TEXT, TEXT_LIGHT, TEXT_MUTED, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
    PAD_X, PAD_Y,
)

logger = logging.getLogger(__name__)


class SkillChatPanel(tk.Toplevel):
    """Chat interface for discussing missing skills with the AI.

    Opens as a modal window from the JobReviewPanel.  The user can
    discuss transferable skills with the AI before deciding to apply
    or skip.

    Parameters:
        parent: Parent window.
        router: :class:`LLMRouter` instance for AI responses.
        resume_text: The candidate's full resume text.
        job_description: The full job description text.
        missing_skills: List of skill name strings that are missing.
        on_done: Callback receiving ``"apply"`` or ``"skip"``.
    """

    _SYSTEM_PROMPT = (
        "You are a career coach helping a candidate decide whether to "
        "apply for a job. The candidate's resume is missing some skills "
        "listed in the job description. Help them identify transferable "
        "skills and articulate relevant experience. Be encouraging but "
        "honest. Keep responses concise (2-4 sentences)."
    )

    def __init__(
        self,
        parent: tk.Widget,
        router,
        resume_text: str,
        job_description: str,
        missing_skills: list[str],
        on_done: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._router = router
        self._resume_text = resume_text
        self._job_description = job_description
        self._missing_skills = missing_skills
        self._on_done = on_done
        self._decision_made = False

        # Conversation history for context in subsequent LLM calls
        self._history: list[dict[str, str]] = []

        self._setup_window()
        self._build_ui()
        self._send_initial_message()

        # Make modal
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._skip)

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.title("Discuss Missing Skills")
        self.configure(bg=BG)
        self.geometry("600x500")
        self.resizable(True, True)
        self.minsize(480, 400)

        # Center on parent
        self.update_idletasks()
        px = self.master.winfo_x()
        py = self.master.winfo_y()
        pw = self.master.winfo_width()
        ph = self.master.winfo_height()
        x = px + (pw - 600) // 2
        y = py + (ph - 500) // 2
        self.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Header: title + missing skill pills ---
        header = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=12)
        header.pack(fill="x")

        tk.Label(
            header, text="Discuss Missing Skills",
            font=FONT_HEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w")

        tk.Label(
            header, text="Chat with the AI about your relevant experience.",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
        ).pack(anchor="w", pady=(2, 8))

        # Skill pills row (wrapping frame)
        pills_frame = tk.Frame(header, bg=BG_CARD)
        pills_frame.pack(anchor="w", fill="x")

        for skill in self._missing_skills:
            pill = tk.Label(
                pills_frame,
                text=f"  {skill}  ",
                font=FONT_SMALL,
                fg="white",
                bg=DANGER,
                padx=6,
                pady=2,
            )
            pill.pack(side="left", padx=(0, 6), pady=2)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Chat area ---
        chat_container = tk.Frame(self, bg=BG)
        chat_container.pack(fill="both", expand=True)

        self._chat_canvas = tk.Canvas(
            chat_container, bg=BG, highlightthickness=0, bd=0,
        )
        chat_scroll = ttk.Scrollbar(
            chat_container, orient="vertical",
            command=self._chat_canvas.yview,
        )
        self._chat_canvas.configure(yscrollcommand=chat_scroll.set)

        self._chat_inner = tk.Frame(self._chat_canvas, bg=BG)
        self._chat_inner_id = self._chat_canvas.create_window(
            (0, 0), window=self._chat_inner, anchor="nw",
        )

        def _on_configure(_event=None):
            self._chat_canvas.configure(
                scrollregion=self._chat_canvas.bbox("all"),
            )
            self._chat_canvas.itemconfig(
                self._chat_inner_id,
                width=self._chat_canvas.winfo_width(),
            )

        self._chat_inner.bind("<Configure>", _on_configure)
        self._chat_canvas.bind("<Configure>", _on_configure)

        def _on_mousewheel(event):
            self._chat_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), "units",
            )

        self._chat_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        chat_scroll.pack(side="right", fill="y")
        self._chat_canvas.pack(side="left", fill="both", expand=True)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Input area ---
        input_frame = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=10)
        input_frame.pack(fill="x")

        self._entry = tk.Entry(
            input_frame, font=FONT_BODY,
            bg="white", fg=TEXT, relief="solid",
            bd=1, highlightcolor=PRIMARY,
        )
        self._entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._entry.bind("<Return>", lambda _e: self._send_message())

        self._send_btn = ttk.Button(
            input_frame, text="Send", style="Primary.TButton",
            command=self._send_message,
        )
        self._send_btn.pack(side="right", padx=(8, 0))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # --- Decision buttons ---
        footer = tk.Frame(self, bg=BG_CARD, padx=PAD_X, pady=10)
        footer.pack(fill="x")

        ttk.Button(
            footer, text="Apply with Context", style="Accent.TButton",
            command=self._apply,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            footer, text="Skip Job", style="Danger.TButton",
            command=self._skip,
        ).pack(side="left")

    # ------------------------------------------------------------------
    # Chat message rendering
    # ------------------------------------------------------------------

    def _add_message(self, text: str, role: str) -> None:
        """Add a chat bubble to the conversation.

        Args:
            text: Message content.
            role: ``"user"`` or ``"ai"``.
        """
        is_user = role == "user"

        row = tk.Frame(self._chat_inner, bg=BG)
        row.pack(fill="x", padx=12, pady=4)

        # Alignment spacer
        if is_user:
            tk.Frame(row, bg=BG, width=80).pack(side="left", fill="y")
        else:
            tk.Frame(row, bg=BG, width=80).pack(side="right", fill="y")

        bubble_bg = PRIMARY if is_user else BG_CARD
        bubble_fg = "white" if is_user else TEXT

        bubble = tk.Frame(
            row, bg=bubble_bg, padx=12, pady=8,
            highlightbackground=BORDER if not is_user else bubble_bg,
            highlightthickness=1 if not is_user else 0,
        )
        if is_user:
            bubble.pack(side="right", fill="x", expand=True)
        else:
            bubble.pack(side="left", fill="x", expand=True)

        label = tk.Label(
            bubble, text=text, font=FONT_BODY,
            fg=bubble_fg, bg=bubble_bg,
            wraplength=400, justify="left", anchor="w",
        )
        label.pack(anchor="w")

        # Sender tag
        sender_text = "You" if is_user else "AI Coach"
        sender_anchor = "e" if is_user else "w"
        tk.Label(
            row, text=sender_text, font=("Segoe UI", 8),
            fg=TEXT_MUTED, bg=BG,
        ).pack(anchor=sender_anchor)

        # Auto-scroll to bottom
        self._chat_inner.update_idletasks()
        self._chat_canvas.configure(
            scrollregion=self._chat_canvas.bbox("all"),
        )
        self._chat_canvas.yview_moveto(1.0)

    def _add_typing_indicator(self) -> tk.Frame:
        """Show a 'typing...' indicator. Returns the frame to destroy later."""
        row = tk.Frame(self._chat_inner, bg=BG)
        row.pack(fill="x", padx=12, pady=4)

        tk.Frame(row, bg=BG, width=80).pack(side="right", fill="y")

        bubble = tk.Frame(
            row, bg=BG_CARD, padx=12, pady=8,
            highlightbackground=BORDER, highlightthickness=1,
        )
        bubble.pack(side="left")

        tk.Label(
            bubble, text="Thinking...", font=FONT_SMALL,
            fg=TEXT_MUTED, bg=BG_CARD,
        ).pack(anchor="w")

        self._chat_inner.update_idletasks()
        self._chat_canvas.configure(
            scrollregion=self._chat_canvas.bbox("all"),
        )
        self._chat_canvas.yview_moveto(1.0)

        return row

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    def _send_initial_message(self) -> None:
        """Insert the first AI message without an LLM call."""
        skills_list = ", ".join(self._missing_skills)
        initial = (
            f"I see you're missing these skills for this role: {skills_list}. "
            "Would you like to discuss your relevant experience with any of "
            "these? I can help you articulate transferable skills."
        )
        self._history.append({"role": "ai", "text": initial})
        self._add_message(initial, "ai")

    def _send_message(self) -> None:
        """Handle user pressing Send or Enter."""
        text = self._entry.get().strip()
        if not text:
            return

        self._entry.delete(0, "end")
        self._add_message(text, "user")
        self._history.append({"role": "user", "text": text})

        # Disable input while waiting
        self._entry.configure(state="disabled")
        self._send_btn.configure(state="disabled")

        typing_indicator = self._add_typing_indicator()

        self._get_ai_response(text, typing_indicator)

    def _get_ai_response(self, user_text: str, typing_indicator: tk.Frame) -> None:
        """Send the conversation to the LLM in a background thread."""

        def _run():
            async def _async():
                # Build conversation context
                history_text = "\n".join(
                    f"{'User' if m['role'] == 'user' else 'Coach'}: {m['text']}"
                    for m in self._history
                )

                prompt = (
                    f"Resume:\n{self._resume_text[:2000]}\n\n"
                    f"Job Description:\n{self._job_description[:1500]}\n\n"
                    f"Missing Skills: {', '.join(self._missing_skills)}\n\n"
                    f"Conversation so far:\n{history_text}\n\n"
                    "Respond helpfully to the user's latest message."
                )

                result = await self._router.complete(
                    prompt,
                    system_prompt=self._SYSTEM_PROMPT,
                    temperature=0.5,
                    max_tokens=512,
                    use_cache=False,
                )
                return result.text

            try:
                result = asyncio.run(_async())
            except Exception as exc:
                logger.warning("Chat LLM call failed: %s", exc)
                result = (
                    "I'm having trouble connecting to the AI right now. "
                    "You can still decide whether to apply based on your "
                    "own assessment of the skill gaps."
                )

            self.after(0, lambda: self._show_ai_response(result, typing_indicator))

        threading.Thread(target=_run, daemon=True).start()

    def _show_ai_response(self, text: str, typing_indicator: tk.Frame) -> None:
        """Display the AI response and re-enable input."""
        typing_indicator.destroy()
        self._add_message(text, "ai")
        self._history.append({"role": "ai", "text": text})

        self._entry.configure(state="normal")
        self._send_btn.configure(state="normal")
        self._entry.focus_set()

    # ------------------------------------------------------------------
    # Decision handlers
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """User chose to apply after discussion."""
        if not self._decision_made:
            self._decision_made = True
            if self._on_done:
                self._on_done("apply")
        self.destroy()

    def _skip(self) -> None:
        """User chose to skip the job."""
        if not self._decision_made:
            self._decision_made = True
            if self._on_done:
                self._on_done("skip")
        self.destroy()
