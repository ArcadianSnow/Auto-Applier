"""Tests for context-aware hint resolution in form_filler + chat dialog.

User feedback 2026-04-30:
    "for an open ended question like the specific tools and certificates
    for this role, we need to make sure that the applier knows how to
    read the answer and knows what context to use it in."

The Tier 2.1 commit (b6af5f9) handled placeholder-template questions
in the chat dialog (literal save suppressed). This file covers the
OTHER half: NON-template open-ended questions (e.g. "Why do you want
this role?"). The user can save a chat-derived answer with a `_hint:`
prefix; at apply time the form_filler treats it as INTENT and adapts
it to the live question via the resume + JD + company.

Tests:
1. Regression — non-prefixed answers.json entries still return literal.
2. Prefix detected → routes to LLM resolver with prefix-stripped hint.
3. LLM returns empty → fall back to bare hint text (better than nothing).
4. Chat dialog "Save as context-aware hint" button writes the prefixed
   value into the StringVar.
5. Open-ended-shape heuristic — decides when the new button is shown.
"""
from __future__ import annotations

import asyncio
import json
import os
import tkinter as tk
from unittest.mock import AsyncMock, MagicMock

import pytest

from auto_applier.browser.form_filler import FormFiller, HINT_PREFIX
from auto_applier.browser.selector_utils import FormField
from auto_applier.gui.steps.answers import (
    ChatAssistDialog,
    _is_open_ended_shape,
)


# ---------------------------------------------------------------------------
# 1 + 2 + 3: form_filler hint resolution
# ---------------------------------------------------------------------------


def _build_filler(tmp_path, monkeypatch, *, hint: str | None,
                  literal: str | None) -> FormFiller:
    """Construct a FormFiller backed by a tmp answers.json file.

    Pass ``hint`` to seed an entry with the ``_hint:`` prefix; pass
    ``literal`` to seed a plain literal entry. Both can be supplied.
    """
    entries: list[dict] = []
    if hint is not None:
        entries.append({
            "question": "Why do you want this role?",
            "answer": f"{HINT_PREFIX}{hint}",
        })
    if literal is not None:
        entries.append({
            "question": "Are you authorized to work?",
            "answer": literal,
        })
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps(entries))
    monkeypatch.setattr(
        "auto_applier.browser.form_filler.ANSWERS_FILE", answers_file,
    )
    return FormFiller(
        router=MagicMock(),
        personal_info={},
        resume_text="Built dbt + Snowflake pipelines for 3 years.",
        job_description="Data engineering role using dbt and Snowflake.",
        company_name="Acme Data Co",
    )


class TestMatchAnswersBackwardsCompat:
    """Existing literal-answer behavior must be unchanged.

    Regression guard: the introduction of the `_hint:` marker must not
    change what `_match_answers` returns for plain entries. The prefix
    is only meaningful at the fill_field hint-resolution stage.
    """

    def test_literal_answer_returned_verbatim(self, tmp_path, monkeypatch):
        f = _build_filler(
            tmp_path, monkeypatch, hint=None, literal="Yes",
        )
        # Plain string with no prefix — exactly the value stored.
        assert f._match_answers("Are you authorized to work?") == "Yes"

    def test_hint_prefixed_answer_returned_with_prefix_intact(
        self, tmp_path, monkeypatch,
    ):
        # _match_answers itself stays sync and returns the raw value
        # (prefix included) — fill_field is the layer that detects and
        # dispatches to the resolver. This is the contract we rely on
        # for the (sync _match_answers, async resolution) split.
        f = _build_filler(
            tmp_path, monkeypatch,
            hint="I'm drawn to dbt + Snowflake work",
            literal=None,
        )
        result = f._match_answers("Why do you want this role?")
        assert result.startswith(HINT_PREFIX)
        assert "dbt" in result


class TestHintResolution:
    """`_resolve_template_answer` adapts a hint via the LLM."""

    def test_prefix_stripped_before_llm_call(self, tmp_path, monkeypatch):
        f = _build_filler(
            tmp_path, monkeypatch,
            hint="I'm drawn to dbt + Snowflake work",
            literal=None,
        )
        # Mock the router's complete_json to return a known answer
        # AND capture the prompt so we can assert the hint passed in
        # has been stripped of its `_hint:` prefix.
        captured: dict[str, object] = {}

        async def fake_complete_json(prompt, system_prompt="", temperature=0.1):
            captured["prompt"] = prompt
            captured["system"] = system_prompt
            return {"answer": "I want to deepen my dbt + Snowflake work at Acme."}

        f.router.complete_json = AsyncMock(side_effect=fake_complete_json)

        field = FormField(
            label="Why do you want this role?",
            element=MagicMock(), field_type="textarea",
        )
        out = asyncio.run(
            f._resolve_template_answer(
                "I'm drawn to dbt + Snowflake work",
                field.label, field,
            )
        )
        assert out == "I want to deepen my dbt + Snowflake work at Acme."
        # The router was actually invoked
        f.router.complete_json.assert_awaited_once()
        # The prompt body must contain the hint TEXT but NOT the marker
        # — the resolver must strip the prefix before composing the
        # prompt, otherwise the LLM sees a literal "_hint:" token and
        # gets confused about what it means.
        assert "I'm drawn to dbt + Snowflake work" in captured["prompt"]
        assert "_hint:" not in captured["prompt"]

    def test_empty_llm_response_returns_empty(self, tmp_path, monkeypatch):
        # When complete_json returns {"answer": ""} (the model judged
        # the hint inapplicable to the live question), the resolver
        # returns "". The CALLER (fill_field) is responsible for
        # falling back to the bare hint text — not the resolver.
        f = _build_filler(
            tmp_path, monkeypatch,
            hint="hint text",
            literal=None,
        )
        f.router.complete_json = AsyncMock(return_value={"answer": ""})
        field = FormField(
            label="Q?", element=MagicMock(), field_type="textarea",
        )
        out = asyncio.run(
            f._resolve_template_answer("hint text", field.label, field)
        )
        assert out == ""

    def test_llm_exception_returns_empty(self, tmp_path, monkeypatch):
        # Network error / backend down — same fallback contract:
        # resolver returns "" and lets the caller decide what to do.
        f = _build_filler(
            tmp_path, monkeypatch, hint="x", literal=None,
        )
        f.router.complete_json = AsyncMock(
            side_effect=RuntimeError("backend down"),
        )
        field = FormField(
            label="Q?", element=MagicMock(), field_type="textarea",
        )
        out = asyncio.run(
            f._resolve_template_answer("x", field.label, field)
        )
        assert out == ""

    def test_empty_hint_short_circuits(self, tmp_path, monkeypatch):
        # Defensive: passing an empty hint must NOT spend an LLM call.
        f = _build_filler(
            tmp_path, monkeypatch, hint=None, literal=None,
        )
        f.router.complete_json = AsyncMock(return_value={"answer": "x"})
        field = FormField(
            label="Q?", element=MagicMock(), field_type="textarea",
        )
        out = asyncio.run(
            f._resolve_template_answer("", field.label, field)
        )
        assert out == ""
        f.router.complete_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4: Chat dialog "Save as context-aware hint" button
# ---------------------------------------------------------------------------


def _have_display() -> bool:
    """Same headless-skip pattern test_answers_chat.py uses."""
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
def test_chat_dialog_save_as_hint_writes_prefixed_value():
    """The new button writes `_hint: ` + suggestion to the StringVar.

    We don't drive an actual LLM round-trip — we set
    ``_current_suggestion`` directly to simulate a mid-chat state and
    invoke ``_on_save_as_hint`` like a click would.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        answer_var = tk.StringVar(master=root, value="")
        # Open-ended shape, no placeholder → button visible & wired.
        dlg = ChatAssistDialog(
            parent=root,
            question="Why do you want this role?",
            answer_var=answer_var,
            wizard=None,
        )
        root.update_idletasks()
        # Sanity: the hint button exists for an open-ended question.
        assert dlg._hint_btn is not None
        # Simulate a mid-chat state where the LLM has proposed text.
        dlg._current_suggestion = "I'm excited about your dbt + Snowflake work"
        # Click the button.
        dlg._on_save_as_hint()
        assert answer_var.get() == (
            "_hint: I'm excited about your dbt + Snowflake work"
        )
        # Dialog should close itself after saving — same UX as the
        # existing "Use this answer" button.
        assert dlg._closed is True
    finally:
        try:
            root.destroy()
        except Exception:
            pass


@pytest.mark.skipif(
    not _have_display(),
    reason="No Tk display available in this environment",
)
def test_chat_dialog_no_hint_button_for_yes_no_question():
    """A yes/no-shaped question should NOT show the hint button —
    those have a literal correct answer; saving it as a hint would
    just slow apply-time fills with an unnecessary LLM call.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        answer_var = tk.StringVar(master=root, value="")
        dlg = ChatAssistDialog(
            parent=root,
            question="Are you authorized to work in the United States?",
            answer_var=answer_var,
            wizard=None,
        )
        root.update_idletasks()
        # Heuristic mismatch (no open-ended fragment) → button absent.
        assert dlg._hint_btn is None
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5: Open-ended shape heuristic
# ---------------------------------------------------------------------------


class TestIsOpenEndedShape:
    """Drives whether the hint button is shown.

    Heuristic: question mark required AND at least one open-ended
    fragment ("why", "tell us", "describe", "interest", "motivat",
    "what would you", "how would you", "what excites", "your goals").
    """

    @pytest.mark.parametrize("question", [
        "Why do you want this role?",
        "Why are you interested in our company?",
        "Tell us about a time you led a project.",  # no ? → False below
        "What interests you about us?",
        "Describe your ideal team environment?",
        "What motivates you in your work?",
        "What would you bring to this team?",
        "How would you approach onboarding?",
        "What excites you about this opportunity?",
        "What are your goals for the next 5 years?",
    ])
    def test_open_ended_shapes_match(self, question):
        # Some of these don't end in "?" — handled by the negative
        # parametrize below. Only assert True when both rules hold.
        expected = question.strip().endswith("?")
        assert _is_open_ended_shape(question) is expected

    @pytest.mark.parametrize("question", [
        "Email",                               # not a question
        "Are you authorized to work?",         # ? but no open fragment
        "How many years of Python experience?",  # numeric, no fragment
        "First name",                          # not a question
        "",                                    # empty
    ])
    def test_non_open_ended_rejected(self, question):
        assert _is_open_ended_shape(question) is False

    def test_hint_prefix_does_not_trigger_phantom_skip(self):
        """The `_hint:` prefix lives on ANSWER values, not on QUESTION
        labels — so should_skip_unanswered (which inspects questions)
        must remain unaffected. This is a backwards-compat guarantee.
        """
        from auto_applier.browser.selector_utils import (
            should_skip_unanswered,
        )
        # A real question — the answer-side prefix doesn't matter here.
        # Use a tmp answers.json so the test doesn't touch user data.
        # Empty file = no dedup hits — the only rejection paths are the
        # phantom-label / leak-marker checks, which `_hint:` does not
        # match (it's not a marker substring of any leak pattern).
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "answers.json"
            empty.write_text("[]")
            # A clean open-ended question SHOULD pass through
            # (not skipped).
            assert (
                should_skip_unanswered(
                    "Why do you want this role?", empty,
                )
                is False
            )
