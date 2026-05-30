"""Entry point for the bundled Auto Applier v3 executable (spec §11a).

Double-clicking the installed ``AutoApplierV3`` (no CLI args) runs ``av3 launch``
— the one-click launcher that starts the worker+server and opens the dashboard
tab. Passing args runs the full ``av3`` CLI (``AutoApplierV3 doctor``,
``AutoApplierV3 telemetry status``, …), so the single bundled binary is both the
non-technical launcher and the power-user CLI.

This is the PyInstaller entry script (see ``build_v3.py``). It is deliberately
tiny: argv shaping + dispatch into the existing Click group, nothing else.
"""

from __future__ import annotations

import sys

from av3.cli.main import cli

if __name__ == "__main__":
    # No subcommand → behave like the one-click launcher. The launcher then
    # spawns ``<this exe> serve`` (see launch_cmd's frozen-aware child_args).
    if len(sys.argv) == 1:
        sys.argv.append("launch")
    cli()
