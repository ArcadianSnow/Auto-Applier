"""Step 7: Pre-configured answers for common application questions."""
import asyncio
import json
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.gui.styles import (
    ACCENT_TEXT, BG, BG_CARD, PRIMARY, WARNING, TEXT, TEXT_LIGHT,
    TEXT_MUTED, BORDER,
    FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_BUTTON,
    PAD_X, PAD_Y, make_scrollable,
)


# Matches placeholder shapes in question templates that haven't been
# customized: [tool], <topic>, {company}, ___, etc. Used both to flag
# template entries in the row list and to bypass LLM calls when the
# user clicks the "?" assist button — otherwise the model hallucinates
# a fill-in for the placeholder (see live run 2026-05-01: "Testpilot"
# returned as a tool the candidate "lists in the question").
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]|<[^>]+>|\{[^}]+\}|_{3,}")


def _find_placeholder(question: str) -> str | None:
    """Return the literal placeholder substring if the question is a
    template, else None.
    """
    if not question:
        return None
    m = PLACEHOLDER_RE.search(question)
    return m.group(0) if m else None


# Matches the trailing `SUGGESTED: ...` line that ANSWER_CHAT prompts
# the LLM to emit on every reply. The capture grabs everything from
# the first non-space character after the colon to end-of-line. The
# (?im) flag-set makes the match case-insensitive (Gemma 4 occasionally
# capitalizes oddly) and multiline-aware so the `$` anchors at the
# physical line break rather than end-of-string.
SUGGESTED_LINE_RE = re.compile(
    r"^[ \t]*SUGGESTED[ \t]*:[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_suggested(text: str) -> str:
    """Pull the proposed answer out of a chat reply.

    The LLM is instructed to finish each reply with a line of the
    form ``SUGGESTED: <answer>``. We search for the LAST such line
    so a model that "thinks aloud" with an interim suggestion mid-
    reply still ends up surfacing its final pick.

    Falls back to the whole reply (stripped) if no SUGGESTED line is
    present — that way a model that ignores the instruction at least
    still drives the preview pane with something the user can read.
    Empty input returns "".
    """
    if not text:
        return ""
    matches = list(SUGGESTED_LINE_RE.finditer(text))
    if matches:
        # Last match wins — the prompt asks for it at the end.
        return matches[-1].group(1).strip()
    return text.strip()

# Default common questions and their input types.
# "dropdown" means use a Combobox; "entry" means a plain text field.
COMMON_QUESTIONS: list[tuple[str, str, list[str] | None]] = [
    ("Are you authorized to work in this country?", "dropdown", ["Yes", "No"]),
    ("Do you require visa sponsorship?", "dropdown", ["Yes", "No"]),
    ("What is your highest level of education?", "dropdown", [
        "High School", "Associate's", "Bachelor's", "Master's", "Doctorate", "Other",
    ]),
    ("How many years of professional experience do you have?", "entry", None),
    ("Are you willing to relocate?", "dropdown", ["Yes", "No", "Depends"]),
    ("What are your salary expectations?", "entry", None),
    ("What is your earliest start date?", "entry", None),
    ("Do you have a valid driver's license?", "dropdown", ["Yes", "No"]),
    ("Are you at least 18 years of age?", "dropdown", ["Yes", "No"]),
    ("Have you previously worked for this company?", "dropdown", ["Yes", "No"]),
    ("What is your preferred work arrangement?", "dropdown", [
        "Remote", "Hybrid", "On-site", "No preference",
    ]),
    # Removed "Do you have experience with [specific tool]?" — the
    # placeholder shape doesn't get clearer with the AI assist, and
    # the form_filler's per-job context-fill handles tool questions
    # better at run time using the actual JD.
    ("What is your current employment status?", "dropdown", [
        "Employed", "Unemployed", "Student", "Freelance", "Other",
    ]),
    ("Are you open to contract positions?", "dropdown", ["Yes", "No"]),
    # Removed "How did you hear about this position?" — form_filler's
    # _match_contextual auto-answers this at run time using the
    # platform display name (Indeed/Dice/ZipRecruiter). Asking the
    # user upfront just fights that auto-answer.
    ("Do you have any security clearances?", "entry", None),
    ("What languages do you speak?", "entry", None),
    ("Are you willing to undergo a background check?", "dropdown", ["Yes", "No"]),
    # Removed "Do you have a professional certification relevant to
    # this role?" — certifications come from the resume; the LLM
    # context-fill at run time has the resume text, so a lossy
    # upfront summary doesn't help.
    ("What is your notice period at your current job?", "entry", None),
]


class AnswersStep(ttk.Frame):
    """Pre-configured answers for common application questions."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def validate(self) -> bool:
        """No required answers — every question is optional. But
        persist what's been entered on advance so users who fill
        some answers and bail don't lose them, and so doctor
        stops complaining that answers.json is missing.
        """
        try:
            self.wizard.save_answers()
        except Exception:
            pass
        return True

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Common Application Questions", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text=(
                "Fill in your default answers. New questions encountered "
                "during runs will appear here next time."
            ),
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Scrollable area for the question list
        scroll_container = ttk.Frame(self)
        scroll_container.pack(fill="both", expand=True, padx=PAD_X, pady=(0, PAD_Y))
        _canvas, inner = make_scrollable(scroll_container)

        # Load saved answers
        saved = self._load_saved_answers()

        # Render common questions
        card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        card.pack(fill="x", padx=4, pady=4)

        tk.Label(
            card, text="Standard Questions", font=FONT_SUBHEADING,
            fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 12))

        for question, input_type, options in COMMON_QUESTIONS:
            self._add_question_row(card, question, input_type, options, saved)

        # Unanswered questions from previous runs
        unanswered = self._load_unanswered()
        if unanswered:
            sep = ttk.Separator(inner, orient="horizontal")
            sep.pack(fill="x", padx=4, pady=12)

            new_card = tk.Frame(
                inner, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=20, pady=16,
            )
            new_card.pack(fill="x", padx=4, pady=4)
            # Stash the card frame + count label so the per-row delete
            # handler can update them when entries are removed.
            self._unanswered_card = new_card

            header_row = tk.Frame(new_card, bg=BG_CARD)
            header_row.pack(fill="x", pady=(0, 12))

            tk.Label(
                header_row,
                text="New Questions from Recent Applications",
                font=FONT_SUBHEADING, fg=WARNING, bg=BG_CARD,
            ).pack(side="left")

            self._unanswered_count_label = tk.Label(
                header_row,
                text=f"  ({len(unanswered)} new)",
                font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
            )
            self._unanswered_count_label.pack(side="left")

            # Bulk-clear button — for when a user just wants to nuke
            # the whole suggested-questions list. Confirmation guards
            # against fat-finger clears.
            ttk.Button(
                header_row,
                text="Clear all",
                command=self._clear_all_unanswered,
            ).pack(side="right")

            for question in unanswered:
                self._add_question_row(
                    new_card, question, "entry", None, saved,
                    deletable=True,
                )

    # ------------------------------------------------------------------
    # Delete-question handlers (only fire for unanswered/new rows)
    # ------------------------------------------------------------------

    def _delete_unanswered(
        self, question: str, row_widget: tk.Widget,
    ) -> None:
        """Remove a single question from unanswered.json + the UI.

        Confirms first because the user may have meant to click "?"
        (AI assist) instead. Cleans the wizard's answer_vars mapping
        too so the eventual save doesn't ressurrect the entry.

        The confirmation dialog is deferred via ``after(0, ...)``
        because Tk's modal ``askyesno`` was eating the original click
        when the wizard's outer modal had grab_set: the first ✕ click
        registered, but the messagebox failed to surface, so the user
        had to click again to actually see the dialog. Flushing
        idle tasks first + scheduling the dialog past the click
        handler resolves both that and the FocusIn auto-scroll
        regression that fired against the messagebox toplevel.
        """
        # Force pending UI updates to drain so the click that triggered
        # us is fully processed before we yield to the modal.
        try:
            self.update_idletasks()
        except Exception:
            pass

        def _confirm() -> None:
            if not messagebox.askyesno(
                "Remove question?",
                f"Remove this question from your saved list?\n\n"
                f"  {question[:200]}\n\n"
                "It won't appear in this wizard again unless a future "
                "application encounters it.",
                parent=self.winfo_toplevel(),
            ):
                return
            self._remove_unanswered_from_disk([question])
            # Forget the StringVar so save_to_config doesn't write it back.
            self.wizard.answer_vars.pop(question, None)
            try:
                row_widget.destroy()
            except Exception:
                pass
            self._refresh_unanswered_count()

        # Defer past the current click handler so Tk doesn't swallow
        # the first click while the messagebox modal is being built.
        self.after(0, _confirm)

    def _clear_all_unanswered(self) -> None:
        """Wipe data/unanswered.json + remove every new-question row.

        Same defer-past-click pattern as ``_delete_unanswered`` so the
        wizard's outer grab doesn't swallow the first click.
        """
        try:
            current = self._load_unanswered()
        except Exception:
            current = []
        if not current:
            return

        try:
            self.update_idletasks()
        except Exception:
            pass

        def _confirm() -> None:
            if not messagebox.askyesno(
                "Clear all suggested questions?",
                f"Remove all {len(current)} suggested questions from your "
                "list?\n\nThey'll only reappear if future applications "
                "encounter them.",
                parent=self.winfo_toplevel(),
            ):
                return
            self._remove_unanswered_from_disk(current)
            # Drop StringVars for the cleared questions so they don't
            # round-trip back into answers.json on save.
            for q in current:
                self.wizard.answer_vars.pop(q, None)
            # Destroy the entire new-questions card so the section
            # disappears cleanly. Reload-and-rebuild would also work but
            # is heavier and would lose scroll position.
            card = getattr(self, "_unanswered_card", None)
            if card is not None:
                try:
                    card.destroy()
                except Exception:
                    pass

        self.after(0, _confirm)

    def _remove_unanswered_from_disk(self, questions: list[str]) -> None:
        """Delete listed questions from data/unanswered.json.

        Tolerates the file being a flat list, a {questions: [...]}
        wrapped dict, or a list of {question, ...} entry dicts —
        same shapes _load_unanswered handles.
        """
        if not UNANSWERED_FILE.exists():
            return
        try:
            with open(UNANSWERED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        target = {q.strip() for q in questions if q.strip()}

        if isinstance(data, list):
            cleaned: list = []
            for item in data:
                if isinstance(item, str):
                    if item.strip() not in target:
                        cleaned.append(item)
                elif isinstance(item, dict):
                    q = str(item.get("question", "")).strip()
                    if q and q not in target:
                        cleaned.append(item)
                else:
                    cleaned.append(item)
            payload: object = cleaned
        elif isinstance(data, dict) and "questions" in data:
            qs = data.get("questions") or []
            cleaned = []
            for item in qs:
                if isinstance(item, dict):
                    q = str(item.get("question", "")).strip()
                    if q and q not in target:
                        cleaned.append(item)
                elif isinstance(item, str):
                    if item.strip() not in target:
                        cleaned.append(item)
                else:
                    cleaned.append(item)
            payload = {"questions": cleaned}
        else:
            return

        try:
            with open(UNANSWERED_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _refresh_unanswered_count(self) -> None:
        """Update the "(N new)" label after a per-row delete."""
        lbl = getattr(self, "_unanswered_count_label", None)
        if lbl is None:
            return
        try:
            remaining = self._load_unanswered()
            lbl.configure(text=f"  ({len(remaining)} new)")
        except Exception:
            pass

    def _add_question_row(
        self,
        parent: tk.Widget,
        question: str,
        input_type: str,
        options: list[str] | None,
        saved: dict[str, str],
        deletable: bool = False,
    ) -> None:
        """Add a single question + input row.

        ``deletable=True`` adds a ✕ button so the user can remove a
        question from data/unanswered.json. The base wizard questions
        (``QUESTIONS``) are NOT deletable — they're seeded into the
        wizard structurally and removing them would only leave them
        re-rendered next launch. Only the "New Questions from Recent
        Applications" section uses ``deletable=True``.
        """
        row = tk.Frame(parent, bg=BG_CARD)
        row.pack(fill="x", pady=(0, 10))

        is_template = _find_placeholder(question) is not None

        q_line = tk.Frame(row, bg=BG_CARD)
        q_line.pack(anchor="w", fill="x")

        tk.Label(
            q_line, text=question, font=FONT_BODY,
            fg=TEXT, bg=BG_CARD, anchor="w",
            wraplength=650, justify="left",
        ).pack(side="left", anchor="w")

        if is_template:
            # Subtle muted hint so users can spot template rows at a
            # glance without clicking the "?" button.
            tk.Label(
                q_line, text=" (template)", font=FONT_SMALL,
                fg=TEXT_MUTED, bg=BG_CARD,
            ).pack(side="left", anchor="w")

        var = tk.StringVar(value=saved.get(question, ""))

        # Input + AI-assist button on a single line. The assist
        # button is intentionally tiny (single-character glyph) so
        # it doesn't compete visually with the answer field.
        input_row = tk.Frame(row, bg=BG_CARD)
        input_row.pack(fill="x", pady=(4, 0))

        if input_type == "dropdown" and options:
            combo = ttk.Combobox(
                input_row, textvariable=var, values=options,
                font=FONT_BODY, width=30, state="readonly",
            )
            combo.pack(side="left", anchor="w")
        else:
            entry = ttk.Entry(
                input_row, textvariable=var,
                font=FONT_BODY,
            )
            entry.pack(side="left", fill="x", expand=True)

        ttk.Button(
            input_row, text="?", width=3,
            command=lambda q=question, v=var: self._open_answer_assist(q, v),
        ).pack(side="left", padx=(8, 0))

        if deletable:
            # Small ✕ to remove the row + its entry from
            # data/unanswered.json. Confirmation lives in the handler
            # so a fat-finger click on a row near "?" doesn't lose
            # data silently.
            ttk.Button(
                input_row, text="✕", width=3,
                command=lambda q=question, r=row:
                    self._delete_unanswered(q, r),
            ).pack(side="left", padx=(4, 0))

        # Store the variable on the wizard for later retrieval
        self.wizard.answer_vars[question] = var

    # ------------------------------------------------------------------
    # Feature B — AI-assisted answer validation + suggestion
    # ------------------------------------------------------------------

    def _open_answer_assist(
        self, question: str, answer_var: tk.StringVar,
    ) -> None:
        """Open the multi-turn chat assistant for a single row.

        Replaces the old single-shot AnswerAssistDialog (which fired two
        LLM calls and showed canned results) with a back-and-forth chat
        so the LLM can ASK the user what an ambiguous question means
        instead of guessing — see live feedback 2026-05-01: AI assist
        was useless for the placeholder/tool question because the LLM
        had no way to clarify.
        """
        ChatAssistDialog(self, question, answer_var, self.wizard)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_saved_answers() -> dict[str, str]:
        """Load previously saved answers from answers.json.

        Tolerates all three historical shapes: flat dict, list of
        entries, and {"questions": [...]} wrapped. Returns a flat
        {question: answer} dict in every case so the rest of the
        answers step can stay simple.
        """
        if not ANSWERS_FILE.exists():
            return {}
        try:
            with open(ANSWERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

        out: dict[str, str] = {}
        if isinstance(data, dict) and "questions" not in data:
            # Flat dict — already canonical
            return {str(k): str(v) for k, v in data.items() if isinstance(k, str)}
        if isinstance(data, dict) and "questions" in data:
            data = data.get("questions", [])
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    q = str(entry.get("question", "")).strip()
                    a = str(entry.get("answer", "")).strip()
                    if q:
                        out[q] = a
        return out

    @staticmethod
    def _load_unanswered() -> list[str]:
        """Load unanswered questions from previous runs.

        Returns a list of question STRINGS. Tolerant of three
        historical shapes on disk:

        1. List of plain strings (original format)::
           ["Zip code", "Street address"]

        2. List of entry dicts (what form_filler._record_unanswered
           writes today)::
           [{"question": "Zip code", "encountered": 3}, ...]

        3. Flat dict of {question: count} (legacy wizard saves)::
           {"Zip code": 3, "Street address": 1}
        """
        if not UNANSWERED_FILE.exists():
            return []
        try:
            with open(UNANSWERED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        if isinstance(data, list):
            out: list[str] = []
            for entry in data:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    q = entry.get("question", "")
                    if isinstance(q, str) and q:
                        out.append(q)
            return out
        if isinstance(data, dict):
            return [k for k in data.keys() if isinstance(k, str)]
        return []


class ChatAssistDialog(tk.Toplevel):
    """Modal multi-turn chat assistant for building a single answer.

    Replaces the older single-shot AnswerAssistDialog. The old dialog
    fired two LLM calls (validate + suggest) and showed canned results
    — useful when the question was unambiguous, useless when it wasn't
    (live feedback 2026-05-01: "AI suggestion doesn't do anything for
    [specific tool questions]" — the LLM had no way to ASK what tool
    the user meant, so it either guessed wrong or punted).

    This dialog is a back-and-forth chat:

    - Open seeds the LLM with profile + concatenated resume text +
      the question + the current saved answer + any detected
      placeholder.
    - User types a message, hits Send (or Enter; Shift+Enter for
      newline). Each turn fires a fresh ``LLMRouter.complete()`` call
      with the full conversation history threaded back into the
      prompt.
    - The LLM is instructed to end every reply with a
      ``SUGGESTED: <answer>`` line; we extract that into a live
      "Suggested answer" preview that the user can save with
      "Use this answer".
    - Conversation is capped at ~12 turns to keep prompt size bounded
      on local models (Gemma 4 starts degrading past ~8k chars).
    """

    # Maximum number of user+assistant turn-pairs before the dialog
    # locks input. Beyond this, the prompt window grows large enough
    # to push local models past their reliable instruction-following
    # range. 12 is generous — most useful exchanges resolve in 2-4.
    MAX_TURNS = 12
    # When the user reaches this turn count, surface a soft warning
    # so they know the dialog is about to cap. 10 leaves a 2-turn
    # buffer for them to wrap up.
    WARN_TURNS = 10

    def __init__(
        self,
        parent: tk.Misc,
        question: str,
        answer_var: tk.StringVar,
        wizard: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._question = question
        self._answer_var = answer_var
        self._wizard = wizard
        # Conversation history — list of dicts ``{role, text}`` where
        # role is ``"user"`` or ``"assist"``. Both seed (system framing)
        # and live turns end up here so the prompt re-render is just
        # `\n`.join over this list. The leading entry is a synthetic
        # "assist" message that opens the chat — see ``_seed_chat``.
        self._history: list[dict[str, str]] = []
        # Most recent SUGGESTED line extracted from an assistant
        # reply. Mirrored into the preview label and used by the
        # "Use this answer" button.
        self._current_suggestion: str = ""
        # In-flight worker guard. We allow the user to close the
        # dialog while a worker is still running — the close handler
        # just flips this and any late after-callbacks no-op.
        self._closed: bool = False
        self._busy: bool = False
        # Cached resume + profile blobs. Resolved lazily on first
        # send so dialog open stays snappy.
        self._resume_text: str | None = None
        self._candidate_profile: str | None = None
        self._placeholder = _find_placeholder(question)

        self.title("AI assist — chat about this answer")
        self.configure(bg=BG)
        self.geometry("720x680")
        self.minsize(560, 520)

        self._build_ui()

        # Modal-grab — match JobReviewPanel / AlmostPanel pattern.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _e: self._on_close())
        # Focus the input so the user can start typing immediately.
        self.after_idle(self._input.focus_set)

        # Seed the chat with an opening assistant message + first
        # auto-fired LLM turn. This gives the user something to read
        # on open instead of a blank pane.
        self.after(50, self._seed_chat)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ---- Header ----
        ttk.Label(
            self, text="AI assist — chat",
            style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        q_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        q_card.pack(fill="x", padx=PAD_X, pady=(0, 8))
        tk.Label(
            q_card, text="Question:",
            font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
        ).pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(
            q_card, text=self._question,
            font=FONT_BODY, fg=TEXT, bg=BG_CARD,
            wraplength=640, justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # Current saved answer + quick-actions row
        cur_row = tk.Frame(q_card, bg=BG_CARD)
        cur_row.pack(fill="x", padx=12, pady=(0, 8))
        cur_lbl_text = self._answer_var.get().strip() or "(no answer saved yet)"
        tk.Label(
            cur_row,
            text=f"Current: {cur_lbl_text[:200]}",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            wraplength=460, justify="left",
        ).pack(side="left", anchor="w")
        ttk.Button(
            cur_row, text="Use as-is",
            command=self._on_close,
        ).pack(side="right", padx=(6, 0))

        # ---- Chat transcript ----
        transcript_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        transcript_card.pack(
            fill="both", expand=True, padx=PAD_X, pady=(0, 8),
        )
        # Read-only Text widget. We toggle state -> normal on each
        # insert, then back to disabled, so the user can't edit
        # transcript history but can still scroll + select to copy.
        transcript_frame = tk.Frame(transcript_card, bg=BG_CARD)
        transcript_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._transcript = tk.Text(
            transcript_frame, wrap="word", state="disabled",
            bg=BG_CARD, fg=TEXT, font=FONT_BODY,
            relief="flat", borderwidth=0, padx=6, pady=6,
            height=12,
        )
        scroll = ttk.Scrollbar(
            transcript_frame, orient="vertical",
            command=self._transcript.yview,
        )
        self._transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._transcript.pack(side="left", fill="both", expand=True)
        # Tag styles for author labels + bubble bg. We keep this
        # minimal — Tk Text tags don't do bubble shapes, so we rely
        # on color coding for author and a leading blank line for
        # visual separation between turns.
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

        # ---- Suggested answer preview ----
        sug_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        sug_card.pack(fill="x", padx=PAD_X, pady=(0, 8))
        tk.Label(
            sug_card, text="Suggested answer",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", padx=12, pady=(8, 0))
        self._suggestion_lbl = tk.Label(
            sug_card, text="(chat hasn't proposed an answer yet)",
            font=FONT_BODY, fg=TEXT_MUTED, bg=BG_CARD,
            wraplength=640, justify="left",
        )
        self._suggestion_lbl.pack(
            anchor="w", padx=12, pady=(2, 8), fill="x",
        )
        # When the question is a TEMPLATE (placeholder present), the
        # chat is help-only — users see the bot's reasoning but
        # there's no per-tool right answer to save. Surface the
        # constraint visibly so the disabled "Use this answer" button
        # makes sense.
        if self._placeholder is not None:
            tk.Label(
                sug_card,
                text=(
                    "Template question — chat is for guidance only. "
                    "The form filler will answer the LIVE question "
                    "with your resume context at apply time."
                ),
                font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
                wraplength=640, justify="left",
            ).pack(
                anchor="w", padx=12, pady=(0, 8), fill="x",
            )

        # ---- Input + status row ----
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

        # Enter-to-send, Shift+Enter for newline. The shift handler
        # returns nothing so default newline-insert behavior wins;
        # the bare Return handler sends and returns "break" so Tk
        # doesn't ALSO insert a newline.
        self._input.bind("<Return>", self._on_return_key)
        self._input.bind("<Shift-Return>", lambda _e: None)

        # ---- Action buttons ----
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        self._use_btn = ttk.Button(
            btn_row, text="Use this answer",
            style="Primary.TButton",
            command=self._on_use_suggestion,
            state="disabled",
        )
        self._use_btn.pack(side="left")
        ttk.Button(
            btn_row, text="Close",
            command=self._on_close,
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Transcript helpers
    # ------------------------------------------------------------------

    def _append_transcript(self, role: str, text: str) -> None:
        """Append a message to the read-only transcript widget.

        ``role`` is ``"user"``, ``"assist"``, or ``"system"``. Each
        message renders as a colored author label on its own line
        followed by the body. We toggle the widget's state to
        ``normal`` only for the duration of the insert so users
        can't edit transcript content.
        """
        if self._closed:
            return
        try:
            self._transcript.configure(state="normal")
            # Leading blank between turns so the transcript reads
            # like a chat log instead of a wall of text.
            if self._transcript.index("end-1c") != "1.0":
                self._transcript.insert("end", "\n")
            label_tag = f"{role}_label"
            body_tag = "system_body" if role == "system" else "msg_body"
            label_text = {
                "user": "[user]", "assist": "[assist]",
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
    # Seeding — first turn primes the chat
    # ------------------------------------------------------------------

    def _seed_chat(self) -> None:
        """Render the opening assistant message + fire the first LLM turn.

        We don't run an LLM call for the OPENING message — it's a
        canned greeting tailored by whether the question contains a
        placeholder. The first real LLM call is deferred until the
        user replies. This keeps the dialog feeling responsive on
        open even when Ollama is cold-loading the model.
        """
        if self._placeholder is not None:
            opener = (
                f"This question contains a placeholder "
                f"`{self._placeholder}` — what should it actually say? "
                "Tell me the real tool, topic, or company name and "
                "I'll help you draft an answer."
            )
        else:
            opener = (
                "Hi — I can help you build an answer to this question. "
                "What context should I work with? You can describe your "
                "experience, paste a JD snippet, or ask me to draft a "
                "first pass from your resume."
            )
        # Seed entry. We deliberately don't append a SUGGESTED line
        # for the opener — there's nothing to suggest yet.
        self._history.append({"role": "assist", "text": opener})
        self._append_transcript("assist", opener)

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    def _on_return_key(self, _event):
        # Bare Return sends; Shift+Return falls through to default
        # newline insert (handled by the separate binding).
        self._on_send()
        return "break"

    def _on_send(self) -> None:
        if self._closed or self._busy:
            return
        try:
            text = self._input.get("1.0", "end").strip()
        except tk.TclError:
            return
        if not text:
            # No-op — empty Send shouldn't fire an LLM call.
            return

        # Hard cap on conversation length. Beyond MAX_TURNS the prompt
        # body gets too long for local models to follow reliably and
        # the user is better served by closing + opening a fresh chat.
        user_turns = sum(1 for m in self._history if m["role"] == "user")
        if user_turns >= self.MAX_TURNS:
            self._set_status(
                "Chat limit reached — close the dialog and reopen "
                "for a fresh conversation."
            )
            return

        # Append user message + clear the input box.
        self._history.append({"role": "user", "text": text})
        self._append_transcript("user", text)
        try:
            self._input.delete("1.0", "end")
        except tk.TclError:
            pass

        # Soft warning at WARN_TURNS so the user knows the cap is
        # close. We render it as a system message so it lives in the
        # transcript log.
        new_user_turns = user_turns + 1
        if new_user_turns == self.WARN_TURNS:
            warn = (
                "Chat is getting long — consider closing and using "
                "the current suggestion."
            )
            self._history.append({"role": "system", "text": warn})
            self._append_transcript("system", warn)

        self._busy = True
        self._set_input_enabled(False)
        self._set_status("Thinking...")

        threading.Thread(
            target=self._worker,
            args=(list(self._history),),
            daemon=True,
        ).start()

    def _worker(self, history_snapshot: list[dict[str, str]]) -> None:
        """Background-thread entrypoint for one chat turn.

        Builds the prompt from the captured history snapshot (so a
        late append from a separate Send can't race), runs the LLM
        call via ``asyncio.run``, and marshals the reply back via
        ``self.after``.
        """
        from auto_applier.llm.prompts import ANSWER_CHAT
        from auto_applier.llm.router import LLMRouter

        # Lazy-resolve resume + profile on first send. Doing it here
        # (off the UI thread) keeps dialog open snappy when the user
        # has many resumes loaded.
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
                    prompt=ANSWER_CHAT.format(
                        candidate_profile=self._candidate_profile or "(no profile)",
                        resume_text=(self._resume_text or "(no resumes)")[:8000],
                        question=self._question,
                        current_answer=self._answer_var.get() or "(empty)",
                        conversation=convo,
                    ),
                    system_prompt=ANSWER_CHAT.system,
                    temperature=0.4,
                    max_tokens=600,
                    # Don't cache — each turn has a different prompt
                    # body anyway, but more importantly two consecutive
                    # asks with the same convo would otherwise collide.
                    use_cache=False,
                )
                return response.text or ""
            except Exception as exc:
                return f"(error reaching the AI: {exc})"

        try:
            reply = asyncio.run(run())
        except Exception as exc:
            reply = f"(error reaching the AI: {exc})"

        # Marshal back to the UI thread. If the dialog has been closed
        # in the meantime, _render_reply no-ops.
        self.after(0, lambda: self._render_reply(reply))

    def _render_reply(self, reply_text: str) -> None:
        if self._closed:
            return
        try:
            suggested = _extract_suggested(reply_text)
            # Strip the SUGGESTED line out of the body before
            # rendering — it's already shown in the preview pane,
            # so leaving it in the transcript would just be noise.
            body = SUGGESTED_LINE_RE.sub("", reply_text).strip()
            if not body:
                # Pure SUGGESTED-only reply — fall back to showing the
                # suggestion in the transcript so the user sees something.
                body = suggested or "(empty reply)"

            self._history.append({"role": "assist", "text": reply_text})
            self._append_transcript("assist", body)

            if suggested:
                self._current_suggestion = suggested
                self._suggestion_lbl.configure(
                    text=suggested, fg=TEXT,
                )
                # Don't enable "Use this answer" for placeholder
                # template questions. The chat result for "Do you
                # have experience with [specific tool]?" is
                # context-poor: at apply time the live question
                # has a real tool name, and the form_filler will
                # match against the resume in context. Saving a
                # literal placeholder-derived answer would risk
                # mis-applying it to every tools-experience
                # variant. Chat stays useful as user-guidance, but
                # the literal save is suppressed.
                if not self._placeholder:
                    self._use_btn.configure(state="normal")
            # No SUGGESTED parsed — leave previous suggestion (if any)
            # in place. The user can still hit "Use this answer" with
            # whatever the last good suggestion was.
        except tk.TclError:
            return
        finally:
            self._busy = False
            self._set_input_enabled(True)
            self._set_status("")
            try:
                self._input.focus_set()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Prompt-context collection
    # ------------------------------------------------------------------

    @staticmethod
    def _format_conversation(history: list[dict[str, str]]) -> str:
        """Render history into the `[user]: ... / [assist]: ...` form
        the ANSWER_CHAT prompt expects. System messages (the warn
        notice) are filtered out so they don't confuse the model.
        """
        lines: list[str] = []
        for entry in history:
            role = entry.get("role", "")
            text = entry.get("text", "").strip()
            if not text:
                continue
            if role == "user":
                lines.append(f"[user]: {text}")
            elif role == "assist":
                lines.append(f"[assist]: {text}")
            # "system" entries (chat-getting-long notice) are local
            # UI hints, not part of the conversation we want the LLM
            # to see.
        return "\n".join(lines) if lines else "(no prior turns)"

    def _collect_resume_text(self) -> str:
        """Concatenate every loaded resume's text, capped at 8000 chars.

        Same shape as the old AnswerAssistDialog used so the LLM can
        scan all resume text and ground its answers in real
        experience.
        """
        try:
            from auto_applier.llm.router import LLMRouter
            from auto_applier.resume.manager import ResumeManager
            rm = ResumeManager(LLMRouter())
            resumes = rm.list_resumes()
            chunks: list[str] = []
            for r in resumes:
                text = rm.get_resume_text(r.label)
                if text:
                    chunks.append(f"=== {r.label} ===\n{text}")
            if not chunks:
                return "(no resumes loaded)"
            blob = "\n\n".join(chunks)
            return blob[:8000]
        except Exception:
            return "(no resumes loaded)"

    def _collect_profile(self) -> str:
        """Build a short profile blob from the wizard's personal_info.

        Email and phone are redacted — those are PII the LLM does
        not need to do its job. The rest (name, location, work
        preferences) gives the model enough context to ground
        answers without leaking contact details into the prompt.
        """
        try:
            data = getattr(self._wizard, "data", None) or {}
            keep_keys = (
                "first_name", "last_name", "city", "state", "country",
                "linkedin_url", "website",
            )
            parts: list[str] = []
            for key in keep_keys:
                var = data.get(key)
                if var is None:
                    continue
                try:
                    val = var.get() if hasattr(var, "get") else str(var)
                except Exception:
                    val = ""
                val = str(val).strip()
                if val:
                    parts.append(f"{key}: {val}")
            if not parts:
                return "(no profile data)"
            return "\n".join(parts)
        except Exception:
            return "(no profile data)"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_use_suggestion(self) -> None:
        if not self._current_suggestion:
            return
        self._answer_var.set(self._current_suggestion)
        self._on_close()

    def _on_close(self) -> None:
        """Tear down the dialog. Any in-flight LLM worker will land
        on a closed `_render_reply` and short-circuit on the
        ``self._closed`` guard — we don't bother trying to cancel
        the underlying ``asyncio.run`` (best-effort, per spec).
        """
        self._closed = True
        try:
            self.destroy()
        except tk.TclError:
            pass


# Backwards-compatible alias. External callers (and tests) can still
# reach the old name; new code should use ChatAssistDialog directly.
AnswerAssistDialog = ChatAssistDialog
