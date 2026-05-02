"""Step 7: Pre-configured answers for common application questions."""
import asyncio
import json
import threading
import tkinter as tk
from tkinter import ttk

from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.gui.styles import (
    ACCENT_TEXT, BG, BG_CARD, DANGER_TEXT, PRIMARY, WARNING, TEXT, TEXT_LIGHT,
    TEXT_MUTED, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)

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
    ("Do you have experience with [specific tool]?", "entry", None),
    ("What is your current employment status?", "dropdown", [
        "Employed", "Unemployed", "Student", "Freelance", "Other",
    ]),
    ("Are you open to contract positions?", "dropdown", ["Yes", "No"]),
    ("How did you hear about this position?", "entry", None),
    ("Do you have any security clearances?", "entry", None),
    ("What languages do you speak?", "entry", None),
    ("Are you willing to undergo a background check?", "dropdown", ["Yes", "No"]),
    ("Do you have a professional certification relevant to this role?", "entry", None),
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

            header_row = tk.Frame(new_card, bg=BG_CARD)
            header_row.pack(fill="x", pady=(0, 12))

            tk.Label(
                header_row,
                text="New Questions from Recent Applications",
                font=FONT_SUBHEADING, fg=WARNING, bg=BG_CARD,
            ).pack(side="left")

            tk.Label(
                header_row,
                text=f"  ({len(unanswered)} new)",
                font=FONT_SMALL, fg=TEXT_MUTED, bg=BG_CARD,
            ).pack(side="left")

            for question in unanswered:
                self._add_question_row(
                    new_card, question, "entry", None, saved,
                )

    def _add_question_row(
        self,
        parent: tk.Widget,
        question: str,
        input_type: str,
        options: list[str] | None,
        saved: dict[str, str],
    ) -> None:
        """Add a single question + input row."""
        row = tk.Frame(parent, bg=BG_CARD)
        row.pack(fill="x", pady=(0, 10))

        tk.Label(
            row, text=question, font=FONT_BODY,
            fg=TEXT, bg=BG_CARD, anchor="w",
            wraplength=650, justify="left",
        ).pack(anchor="w")

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

        # Action buttons
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))
        self._use_btn = ttk.Button(
            btn_row, text="Use suggested answer",
            style="Primary.TButton",
            command=self._on_use_suggestion,
            state="disabled",
        )
        self._use_btn.pack(side="left")
        ttk.Button(
            btn_row, text="Close",
            command=self.destroy,
        ).pack(side="right")

        # Enter on the dialog accepts the suggestion (when ready)
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
            self._use_btn.configure(state="normal")
        except tk.TclError:
            # Window already closed — nothing to render into.
            return

    def _on_use_suggestion(self) -> None:
        if not self._suggested_answer:
            return
        self._answer_var.set(self._suggested_answer)
        self.destroy()
