"""The helper scripts must point at paths and modules that actually exist.

Why this file exists: `scripts/run_smoke.py` and `scripts/refresh_fixtures.py` both silently
rotted at the v3→master package rename (2026-05-30, `av3/` → `auto_applier/`, `tests_v3/` →
`tests/`). `run_smoke.py` invoked `pytest tests_v3/test_live_smoke.py` and `refresh_fixtures.py`
wrote to `tests_v3/fixtures/` while importing `from av3.config import ...` — so **neither could
have worked for two months**, and nothing noticed, because scripts aren't imported by the suite.

That is the worst shape for this particular bug: the smoke runner IS the selector-drift alarm.
A broken alarm looks exactly like "no drift".

These tests are cheap, have no false-alarm mode, and fail loudly the next time a rename outruns
the scripts.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _python_scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")


def test_scripts_dir_exists():
    assert SCRIPTS.is_dir(), f"missing {SCRIPTS}"


@pytest.mark.parametrize("script", _python_scripts(), ids=lambda p: p.name)
def test_script_parses(script: Path):
    """A syntax error in a helper would only ever surface when someone ran it."""
    ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


@pytest.mark.parametrize("script", _python_scripts(), ids=lambda p: p.name)
def test_script_does_not_reference_the_pre_rename_package(script: Path):
    """`av3` is the CLI VERB; the import namespace is `auto_applier` (renamed 2026-05-30)."""
    text = script.read_text(encoding="utf-8")
    bad = re.findall(r"^\s*(?:from|import)\s+av3\b.*$", text, flags=re.MULTILINE)
    assert not bad, (
        f"{script.name} imports the pre-rename package: {bad} -> use `auto_applier` "
        f"(the CLI verb stays `av3`, only the import namespace changed)"
    )


@pytest.mark.parametrize("script", _python_scripts(), ids=lambda p: p.name)
def test_script_does_not_reference_the_pre_rename_test_dir(script: Path):
    """Tests live in `tests/`. A stale `tests_v3/` path makes a runner a silent no-op."""
    text = script.read_text(encoding="utf-8")
    assert "tests_v3" not in text, (
        f"{script.name} still references tests_v3/ -> tests live in tests/ since the "
        f"v3→master rename; a stale path here means the script cannot work"
    )


def test_run_smoke_targets_a_real_suite():
    """The smoke runner's target must exist — it's the selector-drift alarm, and a broken
    alarm is indistinguishable from 'no drift'."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("run_smoke", SCRIPTS / "run_smoke.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.SMOKE_TESTS.exists(), (
        f"run_smoke.py points at {module.SMOKE_TESTS}, which does not exist"
    )
    assert module.SMOKE_TESTS.name == "test_live_smoke.py"


def test_refresh_fixtures_targets_the_real_fixtures_dir():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("refresh_fixtures", SCRIPTS / "refresh_fixtures.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.FIXTURES_DIR.is_dir(), (
        f"refresh_fixtures.py writes to {module.FIXTURES_DIR}, which does not exist"
    )


def test_powershell_helpers_reference_real_targets():
    """Each register-*.ps1 must invoke something that exists."""
    checks = {
        "register-smoke-task.ps1": "scripts\\run_smoke.py",
        "register-discovery-task.ps1": "auto_applier.cli.main",
    }
    for name, needle in checks.items():
        path = SCRIPTS / name
        assert path.exists(), f"missing {path}"
        assert needle in path.read_text(encoding="utf-8"), (
            f"{name} no longer references {needle!r}"
        )
    # The smoke helper's runner target must be a real file.
    assert (SCRIPTS / "run_smoke.py").exists()
