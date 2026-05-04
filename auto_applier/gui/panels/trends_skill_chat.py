"""Skill-discussion chat dialog for the Trends panel.

User feedback 2026-05-04: the "What to learn next" panel surfaces
skills the user is missing across job postings, but the rendered
row gives no way to ask "what IS this?" or "how should I learn
it?". A click → AI chat dialog closes that gap.

Modelled on ``gui.steps.answers.ChatAssistDialog`` but scoped to
single-skill knowledge / decision-making rather than per-question
answer drafting. Differences:

  - No ``answer_var`` to mutate — we don't save anything off this
    dialog except the user's mental model.
  - Seed prompt is skill-centric: what is X, why does it appear in
    the user's gap data, recommended learning path, time-to-
    proficiency, common alternatives.
  - The dialog reads the user's resume + the candidate profile
    (just like ChatAssistDialog) so the LLM can give grounded
    advice instead of generic Wikipedia-style summaries.

Lifecycle / threading mirrors ChatAssistDialog: every user turn
spawns a worker thread that runs ``asyncio.run(router.complete())``,
marshals the reply back via ``self.after(0, ...)`` with a
TclError guard so a dialog close mid-flight doesn't crash.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from auto_applier.gui.styles import (
    ACCENT_TEXT, BG, BG_CARD, BORDER, PRIMARY, TEXT, TEXT_LIGHT, TEXT_MUTED,
    FONT_BODY, FONT_BUTTON, FONT_HEADING, FONT_SMALL, FONT_SUBHEADING,
    PAD_X, PAD_Y,
)

logger = logging.getLogger(__name__)


# Conversation cap mirrors ChatAssistDialog. Past 12 turns Gemma 4
# starts degrading on instruction-following with this prompt shape.
MAX_TURNS = 12
WARN_TURNS = 10


# Seed prompt template. We don't pre-fire an LLM call on dialog
# open (cold-start latency makes that feel slow) — instead we
# render a canned greeting and let the user type their first
# question. The system prompt frames every subsequent turn.
_SYSTEM_PROMPT = (
    "You are a senior career mentor helping a candidate decide "
    "whether and how to learn a specific technical skill. The "
    "candidate's resume and profile are provided as context. "
    "Be concrete, candid, and concise. Avoid generic advice — "
    "tailor every answer to what the candidate already knows and "
    "what they're targeting. Give realistic time estimates and "
    "concrete next steps (specific courses, project ideas, books). "
    "If the skill is overhyped or a poor ROI for this candidate, "
    "say so explicitly."
)


class TrendsSkillChatDialog(tk.Toplevel):
    """Modal multi-turn chat about a single skill from the trends panel."""

    def __init__(self, parent: tk.Misc, skill: str) -> None:
        super().__init__(parent)
        self._skill = skill
        # ``{role, text}`` entries; user/assist messages are
        # threaded back into the prompt on each turn.
        self._history: list[dict[str, str]] = []
        self._closed: bool = False
        self._busy: bool = False
        # Cached lazily on first send so dialog open stays snappy.
        self._resume_text: str | None = None
        self._candidate_profile: str | None = None

        self.title(f"Discuss: {skill[:40]}")
        self.configure(bg=BG)
        self.geometry("720x680")
        self.resizable(True, True)
        self.minsize(560, 520)

        self._build_ui()

        # Modal — match other panel popups.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _e: self._on_close())
        self.after_idle(self._input.focus_set)

        # Seed canned greeting so the dialog isn't blank on open.
        self.after(50, self._seed_chat)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Header — skill name. Wrapped so long names ("Direct GIS
        # experience within the telecommunications industry…") render
        # cleanly instead of overflowing.
        header = tk.Label(
            self, text=self._skill,
            font=FONT_HEADING, fg=PRIMARY, bg=BG,
            wraplength=680, justify="left", anchor="w",
        )
        header.pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4), fill="x")

        tk.Label(
            self,
            text=(
                "Ask anything about this skill — what it is, who needs it, "
                "how long to learn, whether it's worth your time."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG,
            wraplength=680, justify="left",
        ).pack(anchor="w", padx=PAD_X, pady=(0, 8))

        # Transcript card — read-only Text widget, same pattern as
        # ChatAssistDialog so users coming from the Answers wizard
        # find the UI familiar.
        transcript_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        transcript_card.pack(
            fill="both", expand=True, padx=PAD_X, pady=(0, 8),
        )
        transcript_frame = tk.Frame(transcript_card, bg=BG_CARD)
        transcript_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._transcript = tk.Text(
            transcript_frame, wrap="word", state="disabled",
            bg=BG_CARD, fg=TEXT, font=FONT_BODY,
            relief="flat", borderwidth=0, padx=6, pady=6,
            height=16,
        )
        scroll = ttk.Scrollbar(
            transcript_frame, orient="vertical",
            command=self._transcript.yview,
        )
        self._transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._transcript.pack(side="left", fill="both", expand=True)
        self._transcript.tag_configure(
            "user_label", foreground=PRIMARY, font=FONT_BUTTON,
        )
        self._transcript.tag_configure(
            "assist_label", foreground=ACCENT_TEXT, font=FONT_BUTTON,
        )
        self._transcript.tag_configure(
            "system_label", foreground=TEXT_MUTED, font=FONT_SMALL,
        )
        self._transcript.tag_configure(
            "msg_body", foreground=TEXT, font=FONT_BODY,
            lmargin1=8, lmargin2=8,
        )
        self._transcript.tag_configure(
            "system_body", foreground=TEXT_MUTED, font=FONT_SMALL,
            lmargin1=8, lmargin2=8,
        )

        # Status row above input — turn counter, "thinking..." marker.
        input_card = tk.Frame(self, bg=BG)
        input_card.pack(fill="x", padx=PAD_X, pady=(0, 4))

        self._status_lbl = tk.Label(
            input_card, text="",
            font=FONT_SMALL, fg=TEXT_MUTED, bg=BG,
        )
        self._status_lbl.pack(anchor="w", pady=(0, 2))

        input_row = tk.Frame(input_card, bg=BG)
        input_row.pack(fill="x")
        self._input = tk.Text(
            input_row, height=3, wrap="word",
            bg=BG_CARD, fg=TEXT, font=FONT_BODY,
            relief="solid", borderwidth=1, padx=6, pady=4,
        )
        self._input.pack(side="left", fill="x", expand=True)
        self._send_btn = ttk.Button(
            input_row, text="Send", style="Primary.TButton",
            command=self._on_send,
        )
        self._send_btn.pack(side="left", padx=(8, 0))

        # Enter-to-send, Shift+Enter for newline. Same as the
        # answers ChatAssistDialog so the UX is consistent.
        self._input.bind("<Return>", self._on_return_key)
        self._input.bind("<Shift-Return>", lambda _e: None)

        # Footer buttons row
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        ttk.Button(
            btn_row, text="Close",
            command=self._on_close,
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Transcript helpers (mirrored from answers.ChatAssistDialog)
    # ------------------------------------------------------------------

    def _append_transcript(self, role: str, text: str) -> None:
        if self._closed:
            return
        try:
            self._transcript.configure(state="normal")
            if self._transcript.index("end-1c") != "1.0":
                self._transcript.insert("end", "\n")
            label_tag = f"{role}_label"
            body_tag = "system_body" if role == "system" else "msg_body"
            label_text = {
                "user": "[you]", "assist": "[assistant]",
                "system": "[system]",
            }.get(role, f"[{role}]")
            self._transcript.insert("end", f"{label_text}\n", label_tag)
            self._transcript.insert("end", text.strip() + "\n", body_tag)
            self._transcript.see("end")
        finally:
            try:
                self._transcript.configure(state="disabled")
            except tk.TclError:
                pass

    def _set_status(self, text: str) -> None:
        if self._closed:
            return
        try:
            self._status_lbl.configure(text=text)
        except tk.TclError:
            pass

    def _set_input_enabled(self, enabled: bool) -> None:
        if self._closed:
            return
        state = "normal" if enabled else "disabled"
        try:
            self._input.configure(state=state)
            self._send_btn.configure(state=state)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Seed + send loop
    # ------------------------------------------------------------------

    def _seed_chat(self) -> None:
        """Render the canned greeting. No LLM call here — first real
        turn fires when the user hits Send."""
        greeting = (
            f"I can help you decide what to do about \"{self._skill}\". "
            "Try one of these to get started:\n\n"
            f"  • What is {self._skill}, in plain terms?\n"
            f"  • Is {self._skill} worth my time given my background?\n"
            f"  • What's the fastest path to be hireable in {self._skill}?\n"
            f"  • What roles actually use {self._skill}?\n\n"
            "Or ask whatever you like."
        )
        self._append_transcript("assist", greeting)

    def _on_return_key(self, _event) -> str:
        """Enter sends, Shift+Enter inserts newline. Returns 'break'
        so Tk doesn't ALSO insert a newline after our handler runs."""
        self._on_send()
        return "break"

    def _on_send(self) -> None:
        if self._closed or self._busy:
            return
        try:
            text = self._input.get("1.0", "end-1c").strip()
        except tk.TclError:
            return
        if not text:
            return

        # Soft cap warning + hard cap stop. Mirrors ChatAssistDialog.
        user_turn_count = sum(
            1 for h in self._history if h.get("role") == "user"
        )
        if user_turn_count >= MAX_TURNS:
            self._set_status(
                f"Conversation cap reached ({MAX_TURNS} turns). Close "
                "this dialog and re-open to start fresh."
            )
            return
        if user_turn_count == WARN_TURNS - 1:
            self._set_status(
                f"You're approaching the {MAX_TURNS}-turn cap — "
                "wrap up soon."
            )

        self._input.delete("1.0", "end")
        self._append_transcript("user", text)
        self._history.append({"role": "user", "text": text})

        self._busy = True
        self._set_input_enabled(False)
        self._set_status("Thinking...")

        # Snapshot history so a late append from another Send can't
        # poison this in-flight prompt.
        snapshot = list(self._history)
        threading.Thread(
            target=self._worker,
            args=(snapshot,),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Worker thread — talks to the LLM router
    # ------------------------------------------------------------------

    def _worker(self, history_snapshot: list[dict[str, str]]) -> None:
        """Background-thread entrypoint for one chat turn. Runs the
        LLM call via asyncio.run, marshals the reply back via
        self.after with a TclError guard."""
        from auto_applier.llm.router import LLMRouter

        # Lazy-resolve resume + profile on first send so dialog open
        # stays snappy.
        if self._resume_text is None:
            self._resume_text = self._collect_resume_text()
        if self._candidate_profile is None:
            self._candidate_profile = self._collect_profile()

        async def run() -> str:
            router = LLMRouter()
            await router.initialize()
            convo = self._format_conversation(history_snapshot)
            try:
                response = await router.complete(
                    prompt=(
                        f"Skill under discussion: {self._skill}\n\n"
                        f"Candidate profile:\n{self._candidate_profile or '(no profile)'}\n\n"
                        f"Resume excerpt:\n{(self._resume_text or '(no resume)')[:6000]}\n\n"
                        f"Conversation so far:\n{convo}\n\n"
                        "Respond as the senior career mentor described "
                        "in the system prompt. Keep it concrete and "
                        "honest. End with one suggested next question "
                        "the candidate could ask if relevant."
                    ),
                    system_prompt=_SYSTEM_PROMPT,
                    temperature=0.4,
                    max_tokens=600,
                    use_cache=False,
                )
                return response.text or ""
            except Exception as exc:
                return f"(error reaching the AI: {exc})"

        try:
            reply = asyncio.run(run())
        except Exception as exc:
            reply = f"(error reaching the AI: {exc})"

        try:
            self.after(0, lambda: self._render_reply(reply))
        except tk.TclError:
            pass

    def _render_reply(self, reply_text: str) -> None:
        if self._closed:
            return
        try:
            body = (reply_text or "").strip() or "(empty reply)"
            self._append_transcript("assist", body)
            self._history.append({"role": "assist", "text": body})
        finally:
            self._busy = False
            self._set_input_enabled(True)
            self._set_status("")
            try:
                self._input.focus_set()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_conversation(history: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for turn in history:
            role = turn.get("role", "")
            text = (turn.get("text") or "").strip()
            label = "User" if role == "user" else "Assistant"
            lines.append(f"{label}: {text}")
        return "\n\n".join(lines)

    @staticmethod
    def _collect_resume_text() -> str:
        """Concatenate every loaded resume's text. Mirrors how the
        answers ChatAssistDialog grounds itself."""
        try:
            from auto_applier.config import RESUMES_DIR, PROFILES_DIR
            from auto_applier.resume.parser import extract_text
            chunks: list[str] = []
            if RESUMES_DIR.exists():
                for p in RESUMES_DIR.iterdir():
                    if not p.is_file() or p.name.startswith("."):
                        continue
                    try:
                        text = extract_text(p)
                    except Exception:
                        continue
                    if text:
                        chunks.append(f"--- {p.name} ---\n{text}")
            return "\n\n".join(chunks)
        except Exception:
            return ""

    @staticmethod
    def _collect_profile() -> str:
        """Read user_config personal_info as a flat 'key: value' block.
        Profile gives the LLM gross context (location, target roles,
        years experience) without dumping the full resume."""
        try:
            import json as _json
            from auto_applier.config import USER_CONFIG_FILE
            if not USER_CONFIG_FILE.exists():
                return ""
            data = _json.loads(
                USER_CONFIG_FILE.read_text(encoding="utf-8")
            )
            personal = data.get("personal_info", {}) or {}
            keywords = data.get("search_keywords", []) or []
            location = data.get("location", "")
            lines = []
            if personal.get("name") or personal.get("first_name"):
                name = (
                    personal.get("name")
                    or f"{personal.get('first_name', '')} "
                       f"{personal.get('last_name', '')}".strip()
                )
                lines.append(f"Name: {name}")
            if location:
                lines.append(f"Location preference: {location}")
            if keywords:
                lines.append(
                    f"Target roles: {', '.join(str(k) for k in keywords)}"
                )
            return "\n".join(lines)
        except Exception:
            return ""

    def _on_close(self) -> None:
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
