"""Tests for the multi-turn ChatAssistDialog.

The old AnswerAssistDialog fired one validate + one suggest LLM call
on open and closed. Live feedback 2026-05-01 was that this couldn't
help with ambiguous "[specific tool]" questions because the LLM had
no way to ASK what tool the user meant. ChatAssistDialog turns that
single-shot popup into a back-and-forth chat.

These tests cover:

1. SUGGESTED line extraction — the module-level helper that pulls
   the LLM's proposed answer out of a free-form reply.
2. Fallback to whole text when the SUGGESTED marker is missing.
3. Headless dialog construction — same Tk root pattern the
   dashboard tests use, ensuring the dialog can at least be wired
   up without immediate exceptions.
"""
from __future__ import annotations

import os
import tkinter as tk

import pytest

from auto_applier.gui.steps.answers import (
    ChatAssistDialog,
    _extract_suggested,
)


# ---------------------------------------------------------------------------
# _extract_suggested — pure helper, no Tk needed
# ---------------------------------------------------------------------------


class TestExtractSuggested:
    """Parse the trailing `SUGGESTED: <answer>` line out of a reply."""

    def test_basic_extract(self):
        # Canonical case: a one-line preamble plus the SUGGESTED line.
        text = "Some text\nSUGGESTED: my answer"
        assert _extract_suggested(text) == "my answer"

    def test_multiline_body_with_suggested_at_end(self):
        # Realistic shape: 1-3 sentences of reasoning + the marker.
        text = (
            "Based on your resume you've used Tableau for 3 years "
            "across two roles.\n"
            "I'd answer 'Yes' here.\n"
            "SUGGESTED: Yes"
        )
        assert _extract_suggested(text) == "Yes"

    def test_suggested_grabs_to_end_of_line(self):
        # The capture must stop at the line break, not run into the
        # next line. This is what "multi-line SUGGESTED grabs to
        # end-of-line" means.
        text = "Reasoning here.\nSUGGESTED: 5 years\nExtra trailing text"
        assert _extract_suggested(text) == "5 years"

    def test_missing_suggested_falls_back_to_full_text(self):
        # Belt-and-suspenders: if the model ignores the instruction,
        # the preview pane should at least show the whole reply so
        # the user isn't staring at an empty bubble.
        text = "I'm not sure what tool you mean — can you clarify?"
        assert _extract_suggested(text) == text

    def test_empty_string_returns_empty(self):
        assert _extract_suggested("") == ""

    def test_case_insensitive_marker(self):
        # Gemma 4 occasionally renders the marker as "Suggested:" or
        # "suggested:". The helper should still extract it.
        assert _extract_suggested("blah\nsuggested: foo") == "foo"
        assert _extract_suggested("blah\nSuggested: bar") == "bar"

    def test_last_suggested_wins(self):
        # If the model emits an interim SUGGESTED mid-reply followed
        # by a final one, we want the final one — that's the model's
        # last answer.
        text = (
            "First pass:\nSUGGESTED: maybe\n"
            "On reflection:\nSUGGESTED: definitely yes"
        )
        assert _extract_suggested(text) == "definitely yes"

    def test_empty_suggested_after_colon(self):
        # The prompt says "empty SUGGESTED if you don't have enough
        # info yet". An empty value should round-trip as empty.
        assert _extract_suggested("Need more info.\nSUGGESTED:") == ""


# ---------------------------------------------------------------------------
# Headless construction — make sure the dialog wires up without errors
# ---------------------------------------------------------------------------


def _have_display() -> bool:
    """Best-effort check that a Tk root can be created here.

    On Windows CI without an interactive desktop, ``tk.Tk()`` raises
    ``TclError`` immediately. We skip the construction test in that
    case rather than failing the whole suite.
    """
    if os.environ.get("AUTO_APPLIER_SKIP_TK_TESTS"):
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    try:
        root.withdraw()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return True


@pytest.mark.skipif(
    not _have_display(),
    reason="No Tk display available in this environment",
)
def test_chat_dialog_constructs_without_errors():
    """The dialog must come up cleanly given a parent + StringVar.

    We don't drive the chat — that would require mocking the LLM
    router and pumping the Tk event loop. We just want to know the
    constructor wires up the transcript widget, the input box, and
    the modal grab without raising.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        answer_var = tk.StringVar(master=root, value="")
        dlg = ChatAssistDialog(
            parent=root,
            question="Do you have experience with [specific tool]?",
            answer_var=answer_var,
            wizard=None,
        )
        # Force any deferred ``after`` callbacks to drain so a
        # construction-time defer that would explode shows up here.
        root.update_idletasks()
        # Sanity: the transcript widget exists and starts disabled,
        # the input is the focusable entry, and the use-button starts
        # disabled (no suggestion parsed yet).
        assert str(dlg._transcript["state"]) == "disabled"
        assert str(dlg._use_btn["state"]) == "disabled"
        assert dlg._current_suggestion == ""
        # Closing should flip the closed flag and not raise.
        dlg._on_close()
        assert dlg._closed is True
    finally:
        try:
            root.destroy()
        except Exception:
            pass
