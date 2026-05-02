"""Regression tests for the AnswersStep delete-button defer fix.

Live run 2026-05-01 reported two coupled bugs on the Answers wizard:

1. Clicking the per-row ``✕`` button required two clicks — the first
   click registered, but the modal ``messagebox.askyesno`` failed to
   surface (Tk's grab_set on the wizard's outer modal swallowed it).
2. The first click also auto-scrolled the canvas to the bottom because
   the FocusIn handler in ``make_scrollable`` treated the messagebox
   toplevel as if it were a child of the scrollable inner frame.

Both fixes share a common shape: defer the messagebox past the click
handler via ``self.after(0, ...)``. These tests pin that shape so the
defer can't silently regress without breaking a test.
"""
from __future__ import annotations

import inspect


def test_delete_unanswered_uses_after_defer():
    """The handler must schedule the modal via ``after`` so the
    triggering click event finishes propagating before Tk yields to
    the dialog. A direct ``messagebox.askyesno`` call inline would
    re-introduce the swallowed-first-click bug.
    """
    from auto_applier.gui.steps.answers import AnswersStep

    src = inspect.getsource(AnswersStep._delete_unanswered)
    # The defer pattern: scheduling a callback past the click handler.
    assert "self.after(" in src, (
        "_delete_unanswered must use self.after(...) to defer the "
        "confirmation dialog past the originating click"
    )
    # update_idletasks ensures the click is fully processed first.
    assert "update_idletasks" in src


def test_clear_all_unanswered_uses_after_defer():
    """Same defer-past-click shape for the bulk-clear button."""
    from auto_applier.gui.steps.answers import AnswersStep

    src = inspect.getsource(AnswersStep._clear_all_unanswered)
    assert "self.after(" in src
    assert "update_idletasks" in src


def test_focus_handler_guards_against_toplevel():
    """``make_scrollable``'s FocusIn handler must filter out events
    fired against Toplevel widgets (messagebox dialogs, secondary
    windows). Without this guard, opening a confirm dialog auto-
    scrolled the underlying canvas to the bottom because the handler
    ran ``winfo_rooty()`` on the dialog's toplevel.
    """
    from auto_applier.gui import styles

    src = inspect.getsource(styles.make_scrollable)
    # The guard must reference Toplevel explicitly so we know the
    # short-circuit path is wired up.
    assert "Toplevel" in src, (
        "make_scrollable must short-circuit FocusIn events from "
        "Toplevel widgets to avoid auto-scrolling on dialog open"
    )


def test_removed_questions_not_in_common_questions():
    """The three questions removed in the 2026-05-01 cleanup must
    not reappear in COMMON_QUESTIONS:

    - ``[specific tool]`` placeholder — AI assist couldn't help and
      run-time context-fill handles it better.
    - ``How did you hear about this position?`` — form_filler's
      ``_match_contextual`` auto-answers using the platform name.
    - Professional certification question — comes from the resume,
      already available to the LLM at run time.
    """
    from auto_applier.gui.steps.answers import COMMON_QUESTIONS

    questions_lower = [q.lower() for q, _t, _o in COMMON_QUESTIONS]
    for forbidden in (
        "specific tool",
        "how did you hear about this position",
        "professional certification",
    ):
        for q in questions_lower:
            assert forbidden not in q, (
                f"Removed question matching {forbidden!r} reappeared "
                f"in COMMON_QUESTIONS: {q!r}"
            )
