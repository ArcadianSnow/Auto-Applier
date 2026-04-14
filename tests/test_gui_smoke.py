"""Smoke tests for Phase 8 GUI surfaces.

We can't reliably instantiate the full Tk hierarchy in CI (no display
on most CI runners), so these tests stick to module-level structural
checks: the wizard's step order is what we expect, the panels expose
the right names, and the helper module imports succeed.

For interactive validation, run `python run.py` and click through.
"""
from __future__ import annotations

import importlib

import pytest


def test_wizard_step_order_starts_with_welcome():
    """Wizard reorder moved AI Setup to position 2; welcome stays first."""
    from auto_applier.gui.wizard import WizardApp
    # The constructor builds the step lists at import time of the
    # module, but we only need the labels list which is a class
    # ordering decision baked into __init__. Build a stub by reading
    # the source — cheaper than instantiating a Tk root in CI.
    import inspect
    src = inspect.getsource(WizardApp.__init__)
    assert '"Welcome"' in src
    assert '"AI Setup"' in src
    # Welcome must come before AI Setup, AI Setup before everything else
    welcome_pos = src.index('"Welcome"')
    ai_pos = src.index('"AI Setup"')
    platforms_pos = src.index('"Platforms"')
    resumes_pos = src.index('"Resumes"')
    assert welcome_pos < ai_pos < platforms_pos
    assert ai_pos < resumes_pos


def test_all_phase8_panels_importable():
    """Every panel exported in __all__ should import without error."""
    from auto_applier.gui import panels
    expected = {
        "AlmostPanel",
        "JobReviewPanel",
        "OutcomeTrackerPanel",
        "RefinePanel",
        "ResumeEvolutionPanel",
        "SkillChatPanel",
        "TrendsPanel",
    }
    assert set(panels.__all__) == expected
    for name in expected:
        assert hasattr(panels, name), f"{name} missing from panels module"


@pytest.mark.parametrize("module_name", [
    "auto_applier.gui.panels.almost",
    "auto_applier.gui.panels.trends",
    "auto_applier.gui.panels.refine",
    "auto_applier.gui.panels.outcome_tracker",
])
def test_panel_module_loads(module_name):
    """Each new panel module should import cleanly."""
    mod = importlib.import_module(module_name)
    # Each module should expose at least one class ending in 'Panel'
    classes = [
        getattr(mod, name) for name in dir(mod)
        if name.endswith("Panel") and isinstance(getattr(mod, name), type)
    ]
    assert classes, f"{module_name} has no *Panel class"


def test_ready_step_has_after_run_tools():
    """Ready step should expose the after-run tool launchers."""
    from auto_applier.gui.steps.ready import ReadyStep
    # Methods we wired up
    assert hasattr(ReadyStep, "_open_almost")
    assert hasattr(ReadyStep, "_open_trends")
    assert hasattr(ReadyStep, "_open_refine")
    assert hasattr(ReadyStep, "_open_outcome_tracker")


def test_outcome_display_has_all_states():
    """Outcome tracker's display map should cover every valid outcome."""
    from auto_applier.gui.panels.outcome_tracker import _OUTCOME_DISPLAY
    from auto_applier.analysis.outcome import VALID_OUTCOMES
    for state in VALID_OUTCOMES:
        assert state in _OUTCOME_DISPLAY, f"{state} missing from display map"
