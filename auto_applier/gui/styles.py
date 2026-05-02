"""Clean functional UI theme for Auto Applier v2."""
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------------------
# Color palette -- clean, professional
# ---------------------------------------------------------------------------
PRIMARY = "#2563EB"       # Blue
PRIMARY_DARK = "#1D4ED8"
PRIMARY_LIGHT = "#DBEAFE"
ACCENT = "#10B981"        # Green (use as bg/solid fill — white-on-this passes 3:1 large only)
ACCENT_DARK = "#059669"
WARNING = "#F59E0B"       # Amber (use as bg/solid fill only)
DANGER = "#EF4444"        # Red (use as bg/solid fill or hover only)
# Foreground-on-white variants. The base ACCENT/WARNING/DANGER colors
# are too light for text on a white card (fail WCAG AA at 4.5:1).
# Use the *_TEXT variants whenever you'd otherwise render colored
# text on BG_CARD or BG.
ACCENT_TEXT = "#047857"   # ~4.66:1 on white
WARNING_TEXT = "#B45309"  # ~4.79:1 on white
DANGER_TEXT = "#DC2626"   # ~4.83:1 on white (was hover-only; promoted to general fg)
BG = "#F8FAFC"            # Light gray background
BG_CARD = "#FFFFFF"       # White card background
TEXT = "#1E293B"          # Dark slate text
TEXT_LIGHT = "#64748B"    # Lighter text (also reused as TEXT_MUTED — see below)
TEXT_MUTED = "#64748B"    # Muted text — bumped from #94A3B8 (~3.0:1, fails AA)
                          #               to #64748B (~4.83:1, passes AA)
BORDER = "#E2E8F0"        # Light border
BORDER_FOCUS = "#93C5FD"  # Blue border on focus

# Status colors
STATUS_IDLE = "#94A3B8"    # Gray
STATUS_RUNNING = "#F59E0B" # Yellow
STATUS_SUCCESS = "#10B981" # Green
STATUS_ERROR = "#EF4444"   # Red

# ---------------------------------------------------------------------------
# Fonts (Segoe UI for Windows)
# ---------------------------------------------------------------------------
FONT_HEADING = ("Segoe UI", 16, "bold")
FONT_SUBHEADING = ("Segoe UI", 12, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 10)
FONT_BUTTON = ("Segoe UI", 10, "bold")

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
PAD_X = 24
PAD_Y = 16
CARD_PAD = 16
BORDER_RADIUS = 8


def apply_theme(root: tk.Tk) -> None:
    """Apply the clean theme to the application."""
    root.configure(bg=BG)
    style = ttk.Style()
    style.theme_use("clam")

    # Base
    style.configure(".", background=BG, foreground=TEXT, font=FONT_BODY)

    # Frames
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=BG_CARD)

    # Labels
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_BODY)
    style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT, font=FONT_BODY)
    style.configure("Heading.TLabel", font=FONT_HEADING, foreground=PRIMARY, background=BG)
    style.configure("CardHeading.TLabel", font=FONT_HEADING, foreground=PRIMARY, background=BG_CARD)
    style.configure("Subheading.TLabel", font=FONT_SUBHEADING, background=BG)
    style.configure("CardSubheading.TLabel", font=FONT_SUBHEADING, background=BG_CARD)
    style.configure("Small.TLabel", font=FONT_SMALL, foreground=TEXT_LIGHT, background=BG)
    style.configure("CardSmall.TLabel", font=FONT_SMALL, foreground=TEXT_LIGHT, background=BG_CARD)
    style.configure("Muted.TLabel", font=FONT_SMALL, foreground=TEXT_MUTED, background=BG)
    # Foreground on card bg — use the AA-compliant *_TEXT variants
    # so colored status labels are still readable. The base
    # ACCENT/WARNING/DANGER values stay reserved for solid button
    # backgrounds (where the text is white).
    style.configure("Success.TLabel", foreground=ACCENT_TEXT, background=BG_CARD, font=FONT_BODY)
    style.configure("Danger.TLabel", foreground=DANGER_TEXT, background=BG_CARD, font=FONT_BODY)
    style.configure("Warning.TLabel", foreground=WARNING_TEXT, background=BG_CARD, font=FONT_BODY)

    # Buttons
    style.configure("TButton", font=FONT_BODY, padding=(16, 8))
    style.configure(
        "Primary.TButton",
        background=PRIMARY,
        foreground="white",
        font=FONT_BUTTON,
        padding=(20, 10),
    )
    style.map(
        "Primary.TButton",
        background=[("active", PRIMARY_DARK), ("disabled", BORDER)],
        foreground=[("disabled", TEXT_MUTED)],
    )
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground="white",
        font=FONT_BUTTON,
        padding=(20, 10),
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_DARK), ("disabled", BORDER)],
    )
    style.configure(
        "Danger.TButton",
        background=DANGER,
        foreground="white",
        font=FONT_BUTTON,
        padding=(16, 8),
    )
    style.map("Danger.TButton", background=[("active", "#DC2626")])

    # Entry / Spinbox
    style.configure("TEntry", padding=6, font=FONT_BODY)
    style.configure("TSpinbox", padding=6, font=FONT_BODY)

    # Checkbutton
    style.configure("TCheckbutton", background=BG, font=FONT_BODY)
    style.configure("Card.TCheckbutton", background=BG_CARD, font=FONT_BODY)

    # Labelframe
    style.configure("TLabelframe", background=BG, font=FONT_BODY)
    style.configure(
        "TLabelframe.Label",
        background=BG,
        foreground=PRIMARY,
        font=FONT_SUBHEADING,
    )
    style.configure("Card.TLabelframe", background=BG_CARD)
    style.configure(
        "Card.TLabelframe.Label",
        background=BG_CARD,
        foreground=PRIMARY,
        font=FONT_SUBHEADING,
    )

    # Scrollbar
    style.configure("TScrollbar", troughcolor=BG, background=BORDER)
    style.map("TScrollbar", background=[("active", TEXT_MUTED)])

    # Combobox
    style.configure("TCombobox", font=FONT_BODY, padding=6)

    # Separator
    style.configure("TSeparator", background=BORDER)

    # Notebook (tabs)
    style.configure("TNotebook", background=BG)
    style.configure("TNotebook.Tab", font=FONT_BODY, padding=(12, 6))


def make_scrollable(parent: tk.Widget) -> tuple[tk.Canvas, ttk.Frame]:
    """Create a scrollable frame inside *parent*.

    Returns (canvas, inner_frame).  Pack widgets into inner_frame.
    The canvas fills *parent* and scrolls vertically.

    Wheel scrolling is bound per-canvas (NOT global) — the previous
    ``bind_all`` made the last-mounted scrollable area steal wheel
    events from every other step / panel in the app, so wheel only
    worked on whichever wizard step was rendered last. Now each
    canvas binds the wheel only while the pointer is over it.

    Tab-into-view: any widget that gains focus inside ``inner`` is
    scrolled into the visible viewport. Without this, tabbing to a
    text field below the fold leaves the cursor invisible until the
    user reaches for the mouse.
    """
    canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)

    inner = ttk.Frame(canvas, style="TFrame")
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    canvas.configure(yscrollcommand=scrollbar.set)

    def _on_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        # Match inner frame width to canvas width
        canvas.itemconfig(inner_id, width=canvas.winfo_width())

    inner.bind("<Configure>", _on_configure)
    canvas.bind("<Configure>", _on_configure)

    # ------------------------------------------------------------------
    # Wheel scrolling — per-canvas, scoped to pointer-hover. Windows /
    # macOS deliver <MouseWheel> with event.delta in 120-unit ticks;
    # Linux uses <Button-4> / <Button-5>.
    # ------------------------------------------------------------------
    def _on_mousewheel(event):
        if hasattr(event, "delta") and event.delta:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        else:
            # Linux click-wheel events
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def _bind_wheel(_e=None):
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind_wheel(_e=None):
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    # Bind wheel only while pointer is over THIS canvas's visible
    # area, so two scrollable surfaces never fight over events.
    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)
    inner.bind("<Enter>", _bind_wheel)
    inner.bind("<Leave>", _unbind_wheel)

    # ------------------------------------------------------------------
    # Tab-into-view — when a child widget gets focus (typically via
    # Tab), make sure it's actually visible. Without this, tabbing
    # past the fold puts the focused entry behind the bottom edge
    # and the user has no idea where the cursor went.
    # ------------------------------------------------------------------
    def _scroll_focused_into_view(event):
        # Only react if the focused widget is a descendant of our inner.
        widget = event.widget
        try:
            cur = widget
            while cur is not None and cur is not inner:
                cur = cur.master
            if cur is not inner:
                return
        except Exception:
            return
        try:
            # widget position relative to canvas
            wy = widget.winfo_rooty() - canvas.winfo_rooty()
            wh = widget.winfo_height()
            ch = canvas.winfo_height()
            if ch <= 0:
                return
            scroll_top, scroll_bottom = canvas.yview()
            inner_h = max(inner.winfo_height(), 1)
            # Convert pixel-space to fraction-space
            top_frac = (wy / inner_h) + scroll_top
            bot_frac = ((wy + wh) / inner_h) + scroll_top
            if wy < 0:
                # Widget is above the viewport — scroll up so its top
                # is at the viewport top (with a small padding).
                target = max(top_frac - 0.02, 0.0)
                canvas.yview_moveto(target)
            elif wy + wh > ch:
                # Widget is below the viewport — scroll down so its
                # bottom is at the viewport bottom (with padding).
                visible_frac = ch / inner_h
                target = min(bot_frac - visible_frac + 0.02, 1.0)
                canvas.yview_moveto(target)
        except Exception:
            pass

    # bind_class would catch every TEntry / TCombobox in the app; we
    # restrict to inner via the descendant-check inside the handler.
    inner.bind_all("<FocusIn>", _scroll_focused_into_view, add="+")

    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    return canvas, inner
