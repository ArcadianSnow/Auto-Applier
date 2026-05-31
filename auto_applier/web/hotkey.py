"""F6 control-handoff hotkey (spec §7a).

Phase 4 (3/M) — system-level hotkey watcher that toggles the
:class:`auto_applier.web.service.SchedulerService` between paused and running so
the user can take the browser back instantly even while it has focus.

**Why a Win32 ``RegisterHotKey`` system hook** (and not a Python keyboard
library):

* The spec is explicit: *"a system-level key hook so it works even while
  the browser has focus"* — that rules out per-window key listeners and
  any approach that needs admin / accessibility permissions (the
  zero-friction install bar).
* Win32's ``RegisterHotKey`` is the canonical system-wide hotkey API and
  works at non-elevated integrity on Windows. It's the same mechanism
  Windows itself uses for Win+L, Win+D, etc.
* No new dependency. ``ctypes`` ships with Python; ``pynput`` / ``keyboard``
  would each be ~1 MB of Python for one keypress on one platform, and
  ``keyboard`` requires root on Linux anyway.

**Cross-platform posture (Windows-native primary, soft-fail elsewhere):**
``HotkeyWatcher.start()`` returns ``False`` on non-Windows and on any
Win32 error — the dashboard's pause button still works, and the spec
explicitly says *"On a dedicated runner box, neither matters — the bot
owns the screen"*, so a Linux runner box ignoring F6 is fine.

The watcher runs a message loop on a **daemon thread** because
``RegisterHotKey`` requires the registering thread to dispatch
``WM_HOTKEY`` messages. The daemon flag means a stuck loop never blocks
process exit if the lifespan cleanup hits an edge case.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

from auto_applier.web.control import SOURCE_HOTKEY

log = logging.getLogger(__name__)

# Win32 virtual-key codes for the most likely choices. F6 is the spec
# default; the others let the (5/M) onboarding wizard offer alternates if
# the user finds F6 collides with something in their workflow.
_VK_CODES: dict[str, int] = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
}

# Win32 message constants — minimal set we need. Avoid pulling in
# pywin32 just for these; they're stable forever.
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x4156_4633  # 'AVF3' — distinct so we never collide with
                          # another app's hotkey id under the same thread.


class HotkeyWatcher:
    """Daemon-thread watcher that toggles a callback on each F-key press.

    The ``on_toggle`` callback runs on the watcher's own thread, NOT the
    asyncio event loop. The :class:`auto_applier.web.service.SchedulerService`
    methods it calls (``toggle`` / ``pause`` / ``resume``) all flip the
    thread-safe :class:`auto_applier.web.control.ControlState`, so this is safe by
    design.

    The watcher records its last ``RegisterHotKey`` failure (if any) in
    ``last_error`` so the dashboard / doctor can surface a useful message
    instead of silently doing nothing.
    """

    def __init__(
        self,
        on_toggle: Callable[[], None],
        *,
        key: str = "F6",
    ) -> None:
        self._on_toggle = on_toggle
        self._key = key.upper()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()   # signalled once register succeeds (or fails)
        self._thread_id: int = 0
        self.active: bool = False
        self.last_error: str | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Spawn the watcher thread; block briefly until ``RegisterHotKey``
        either succeeds or fails.

        Returns ``True`` if the hotkey was registered, ``False`` otherwise
        (non-Windows, unknown key, RegisterHotKey error, etc.). The caller
        echoes the return value to logs so the user knows whether F6 is
        actually wired in. Soft-fail by design: a False return is not an
        exception.
        """
        if sys.platform != "win32":
            self.last_error = f"hotkey unsupported on {sys.platform}"
            log.info("HotkeyWatcher: %s", self.last_error)
            return False
        if self._key not in _VK_CODES:
            self.last_error = (
                f"unknown hotkey {self._key!r}; choose one of {sorted(_VK_CODES)}"
            )
            log.warning("HotkeyWatcher: %s", self.last_error)
            return False
        if self._thread is not None and self._thread.is_alive():
            # Already started — idempotent so the lifespan can be re-entered
            # safely under uvicorn auto-reload.
            return self.active

        self._thread = threading.Thread(
            target=self._run, name="av3-hotkey", daemon=True
        )
        self._thread.start()
        # Wait for the thread to either register or fail. 2s is generous —
        # RegisterHotKey is a single syscall; if we don't hear back in 2s
        # something's wrong with the OS, not our code.
        self._ready.wait(timeout=2.0)
        return self.active

    def stop(self) -> None:
        """Post WM_QUIT to the watcher thread and wait for it to exit. Safe
        to call even if the watcher never started — the no-op fast paths
        handle both before-start and after-already-stopped."""
        if self._thread is None or not self._thread.is_alive():
            return
        if self._thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, _WM_QUIT, 0, 0
                )
            except Exception as e:
                # If we can't post the quit message, the daemon thread will
                # die with the process anyway. Don't raise from a teardown.
                log.warning("HotkeyWatcher: PostThreadMessage failed: %s", e)
        self._thread.join(timeout=2.0)
        self._thread = None
        self.active = False

    # ---------------------------------------------------------------- thread

    def _run(self) -> None:
        """Thread body — register the hotkey, then loop on GetMessage until
        WM_QUIT. Each WM_HOTKEY invokes ``on_toggle()``; the callback
        runs synchronously on this thread (it just flips a lock-protected
        bool, so this is fine)."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception as e:  # pragma: no cover — ctypes is stdlib
            self.last_error = f"ctypes import failed: {e}"
            self._ready.set()
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        vk = _VK_CODES[self._key]
        # fsModifiers=0 means "no modifier required" (bare F6). The Win32 docs
        # call this out as supported even though the canonical examples use
        # modifiers like MOD_CONTROL.
        ok = user32.RegisterHotKey(None, _HOTKEY_ID, 0, vk)
        if not ok:
            err = ctypes.WinError()
            self.last_error = f"RegisterHotKey failed: {err}"
            log.warning("HotkeyWatcher: %s", self.last_error)
            self._ready.set()
            return

        self.active = True
        self._ready.set()

        try:
            msg = wintypes.MSG()
            # GetMessage returns 0 on WM_QUIT, -1 on error, >0 otherwise.
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    try:
                        self._on_toggle()
                    except Exception as e:
                        # Don't kill the message loop on a callback bug —
                        # the user can still press F6 again. Log so the
                        # bug surfaces.
                        log.exception("HotkeyWatcher callback raised: %s", e)
        finally:
            try:
                user32.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:
                pass
            self.active = False


def build_hotkey_toggle(service) -> Callable[[], None]:
    """Convenience: return a zero-arg callable that toggles ``service`` via
    the ``hotkey`` source. Used by the CLI to wire ``HotkeyWatcher`` into a
    live :class:`SchedulerService` without leaking the source string."""

    def _toggle() -> None:
        service.toggle(SOURCE_HOTKEY)

    return _toggle
