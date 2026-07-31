"""Entry point for the bundled Auto Applier executable (spec §11a).

Double-clicking the installed ``AutoApplier`` (no CLI args) runs ``av3 launch`` — the
one-click launcher that starts the worker+server and opens the dashboard tab. Passing args
runs the full ``av3`` CLI (``AutoApplier doctor``, ``AutoApplier telemetry status``, …), so
the single bundled binary is both the non-technical launcher and the power-user CLI.

This is the PyInstaller entry script (see ``build.py``). It is deliberately tiny: fix the
browser path, shape argv, dispatch into the existing Click group. Nothing else.
"""

from __future__ import annotations

import sys

# MUST run before anything can launch a browser. Inside a PyInstaller bundle the patchright
# package lives in the extracted _internal/ dir, so the driver looks for Chromium *inside the
# bundle* — where it never is, because build.py deliberately doesn't ship it ("fetched on first
# run"). Pointing PLAYWRIGHT_BROWSERS_PATH at the ordinary per-user cache makes the first-run
# fetch and every later launch use the same directory. No-op when not frozen, and an explicit
# user-set value always wins. See auto_applier/browser_paths.py.
from auto_applier.browser_paths import ensure_browsers_path  # noqa: E402

ensure_browsers_path()

from auto_applier.cli.main import cli  # noqa: E402

if __name__ == "__main__":
    # No subcommand → behave like the one-click launcher. The launcher then
    # spawns ``<this exe> serve`` (see launch_cmd's frozen-aware child_args).
    if len(sys.argv) == 1:
        sys.argv.append("launch")
    cli()
