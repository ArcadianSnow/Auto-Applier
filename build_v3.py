"""Build Auto Applier v3 into a standalone executable using PyInstaller (spec §11a).

The v3 counterpart to v2's ``build.py``. Bundles the ``av3`` package + the
FastAPI web UI (templates/static) + the SQLite schema into a single executable
whose no-arg launch is the one-click dashboard (``run_v3.py`` → ``av3 launch``).

## Chromium is NOT bundled — fetched on first run (the lean-installer decision)

Spec §11a says "PyInstaller lean." Playwright/patchright resolve their browser
binaries through their own registry/cache, not PyInstaller's ``_MEIPASS`` temp
dir, so stuffing a ~150 MB Chromium into the onefile is both fragile and against
"lean." Instead the installer ships only the Python app, and the **first launch
runs ``av3 install-browser``** (or the installer's post-install step does) to
fetch Chromium into the normal per-user browser cache. Most applies use the
user's **real Chrome via channel** (spec §8c) and never touch this Chromium at
all — it's the stealth driver + the busy-Chrome fallback.

So the install story is two steps, both scriptable by the installer:
    1.  AutoApplierV3.exe            (this build's output, in dist/)
    2.  AutoApplierV3.exe install-browser   (first-run; idempotent)

## Requires PyInstaller (not a default dep)

    pip install -e ".[v3]" pyinstaller
    python build_v3.py

PyInstaller is intentionally absent from the runtime deps; it's a build-host
tool. This script is NOT exercised by the test suite (it shells out to a
multi-minute native build); the update-check + first-run-browser logic it relies
on (``av3/update.py``, ``av3 install-browser``) ARE unit-tested.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SEP = ";" if sys.platform == "win32" else ":"  # PyInstaller --add-data separator


def _add_data(src: str, dest: str) -> list[str]:
    return ["--add-data", f"{ROOT / src}{SEP}{dest}"]


def build(onefile: bool = True) -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "AutoApplierV3",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        # Console kept (NOT --windowed): `av3 launch` streams the server log here;
        # the Windows shortcut/.cmd wrapper hides the window for the non-technical
        # UX (see av3-launcher.cmd). A windowed build would swallow startup errors.
        # ---- bundled data: the web UI assets + the SQLite schema ----
        *_add_data("av3/web/templates", "av3/web/templates"),
        *_add_data("av3/web/static", "av3/web/static"),
        *_add_data("av3/db", "av3/db"),
        *_add_data(".env.example", "."),
        # ---- FastAPI/uvicorn need their submodules collected (dynamic imports) ----
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "fastapi",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        str(ROOT / "run_v3.py"),
    ]
    print("Building AutoApplierV3 ...")
    print("Command:\n  " + " ".join(cmd) + "\n")
    subprocess.run(cmd, check=True)
    print("\nDone. Executable is in dist/.")
    print("First-run browser fetch (or installer post-step):")
    print("  dist/AutoApplierV3 install-browser")


if __name__ == "__main__":
    build(onefile="--onedir" not in sys.argv)
