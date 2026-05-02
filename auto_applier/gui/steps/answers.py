"""Step 7: Pre-configured answers for common application questions."""
import asyncio
import json
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.gui.styles import (
    ACCENT_TEXT, BG, BG_CARD, DANGER_TEXT, PRIMARY, WARNING, TEXT, TEXT_LIGHT,
    TEXT_MUTED, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL, FONT_MONO,
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
        """Open the validation + suggestion popup for a single row."""
        AnswerAssistDialog(self, question, answer_var)

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


class AnswerAssistDialog(tk.Toplevel):
    """Modal popup: validate an answer and propose a resume-grounded one.

    Two LLM calls fire in parallel on a single background thread (one
    asyncio.run() drives both via ``asyncio.gather``). Each section
    shows a "Checking..." placeholder until its result lands, then
    flips to the rendered output. The user can replace their answer
    with the suggestion via "Use suggested answer".
    """

    def __init__(
        self,
        parent: tk.Misc,
        question: str,
        answer_var: tk.StringVar,
    ) -> None:
        super().__init__(parent)
        self._question = question
        self._answer_var = answer_var
        self._suggested_answer: str = ""
        # Detect un-customized template placeholders (e.g. "[specific
        # tool]", "<topic>", "___"). When set, we skip the LLM calls
        # entirely — otherwise the model fabricates fills for the
        # placeholder by stitching together unrelated tokens from its
        # context (live run 2026-05-01: "Testpilot" hallucination).
        self._placeholder = _find_placeholder(question)

        self.title("AI assist — answer review")
        self.configure(bg=BG)
        self.geometry("620x540")
        self.minsize(460, 420)

        self._build_ui()

        # Modal-grab — match JobReviewPanel / AlmostPanel pattern.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        # First focusable control on open is the close/use button —
        # but the user usually wants to read first, so focus the
        # window itself so Escape works without a tab.
        self.after_idle(self.focus_set)

        if self._placeholder is None:
            # Kick off both LLM calls immediately on open.
            self.after(50, self._start_workers)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Header — full question text
        ttk.Label(
            self, text="Answer review",
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
            wraplength=540, justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # Validation card
        val_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        val_card.pack(fill="x", padx=PAD_X, pady=(0, 8))
        tk.Label(
            val_card, text="Validation",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", padx=12, pady=(8, 4))
        if self._placeholder is not None:
            self._validation_lbl = tk.Label(
                val_card,
                text=(
                    "Cannot validate — this question contains a "
                    "placeholder. Customize it first."
                ),
                font=FONT_BODY, fg=WARNING, bg=BG_CARD,
                wraplength=540, justify="left",
            )
        else:
            self._validation_lbl = tk.Label(
                val_card, text="Checking...",
                font=FONT_BODY, fg=TEXT_MUTED, bg=BG_CARD,
                wraplength=540, justify="left",
            )
        self._validation_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        # Suggestion card
        sug_card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1,
        )
        sug_card.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 8))
        tk.Label(
            sug_card, text="Suggested answer",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
        ).pack(anchor="w", padx=12, pady=(8, 4))
        if self._placeholder is not None:
            # Bypass LLM entirely. Render a fixed warning that names
            # the exact placeholder substring so the user knows what
            # to replace.
            self._suggestion_lbl = tk.Label(
                sug_card,
                text=(
                    "This question is a TEMPLATE. Replace the "
                    "placeholder below with the real tool/topic/"
                    "company name from the actual job application "
                    "before saving an answer here."
                ),
                font=FONT_BODY, fg=TEXT, bg=BG_CARD,
                wraplength=540, justify="left",
            )
            self._suggestion_lbl.pack(anchor="w", padx=12, pady=(0, 6))
            tk.Label(
                sug_card, text=self._placeholder,
                font=FONT_MONO, fg=DANGER_TEXT, bg=BG_CARD,
                wraplength=540, justify="left",
            ).pack(anchor="w", padx=12, pady=(0, 4))
            self._rationale_lbl = tk.Label(
                sug_card, text="",
                font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
                wraplength=540, justify="left",
            )
            self._rationale_lbl.pack(anchor="w", padx=12, pady=(0, 8))
        else:
            self._suggestion_lbl = tk.Label(
                sug_card, text="Checking...",
                font=FONT_BODY, fg=TEXT, bg=BG_CARD,
                wraplength=540, justify="left",
            )
            self._suggestion_lbl.pack(anchor="w", padx=12, pady=(0, 4))
            self._rationale_lbl = tk.Label(
                sug_card, text="",
                font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
                wraplength=540, justify="left",
            )
            self._rationale_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        # Action buttons. For placeholder-bearing questions we omit
        # the "Use suggested answer" button — there is no suggestion
        # to use, only Close.
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        if self._placeholder is None:
            self._use_btn = ttk.Button(
                btn_row, text="Use suggested answer",
                style="Primary.TButton",
                command=self._on_use_suggestion,
                state="disabled",
            )
            self._use_btn.pack(side="left")
        else:
            self._use_btn = None
        ttk.Button(
            btn_row, text="Close",
            command=self.destroy,
        ).pack(side="right")

        # Enter on the dialog accepts the suggestion (when ready).
        # For placeholder dialogs Enter is a no-op (closes via Escape).
        if self._use_btn is not None:
            self.bind(
                "<Return>",
                lambda _e: self._on_use_suggestion()
                if str(self._use_btn["state"]) == "normal" else None,
            )

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _start_workers(self) -> None:
        current_answer = self._answer_var.get()
        # Capture resume context once on the UI thread — ResumeManager
        # only needs the router for new adds, but we still construct
        # it the same way panels/almost.py does for consistency.
        threading.Thread(
            target=self._worker,
            args=(self._question, current_answer),
            daemon=True,
        ).start()

    def _worker(self, question: str, current_answer: str) -> None:
        # Lazy imports — keeps wizard startup snappy.
        from auto_applier.llm.prompts import (
            ANSWER_SUGGESTION,
            ANSWER_VALIDATION,
        )
        from auto_applier.llm.router import LLMRouter
        from auto_applier.resume.manager import ResumeManager

        async def run() -> tuple[dict | None, dict | None]:
            router = LLMRouter()
            await router.initialize()

            # Concatenate every loaded resume so the suggestion call
            # has the full picture. The user's pain point is "do you
            # have experience with X?" — we want the LLM to scan all
            # resume text and answer truthfully.
            try:
                rm = ResumeManager(router)
                resumes = rm.list_resumes()
                resume_chunks: list[str] = []
                for r in resumes:
                    text = rm.get_resume_text(r.label)
                    if text:
                        resume_chunks.append(f"=== {r.label} ===\n{text}")
                resume_text = (
                    "\n\n".join(resume_chunks) if resume_chunks
                    else "(no resumes loaded)"
                )
            except Exception:
                resume_text = "(no resumes loaded)"

            async def _validate() -> dict | None:
                try:
                    return await router.complete_json(
                        prompt=ANSWER_VALIDATION.format(
                            question=question,
                            answer=current_answer or "(empty)",
                        ),
                        system_prompt=ANSWER_VALIDATION.system,
                    )
                except Exception:
                    return None

            async def _suggest() -> dict | None:
                # Truncate resume text — local models choke past ~8k
                # chars in the prompt body, and we already saw the
                # equivalent cap in analysis/title_expansion.py.
                excerpt = resume_text[:8000]
                try:
                    return await router.complete_json(
                        prompt=ANSWER_SUGGESTION.format(
                            question=question,
                            resume_text=excerpt,
                        ),
                        system_prompt=ANSWER_SUGGESTION.system,
                    )
                except Exception:
                    return None

            return await asyncio.gather(_validate(), _suggest())

        try:
            validation, suggestion = asyncio.run(run())
        except Exception:
            validation, suggestion = None, None

        self.after(0, lambda: self._render_results(validation, suggestion))

    # ------------------------------------------------------------------
    # UI updates
    # ------------------------------------------------------------------

    def _render_results(
        self,
        validation: dict | None,
        suggestion: dict | None,
    ) -> None:
        try:
            # Validation
            if validation is None:
                self._validation_lbl.configure(
                    text="Couldn't reach the AI — is Ollama running?",
                    fg=DANGER_TEXT,
                )
            else:
                valid = bool(validation.get("valid"))
                issue = str(validation.get("issue") or "").strip()
                if valid:
                    self._validation_lbl.configure(
                        text="✓ Looks good — your saved answer "
                             "fits the question.",
                        fg=ACCENT_TEXT,
                    )
                else:
                    msg = "✗ " + (issue or "This answer may not fit.")
                    self._validation_lbl.configure(
                        text=msg, fg=DANGER_TEXT,
                    )

            # Suggestion
            if suggestion is None:
                self._suggestion_lbl.configure(
                    text="Couldn't generate a suggestion.",
                    fg=DANGER_TEXT,
                )
                self._rationale_lbl.configure(text="")
                return

            answer = str(suggestion.get("answer") or "").strip()
            rationale = str(suggestion.get("rationale") or "").strip()

            if not answer:
                self._suggestion_lbl.configure(
                    text="(no resume-backed suggestion available)",
                    fg=TEXT_MUTED,
                )
                if rationale:
                    self._rationale_lbl.configure(text=rationale)
                return

            self._suggested_answer = answer
            self._suggestion_lbl.configure(text=answer, fg=TEXT)
            if rationale:
                self._rationale_lbl.configure(
                    text=f"Why: {rationale}",
                )
            if self._use_btn is not None:
                self._use_btn.configure(state="normal")
        except tk.TclError:
            # Window already closed — nothing to render into.
            return

    def _on_use_suggestion(self) -> None:
        if not self._suggested_answer:
            return
        self._answer_var.set(self._suggested_answer)
        self.destroy()
