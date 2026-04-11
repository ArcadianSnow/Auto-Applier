"""Allow running as: python -m auto_applier"""
import sys


def _hide_console_on_windows() -> None:
    """Hide the console window that Python attaches to GUI launches.

    On Windows, running ``python run.py`` opens a cmd.exe host window
    that sits behind the Tk wizard for the whole session. It serves
    no purpose (all GUI output goes to the dashboard) and looks
    unprofessional. This helper hides that window via the Win32 API.

    Safe fail — if we're not on Windows, the user launched via
    pythonw.exe (no console to hide), or ctypes is unavailable,
    we silently skip.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE = 0
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def main():
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from auto_applier.main import cli
        cli()
    else:
        # GUI mode — hide the host console window on Windows so
        # users don't see a blank terminal sitting behind the wizard.
        _hide_console_on_windows()
        from auto_applier.gui.wizard import launch_wizard
        launch_wizard()


if __name__ == "__main__":
    main()
