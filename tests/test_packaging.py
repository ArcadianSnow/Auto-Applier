"""Packaging config must stay coherent, and the browser path must be resolvable.

The frozen build had three independent defects, each of which silently produced an exe that
LOOKED fine and could not apply to a single job (all measured on real builds, 2026-07-31):

1. **patchright's node driver was not bundled.** patchright kept upstream's hook filenames
   (`hook-playwright.*`), so PyInstaller never fired them for our `patchright.*` imports.
   A probe exe reported `driver dir present: False` and `BrowserType.launch` raised
   `FileNotFoundError`. Upstream: wontfix.
2. **Name mismatch.** `build.py` emitted `AutoApplierV3.exe`; `installer/auto_applier.iss`
   copies `dist\\AutoApplier.exe`. The installer step could never find the build's output.
3. **`install_browser` shelled out to `sys.executable -m patchright`** — but in a frozen app
   `sys.executable` IS the app, so the first-run Chromium fetch (which the whole
   "Chromium is NOT bundled" design depends on) could not work.

None of these are visible from the Python test suite by default — they only appear in a real
build — so these cheap structural assertions stand in for that.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from auto_applier import browser_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = REPO_ROOT / "build.py"
ISS = REPO_ROOT / "installer" / "auto_applier.iss"
HOOKS = REPO_ROOT / "installer" / "pyinstaller_hooks"


# --------------------------------------------------------------- PyInstaller config

def test_patchright_hooks_exist():
    """Without these the shipped exe cannot open a browser at all."""
    for api in ("async_api", "sync_api"):
        hook = HOOKS / f"hook-patchright.{api}.py"
        assert hook.exists(), f"missing {hook}"
        assert "collect_data_files" in hook.read_text(encoding="utf-8")


def test_build_registers_the_hooks_dir():
    text = BUILD_PY.read_text(encoding="utf-8")
    assert "--additional-hooks-dir" in text, (
        "build.py must pass --additional-hooks-dir or patchright's driver is omitted"
    )
    assert "pyinstaller_hooks" in text


def test_build_exe_name_matches_the_installer_script():
    """build.py's --name and the .iss's MyAppExeName must agree, or the installer build
    fails looking for an exe that was never produced under that name."""
    build_name = re.search(r'"--name",\s*"([^"]+)"', BUILD_PY.read_text(encoding="utf-8"))
    assert build_name, "could not find --name in build.py"
    iss_name = re.search(r'#define\s+MyAppExeName\s+"([^"]+)"', ISS.read_text(encoding="utf-8"))
    assert iss_name, "could not find MyAppExeName in auto_applier.iss"
    assert f"{build_name.group(1)}.exe" == iss_name.group(1), (
        f"build.py builds {build_name.group(1)}.exe but the installer copies "
        f"{iss_name.group(1)}"
    )


def test_entry_point_sets_the_browsers_path_before_importing_the_cli():
    """run.py must call ensure_browsers_path() BEFORE anything that could launch a browser,
    otherwise the frozen app looks for Chromium inside its own bundle."""
    text = (REPO_ROOT / "run.py").read_text(encoding="utf-8")
    assert "ensure_browsers_path()" in text
    assert text.index("ensure_browsers_path()") < text.index("from auto_applier.cli.main"), (
        "ensure_browsers_path() must run before the CLI import"
    )


# --------------------------------------------------------------- browser path resolution

def test_registry_dirs_honour_an_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv(browser_paths.BROWSERS_PATH_ENV, str(tmp_path))
    assert browser_paths.browser_registry_dirs() == [tmp_path]


def test_registry_dirs_ignore_the_zero_sentinel(monkeypatch):
    """"0" is Playwright's documented "browsers live inside the package" value, not a path."""
    monkeypatch.setenv(browser_paths.BROWSERS_PATH_ENV, "0")
    dirs = browser_paths.browser_registry_dirs()
    assert len(dirs) == 2 and all(d.name in {"ms-playwright", "patchright"} for d in dirs)


def test_default_path_prefers_a_root_that_already_has_chromium(monkeypatch, tmp_path):
    monkeypatch.delenv(browser_paths.BROWSERS_PATH_ENV, raising=False)
    empty, stocked = tmp_path / "ms-playwright", tmp_path / "patchright"
    empty.mkdir()
    (stocked / "chromium-1208").mkdir(parents=True)
    monkeypatch.setattr(browser_paths, "_registry_roots", lambda: [empty, stocked])
    assert browser_paths.default_browsers_path() == stocked


def test_default_path_falls_back_to_the_platform_default(monkeypatch, tmp_path):
    """Nothing installed yet → a predictable place for the first-run fetch to land."""
    monkeypatch.delenv(browser_paths.BROWSERS_PATH_ENV, raising=False)
    a, b = tmp_path / "ms-playwright", tmp_path / "patchright"
    monkeypatch.setattr(browser_paths, "_registry_roots", lambda: [a, b])
    assert browser_paths.default_browsers_path() == a


def test_ensure_is_a_noop_when_not_frozen(monkeypatch):
    """A pip install resolves browsers correctly on its own; don't pin it."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(browser_paths.BROWSERS_PATH_ENV, raising=False)
    browser_paths.ensure_browsers_path()
    assert browser_paths.BROWSERS_PATH_ENV not in os.environ


def test_ensure_sets_the_path_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv(browser_paths.BROWSERS_PATH_ENV, raising=False)
    monkeypatch.setattr(browser_paths, "default_browsers_path", lambda: tmp_path)
    assert browser_paths.ensure_browsers_path() == str(tmp_path)
    assert os.environ[browser_paths.BROWSERS_PATH_ENV] == str(tmp_path)


def test_ensure_never_overrides_an_explicit_user_choice(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(browser_paths.BROWSERS_PATH_ENV, "D:/mybrowsers")
    assert browser_paths.ensure_browsers_path() == "D:/mybrowsers"


def test_doctor_shares_one_definition_with_the_runtime():
    """doctor, install_browser and the frozen runtime must not drift about where Chromium is."""
    from auto_applier import doctor

    assert doctor._browser_registry_dirs is browser_paths.browser_registry_dirs


# --------------------------------------------------------------- installer version wiring

def _iss_directives() -> str:
    """The .iss with ``;`` comment lines stripped.

    These guards must judge what the script DOES, not what its comments say about the bug they
    exist to prevent — otherwise documenting the old behaviour trips the very check that
    forbids it.
    """
    lines = ISS.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith(";"))


def test_installer_has_no_hardcoded_version_fallback():
    """PyInstaller doesn't stamp version info, so ``GetFileVersion(dist/AutoApplier.exe)``
    always returned "" and the .iss silently fell through to a hardcoded "2.0.0" — shipping a
    v3 product labelled v2 in the setup filename, the Add/Remove Programs entry, and every bug
    report a tester would file. Caught on the first real installer build (2026-08-03)."""
    directives = _iss_directives()
    assert '"2.0.0"' not in directives, (
        "the .iss still carries a hardcoded version fallback; it must read app_version.txt"
    )
    assert "GetFileVersion" not in directives, (
        "GetFileVersion is always empty for a PyInstaller exe — read app_version.txt instead"
    )


def test_installer_reads_the_generated_version_file():
    directives = _iss_directives()
    assert "app_version.txt" in directives
    # A missing/empty version file must FAIL the compile, not fall back to something plausible.
    assert directives.count("#error") >= 2


def test_build_installer_writes_the_package_version():
    """The installer label and the running app must agree by construction."""
    from importlib.util import module_from_spec, spec_from_file_location

    import auto_applier

    spec = spec_from_file_location(
        "build_installer", REPO_ROOT / "installer" / "build_installer.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    written = module._write_app_version()
    assert written == auto_applier.__version__.strip()
    assert module.VERSION_FILE.read_text(encoding="utf-8").strip() == written


def test_generated_version_file_is_not_committed():
    """It's a build artifact derived from __version__, not a source of truth."""
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "installer/app_version.txt" in ignore
