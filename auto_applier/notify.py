"""User-attention notifications.

When the program needs the user to do something (manual login, CAPTCHA
solve, mid-apply review prompt) it currently relies on the user
watching the GUI dashboard or terminal. In CLI mode and on long runs
that's not practical — the user is making coffee or in another tab
and the engine sits idle for 180s before timing out.

This module ships a single ``notify_user(title, message)`` that
triggers a Windows toast, a macOS banner, or an Ubuntu libnotify
notification depending on the OS. Failure to deliver is silent — a
notification is best-effort polish, never something the engine
should block on.

Implementation: PowerShell ``New-BurntToastNotification`` is a
common Windows path but requires installing the BurntToast module.
We use the more reliable approach of ``[Windows.UI.Notifications]``
via PowerShell on Win10+, which doesn't need a module install. macOS
uses ``osascript -e 'display notification'``. Linux uses
``notify-send``. The helper picks at import time and caches.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


# A short cooldown so we never burst-ping the user with N identical
# toasts in a tight loop (the captcha check fires repeatedly).
import time
_LAST_NOTIFY: dict[str, float] = {}
_COOLDOWN_SECONDS = 30.0


def notify_user(
    title: str, message: str, *,
    force: bool = False, urgent: bool = False,
) -> bool:
    """Fire a desktop notification. Returns True on success, False
    on any failure (silent — caller never blocks on this).

    Per-(title+message) cooldown prevents spam: re-firing the same
    notification within 30s is a no-op unless ``force=True``.

    ``urgent=True`` adds an audible beep + bypasses the cooldown so
    user-blocking events (CAPTCHA, login walls) are never missed even
    when Focus Assist silences toasts. The Windows toast itself can
    take 1-2s to render via PowerShell; the beep is instant.
    """
    if not title:
        title = "Auto Applier"
    if urgent:
        force = True
        _play_alert_sound()
    key = f"{title}::{message[:60]}"
    now = time.monotonic()
    if not force:
        last = _LAST_NOTIFY.get(key, 0.0)
        if now - last < _COOLDOWN_SECONDS:
            return False
    _LAST_NOTIFY[key] = now

    try:
        if sys.platform.startswith("win"):
            return _notify_windows(title, message)
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        return _notify_linux(title, message)
    except Exception as exc:
        logger.debug("notify_user failed: %s", exc)
        return False


def _play_alert_sound() -> None:
    """Play a system alert sound to grab the user's attention.

    Bypasses Focus Assist / Do Not Disturb on Windows, which silently
    swallow toast notifications. The toast still fires (visual record)
    but the beep guarantees the user notices a CAPTCHA / login wall
    even if they're in another window.

    Best-effort: failure is silent.
    """
    try:
        if sys.platform.startswith("win"):
            import winsound
            # MB_ICONHAND = critical-stop sound. Reliably audible
            # even when notifications are silenced.
            winsound.MessageBeep(0x00000010)
        elif sys.platform == "darwin":
            subprocess.run(
                ["afplay", "/System/Library/Sounds/Sosumi.aiff"],
                timeout=3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Linux — paplay if available, else fall back to bell
            if shutil.which("paplay"):
                subprocess.run(
                    ["paplay", "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"],
                    timeout=3,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                # Terminal bell — last resort
                print("\a", end="", flush=True)
    except Exception:
        pass


def _notify_windows(title: str, message: str) -> bool:
    """Fire a Windows 10+ toast via PowerShell + WinRT.

    Avoids requiring the BurntToast module — uses the built-in
    [Windows.UI.Notifications] runtime classes available on every
    modern Windows install. The script registers a generic app id
    ("Auto Applier") so the toast attribution looks right.
    """
    if shutil.which("powershell") is None:
        return False
    # Single quotes in title/message break the script — escape them.
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "[Windows.Data.Xml.Dom.XmlDocument,"
        "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]>$null;"
        "$tmpl=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$nodes=$tmpl.GetElementsByTagName('text');"
        f"$nodes[0].AppendChild($tmpl.CreateTextNode('{safe_title}'))>$null;"
        f"$nodes[1].AppendChild($tmpl.CreateTextNode('{safe_msg}'))>$null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($tmpl);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('Auto Applier').Show($toast);"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", script],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _notify_macos(title: str, message: str) -> bool:
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}"'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _notify_linux(title: str, message: str) -> bool:
    if shutil.which("notify-send") is None:
        return False
    try:
        proc = subprocess.run(
            ["notify-send", "-a", "Auto Applier", title, message],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False
