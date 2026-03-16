"""Allow running as: python -m auto_applier"""

import sys


def main():
    # Launch GUI wizard by default, CLI if --cli flag is passed
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from auto_applier.main import cli
        cli()
    else:
        from auto_applier.gui.wizard import launch_wizard
        launch_wizard()


main()
