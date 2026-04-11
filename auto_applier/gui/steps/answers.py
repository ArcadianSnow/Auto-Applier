"""Step 7: Pre-configured answers for common application questions."""
import json
import tkinter as tk
from tkinter import ttk

from auto_applier.config import ANSWERS_FILE, UNANSWERED_FILE
from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, WARNING, TEXT, TEXT_LIGHT, TEXT_MUTED, BORDER,
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

        if input_type == "dropdown" and options:
            combo = ttk.Combobox(
                row, textvariable=var, values=options,
                font=FONT_BODY, width=30, state="readonly",
            )
            combo.pack(anchor="w", pady=(4, 0))
        else:
            entry = ttk.Entry(
                row, textvariable=var,
                font=FONT_BODY, width=50,
            )
            entry.pack(fill="x", pady=(4, 0))

        # Store the variable on the wizard for later retrieval
        self.wizard.answer_vars[question] = var

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
        """Load unanswered questions from previous runs."""
        if not UNANSWERED_FILE.exists():
            return []
        try:
            with open(UNANSWERED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return list(data.keys())
            return []
        except (json.JSONDecodeError, OSError):
            return []
