"""Allow running as: python -m auto_applier"""
import sys


def main():
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from auto_applier.main import cli
        cli()
    else:
        from auto_applier.gui.wizard import launch_wizard
        launch_wizard()


if __name__ == "__main__":
    main()
