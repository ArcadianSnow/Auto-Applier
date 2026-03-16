"""ttk style definitions — Animal Crossing 'Tom Nook's Job Agency' theme."""

import tkinter as tk
from tkinter import ttk

# ── Animal Crossing Color Palette ────────────────────────────────

# Primary
NOOK_GREEN = "#4CAF7D"
NOOK_GREEN_HOVER = "#3D9A6A"
NOOK_GREEN_DARK = "#2E7D52"
NOOK_TAN = "#C8A882"
LEAF_GOLD = "#E8B84B"
LEAF_GOLD_DARK = "#C89030"

# Secondary / Accent
BLUEBELL = "#7BB8D4"
PEACH = "#F4A88A"
PEACH_HOVER = "#E8875E"
LAVENDER = "#C4AED8"

# Backgrounds
SANDY_SHORE = "#F7F0E6"
CREAM = "#FDFAF4"
MORNING_SKY = "#E8F4F8"
DRIFTWOOD = "#EDE0CE"
WARM_WHITE = "#FFFEF9"

# Text
SOIL_BROWN = "#3D2B1F"
BARK_BROWN = "#5C3D2E"
DRIFTWOOD_GRAY = "#8B7355"
FOGGY = "#B5A48A"

# Status
ERROR_RED = "#C44B2B"
ERROR_BG = "#FAEAE4"
ERROR_DOT = "#E06040"
SUCCESS_GREEN = NOOK_GREEN_DARK
SUCCESS_BG = "#D4EFE3"
INFO_BLUE = "#2A6F8E"
WARNING_GOLD = LEAF_GOLD_DARK
SKIP_GRAY = "#B5A48A"

# Borders
BORDER_LIGHT = "#D4C4A8"
BORDER_MED = "#C8A882"
BORDER_DARK = "#B8906A"

# Step indicator
STEP_DONE = NOOK_GREEN
STEP_ACTIVE = LEAF_GOLD
STEP_UPCOMING = "#D4C4A8"

# Fonts
HEADING_FONT = "Bahnschrift"
BODY_FONT = "Segoe UI"
MONO_FONT = "Consolas"


def apply_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")

    # Primary button (Nook Green)
    style.configure(
        "Primary.TButton",
        background=NOOK_GREEN,
        foreground=WARM_WHITE,
        font=(HEADING_FONT, 10, "bold"),
        padding=(16, 8),
        borderwidth=0,
    )
    style.map(
        "Primary.TButton",
        background=[("active", NOOK_GREEN_HOVER), ("disabled", "#A8D5BC")],
        foreground=[("disabled", "#D4EDE0")],
    )

    # Secondary button (Driftwood)
    style.configure(
        "Secondary.TButton",
        background=DRIFTWOOD,
        foreground=BARK_BROWN,
        font=(HEADING_FONT, 10, "bold"),
        padding=(16, 8),
        borderwidth=1,
        relief="solid",
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#E0CEB8")],
    )

    # Ghost button (transparent-ish)
    style.configure(
        "Ghost.TButton",
        background=SANDY_SHORE,
        foreground=BARK_BROWN,
        font=(BODY_FONT, 10),
        padding=(12, 8),
        borderwidth=0,
    )
    style.map(
        "Ghost.TButton",
        background=[("active", DRIFTWOOD)],
    )

    # Danger button (Peach)
    style.configure(
        "Danger.TButton",
        background=PEACH,
        foreground=SOIL_BROWN,
        font=(HEADING_FONT, 10, "bold"),
        padding=(12, 8),
        borderwidth=0,
    )
    style.map(
        "Danger.TButton",
        background=[("active", PEACH_HOVER)],
        foreground=[("active", WARM_WHITE)],
    )

    # Entry fields
    style.configure(
        "TEntry",
        fieldbackground=WARM_WHITE,
        foreground=BARK_BROWN,
        font=(BODY_FONT, 10),
        padding=6,
        bordercolor=NOOK_TAN,
    )

    # Error entry
    style.configure(
        "Error.TEntry",
        fieldbackground="#FEF5F2",
        foreground=BARK_BROWN,
        font=(BODY_FONT, 10),
        padding=6,
        bordercolor=ERROR_DOT,
    )

    # Checkbutton
    style.configure(
        "TCheckbutton",
        background=SANDY_SHORE,
        foreground=BARK_BROWN,
        font=(BODY_FONT, 9),
    )

    # Separator
    style.configure(
        "TSeparator",
        background=BORDER_LIGHT,
    )
