"""ttk style definitions for the wizard UI."""

import tkinter as tk
from tkinter import ttk


def apply_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")

    # Primary button (blue)
    style.configure(
        "Primary.TButton",
        background="#2563EB",
        foreground="#FFFFFF",
        font=("Segoe UI", 10, "bold"),
        padding=(16, 8),
        borderwidth=0,
    )
    style.map(
        "Primary.TButton",
        background=[("active", "#1D4ED8"), ("disabled", "#93C5FD")],
        foreground=[("disabled", "#FFFFFF")],
    )

    # Secondary button (white with blue text)
    style.configure(
        "Secondary.TButton",
        background="#FFFFFF",
        foreground="#2563EB",
        font=("Segoe UI", 10, "bold"),
        padding=(16, 8),
        borderwidth=1,
        relief="solid",
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#EFF6FF")],
    )

    # Ghost button (subtle)
    style.configure(
        "Ghost.TButton",
        background="#F5F7FA",
        foreground="#374151",
        font=("Segoe UI", 10),
        padding=(12, 8),
        borderwidth=0,
    )
    style.map(
        "Ghost.TButton",
        background=[("active", "#E2E8F0")],
    )

    # Danger button (red text)
    style.configure(
        "Danger.TButton",
        background="#FFFFFF",
        foreground="#EF4444",
        font=("Segoe UI", 10, "bold"),
        padding=(12, 8),
        borderwidth=1,
        relief="solid",
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#FEF2F2")],
    )

    # Entry fields
    style.configure(
        "TEntry",
        fieldbackground="#FFFFFF",
        foreground="#1E293B",
        font=("Segoe UI", 10),
        padding=6,
    )

    # Error entry
    style.configure(
        "Error.TEntry",
        fieldbackground="#FEF2F2",
        foreground="#1E293B",
        font=("Segoe UI", 10),
        padding=6,
    )

    # Checkbutton
    style.configure(
        "TCheckbutton",
        background="#F5F7FA",
        foreground="#374151",
        font=("Segoe UI", 9),
    )
