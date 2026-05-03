"""End-to-end installer build driver.

Runs the three steps a Windows release needs in order:

    1. ``python scripts/write_version.py``  →  writes ``VERSION``
    2. ``python build.py``                   →  writes ``dist/AutoApplier.exe``
    3. ``iscc installer/auto_applier.iss``   →  writes ``installer/Output/AutoApplier-Setup-*.exe``

Designed to be run from any working directory; resolves PROJECT_ROOT
via the script's location. The result is a single-file installer
that bundles the PyInstaller exe + a post-install PowerShell
bootstrap (``installer/post_install.ps1``) which:

  - Installs Playwright's Chromium (the apply paths need it)
  - Detects whether Ollama is installed; offers download if not

Friends double-click ``AutoApplier-Setup-<version>.exe``, click
through the wizard, and have a working install. Install bytes are
~50 MB (PyInstaller exe + a few support files); Chromium download
happens during install (~150 MB). The ~9.6 GB Gemma 4 model is
deferred to first-run-of-the-app on purpose — the wizard surfaces
download progress, vs. the installer hanging on a 9-minute pull
with no UI.

Prerequisites on the build machine:

  * **Python 3.11+** with project dependencies installed
    (``pip install -e ".[dev]"``).
  * **PyInstaller** in the same environment (already in ``[dev]``).
  * **Inno Setup 6** with ``iscc.exe`` on PATH. Free download:
    https://jrsoftware.org/isdl.php
  * Project must be a git checkout (write_version.py uses git)
    OR ``VERSION`` must already exist (a packaged tarball is OK).

Usage::

    python installer/build_installer.py

Optional flags::

    --skip-version    Don't regenerate VERSION (keeps existing)
    --skip-pyinstaller Don't rebuild dist/AutoApplier.exe
    --skip-iscc       Stop after PyInstaller (debug)
    --output-dir PATH  Override the installer Output/ directory

Exit codes:
    0  success
    1  any subprocess failed; stderr already printed; investigate.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
DIST_EXE = PROJECT_ROOT / "dist" / "AutoApplier.exe"
ISS_FILE = INSTALLER_DIR / "auto_applier.iss"


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"\n→ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, check=False,
        )
    except FileNotFoundError as exc:
        print(f"  ERROR: command not found: {exc}", file=sys.stderr)
        return 127
    return result.returncode


def _find_iscc() -> str | None:
    """Locate ``iscc.exe`` either on PATH or in the standard
    Inno Setup 6 install directory.

    Inno Setup's installer doesn't add ``iscc.exe`` to PATH by
    default on per-user installs, so checking the conventional
    install paths is a friendly fallback. Returns the path to the
    binary if found; ``None`` if the user needs to install it.
    """
    found = shutil.which("iscc")
    if found:
        return found
    # Inno Setup 6 default install locations
    for guess in (
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ):
        if guess.exists():
            return str(guess)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-version", action="store_true")
    parser.add_argument("--skip-pyinstaller", action="store_true")
    parser.add_argument("--skip-iscc", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    print(f"Auto Applier installer build")
    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print("=" * 70)

    # 1. VERSION stamp
    if not args.skip_version:
        rc = _run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "write_version.py")],
        )
        if rc != 0:
            print(
                "  Note: write_version.py failed. The installer will fall back\n"
                "        to AppVersion='2.0.0' from the .iss file. Likely safe\n"
                "        to continue, but the run logs won't have a date stamp.",
            )

    # 2. PyInstaller bundle
    if not args.skip_pyinstaller:
        rc = _run([sys.executable, str(PROJECT_ROOT / "build.py")])
        if rc != 0:
            print("  ERROR: PyInstaller build failed. Aborting.", file=sys.stderr)
            return 1
    if not DIST_EXE.exists():
        print(
            f"  ERROR: {DIST_EXE} not found. Run without --skip-pyinstaller "
            f"or ensure dist/AutoApplier.exe exists.",
            file=sys.stderr,
        )
        return 1

    if args.skip_iscc:
        print("\n→ --skip-iscc set; stopping before installer step.")
        print(f"  PyInstaller exe is at: {DIST_EXE}")
        return 0

    # 3. Inno Setup compile
    iscc = _find_iscc()
    if not iscc:
        print(
            "\n  ERROR: iscc.exe (Inno Setup compiler) not found on PATH or in\n"
            "         standard locations. Install Inno Setup 6:\n"
            "         https://jrsoftware.org/isdl.php\n"
            "         Then re-run this script.",
            file=sys.stderr,
        )
        return 1

    iscc_args = [iscc]
    if args.output_dir:
        iscc_args.append(f"/O{args.output_dir}")
    iscc_args.append(str(ISS_FILE))
    rc = _run(iscc_args, cwd=INSTALLER_DIR)
    if rc != 0:
        print(f"  ERROR: iscc returned {rc}. Aborting.", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else INSTALLER_DIR / "Output"
    print(f"\n✓ Installer build complete.")
    print(f"  Output: {output_dir}")
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.exe")):
            print(f"  - {f.name}  ({f.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
