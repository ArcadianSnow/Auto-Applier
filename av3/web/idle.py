"""Idle-detection auto-pause (spec §7a — *optional* complement to F6).

Phase 4 (3/M) — when the user is actively typing / clicking, pause the
scheduler so the bot isn't fighting them for the cursor; when they've
been idle for ``idle_threshold_s``, release the pause so the bot can
work uninterrupted again.

**Semantic:** *user-active → bot paused.* The pause source name is
``idle`` because the predicate flips ON when the user is the OPPOSITE of
idle. (The naming is awkward but matches the dashboard's "Paused —
user active" reason string and the ControlState canonical source set.)

**Why ``GetLastInputInfo``** (Win32 directly, no library):

* Returns the system-wide "last input" tick — keyboard, mouse, anything.
  That's the exact signal the spec wants (any user interaction).
* Zero dependency. ``ctypes`` ships with Python.
* Works at non-elevated integrity.
* Cross-process — it reports system input, not just our process.

**Cross-platform posture:** non-Windows, ``start()`` returns ``False`` and
the watcher does nothing. macOS has CGEventSourceSecondsSinceLastEventType
(equivalent functionality) and Linux has XScreenSaverQueryInfo — both
are doable later, but this is a v3.0 *Windows-native primary* tradeoff
(noted in [[project_v3_rewrite]]).

The poll loop runs on a daemon thread so a stuck poll never blocks
process exit.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from av3.web.control import SOURCE_IDLE, ControlState

log = logging.getLogger(__name__)


class IdleWatcher:
    """Daemon-thread watcher that flips ``control[idle]`` based on system
    input cadence.

    Two thresholds drive the state machine:

    * ``idle_threshold_s`` — how long the user must be silent before the
      bot is allowed to drive. Sensible default: 60s. Lower = more
      aggressive (bot resumes faster but pauses on a stray click);
      higher = more deferential.
    * ``poll_interval_s`` — how often we re-check. The pause-state lag is
      bounded by this; 2s is a good balance.

    On every poll, the watcher compares "seconds since last input" to the
    threshold and pauses/resumes the ``idle`` source. Idempotent at the
    ControlState level — repeated pause(SOURCE_IDLE) calls don't compound.

    Tests can pass a fake ``read_idle_seconds`` to drive the loop without
    Win32. The production path uses ``_win32_idle_seconds`` (defined at
    module scope so it can be patched in tests via monkeypatch).
    """

    def __init__(
        self,
        control: ControlState,
        *,
        idle_threshold_s: float = 60.0,
        poll_interval_s: float = 2.0,
        read_idle_seconds=None,
    ) -> None:
        self._control = control
        self._idle_threshold_s = float(idle_threshold_s)
        self._poll_interval_s = float(poll_interval_s)
        # ``read_idle_seconds`` defaults to the Win32 path. Tests inject a
        # fake; macOS/Linux backends can swap implementations here in v3.1.
        self._read_idle = read_idle_seconds or _win32_idle_seconds
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.active: bool = False
        self.last_error: str | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Spawn the poll thread. Returns ``True`` if the watcher is now
        running (Windows or a test-injected reader), ``False`` on unsupported
        platforms (spec §7a allows this — "On a dedicated runner box,
        neither matters")."""
        if sys.platform != "win32" and self._read_idle is _win32_idle_seconds:
            self.last_error = f"idle-detect unsupported on {sys.platform}"
            log.info("IdleWatcher: %s", self.last_error)
            return False
        if self._thread is not None and self._thread.is_alive():
            return self.active
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="av3-idle", daemon=True
        )
        self._thread.start()
        self.active = True
        return True

    def stop(self) -> None:
        """Signal the poll thread to exit; wait briefly for it to settle.
        Always clears the ``idle`` pause source so a stopped watcher doesn't
        leave the scheduler stuck-paused."""
        if self._thread is None:
            return
        self._stop_evt.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        self.active = False
        # Defensive: ensure we don't leave a lingering pause behind.
        try:
            self._control.resume(SOURCE_IDLE)
        except Exception:
            pass

    # ---------------------------------------------------------------- loop

    def _run(self) -> None:
        """Poll body — runs on the daemon thread. Each tick reads the idle
        seconds, compares to threshold, and flips the pause source. The
        :class:`ControlState` mutators are idempotent so repeated pauses
        / resumes are cheap and free of churn."""
        while not self._stop_evt.is_set():
            try:
                idle_s = self._read_idle()
            except Exception as e:
                # Don't kill the loop on a transient read error. Mark it and
                # try again next tick — the user's signal is far more
                # important than perfect data.
                self.last_error = f"read_idle_seconds raised: {e}"
                log.warning("IdleWatcher: %s", self.last_error)
                idle_s = None

            if idle_s is not None:
                if idle_s < self._idle_threshold_s:
                    # User just touched the keyboard / mouse — pause the bot
                    # with a "user active" reason that the dashboard renders
                    # in the status bar.
                    self._control.pause(
                        SOURCE_IDLE,
                        reason=f"user active {idle_s:.0f}s ago",
                    )
                else:
                    self._control.resume(SOURCE_IDLE)

            # Use Event.wait so stop() can interrupt the sleep promptly. A
            # plain time.sleep would delay shutdown by up to poll_interval_s.
            self._stop_evt.wait(timeout=self._poll_interval_s)


def _win32_idle_seconds() -> float:
    """Return seconds since the last system-wide keyboard / mouse input,
    via the Win32 ``GetLastInputInfo`` API.

    Raises ``OSError`` (or ``RuntimeError`` for non-Windows / missing
    user32) if the call can't be made. The caller treats that as "skip
    this tick" — better than poisoning the loop.
    """
    if sys.platform != "win32":
        raise RuntimeError(f"_win32_idle_seconds is Windows-only (got {sys.platform})")
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError()
    # GetTickCount wraps at 49.7 days — long enough that idle-detection
    # accuracy past that boundary isn't a real concern, but we mask with
    # 0xFFFFFFFF in case the cast to int crosses the wrap.
    now = ctypes.windll.kernel32.GetTickCount()
    delta_ms = (now - info.dwTime) & 0xFFFFFFFF
    return delta_ms / 1000.0
