"""Lightweight hover tooltip widget for Tk.

Shared helper used by wizard steps to attach explanatory hover text
to labels, buttons, spinboxes, etc. Tk has no native tooltip, so we
create a borderless Toplevel on Enter and destroy it on Leave.

Usage::

    from auto_applier.gui.tooltip import Tooltip, attach_help_icon

    # Attach to any widget
    Tooltip(entry_widget, "Comma-separated list, e.g. 'python, sql'.")

    # Or render a ? icon next to a label with hover text
    attach_help_icon(parent_frame, "What this setting does...").pack(side='left')
"""

from __future__ import annotations

import tkinter as tk

# Keep the tooltip palette in sync with the Animal Crossing theme
# without hard-importing styles (avoids circular dependency risk).
_TOOLTIP_BG = "#FFF8DC"   # cornsilk
_TOOLTIP_FG = "#5D4037"   # dark brown
_TOOLTIP_BORDER = "#8D6E63"


class Tooltip:
    """Attach a hover tooltip to any Tk widget.

    The tooltip appears after a short delay when the cursor enters
    the widget and disappears when it leaves. Wraps long text at
    ``wraplength`` pixels.
    """

    def __init__(
        self,
        widget: tk.Widget,
        text: str,
        delay_ms: int = 400,
        wraplength: int = 320,
    ) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._tip_window: tk.Toplevel | None = None
        self._after_id: str | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip_window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return

        self._tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass

        frame = tk.Frame(
            tw, bg=_TOOLTIP_BG,
            highlightbackground=_TOOLTIP_BORDER, highlightthickness=1,
        )
        frame.pack()
        tk.Label(
            frame, text=self.text, justify="left",
            bg=_TOOLTIP_BG, fg=_TOOLTIP_FG,
            font=("Segoe UI", 9), wraplength=self.wraplength,
            padx=8, pady=6,
        ).pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip_window is not None:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None


def attach_help_icon(parent: tk.Widget, text: str, bg: str | None = None) -> tk.Label:
    """Create a small '?' label with a tooltip, ready to be packed.

    The caller is responsible for packing/gridding the returned widget.
    Use ``bg`` when the icon lives inside a coloured card so it blends in.
    """
    label = tk.Label(
        parent,
        text="?",
        font=("Segoe UI", 9, "bold"),
        fg="#FFFFFF",
        bg="#8D6E63",
        width=2,
        cursor="question_arrow",
        relief="flat",
    )
    if bg:
        # Pad with a bit of space from the surrounding card colour
        label.configure(highlightbackground=bg)
    Tooltip(label, text)
    return label
