"""Trends panel — what skills to learn next, ranked.

GUI version of `cli trends`. Splits gaps into:
- UNIVERSAL: missing in >= 2 different archetypes AND >= 2 apps
- TRACK-SPECIFIC: only appear in one archetype

Each row has Mark Learning / Mark Certified / Dismiss buttons that
hit the same learning_goals storage as the CLI commands.
"""
from __future__ import annotations

import tkinter as tk
from collections import Counter, defaultdict
from tkinter import ttk

from auto_applier.gui.styles import (
    BG, BG_CARD, BORDER, PRIMARY, ACCENT, WARNING, TEXT, TEXT_LIGHT,
    TEXT_MUTED, FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)


class TrendsPanel(tk.Toplevel):
    """Window showing what to prioritize learning, with state buttons."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.title("Trends — what to learn next")
        self.configure(bg=BG)
        self.geometry("760x640")
        self.minsize(560, 480)

        self._build_ui()
        self.after(50, self._reload)

        # Modal behavior — keep the trends popup on top of the
        # dashboard. Mirror JobReviewPanel.
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=BG_CARD, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)

        ttk.Label(
            header, text="Trends — what to learn next",
            style="CardHeading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(12, 0))

        ttk.Label(
            header, text=(
                "Universal skills (cross-track) usually have the highest "
                "learning ROI."
            ),
            style="CardSmall.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(2, 12))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        action_row = tk.Frame(self, bg=BG)
        action_row.pack(fill="x", padx=PAD_X, pady=(PAD_Y, 0))
        ttk.Button(
            action_row, text="Refresh",
            command=self._reload,
        ).pack(side="left")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)

        self._canvas, self._inner = make_scrollable(body)

        self._status_label = ttk.Label(
            self, text="Loading...", style="Muted.TLabel",
        )
        self._status_label.pack(side="bottom", anchor="w", padx=PAD_X, pady=(0, 8))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        from auto_applier.analysis.gap_tracker import gaps_with_context
        from auto_applier.analysis.learning_goals import skills_by_state

        for child in self._inner.winfo_children():
            child.destroy()

        contexts = gaps_with_context()
        if not contexts:
            ttk.Label(
                self._inner,
                text=(
                    "No skill gaps recorded yet.\n\n"
                    "Apply to some jobs first — the gaps will accumulate "
                    "automatically."
                ),
                style="Small.TLabel",
                justify="left",
            ).pack(anchor="w", pady=PAD_Y)
            self._status_label.configure(text="0 gaps")
            return

        # Filter dismissed + certified
        goal_states = skills_by_state()
        excluded = goal_states["not_interested"] | goal_states["certified"]
        learning_set = goal_states["learning"]

        skill_archetypes: dict[str, set[str]] = defaultdict(set)
        skill_counts: Counter = Counter()
        for c in contexts:
            key = c.gap.field_label.lower().strip()
            if key in excluded:
                continue
            skill_archetypes[key].add(c.archetype)
            skill_counts[key] += 1

        universal: list[tuple[str, int, set[str]]] = []
        track_specific: dict[str, list[tuple[str, int]]] = defaultdict(list)

        for skill, count in skill_counts.most_common():
            archetypes = skill_archetypes[skill]
            real = archetypes - {"other"}
            if len(real) >= 2 and count >= 2:
                universal.append((skill, count, real))
            elif len(real) == 1:
                (track,) = real
                track_specific[track].append((skill, count))

        if not universal and not track_specific:
            ttk.Label(
                self._inner,
                text=(
                    "Not enough cross-archetype data yet.\n\n"
                    "Apply to a wider variety of role types to see "
                    "trend patterns."
                ),
                style="Small.TLabel",
                justify="left",
            ).pack(anchor="w", pady=PAD_Y)
            self._status_label.configure(text="No clear trends")
            return

        # Universal block
        if universal:
            self._render_section_header(
                "UNIVERSAL skills",
                "Open more doors — these appear across multiple career tracks.",
            )
            for skill, count, archetypes in universal[:15]:
                self._render_skill_row(
                    skill=skill,
                    count=count,
                    extra=f"across: {', '.join(sorted(archetypes))}",
                    is_learning=skill in learning_set,
                )

        # Track-specific block
        if track_specific:
            self._render_section_header(
                "TRACK-SPECIFIC skills",
                "Useful for one career path. Lower priority than universal.",
            )
            for track in sorted(track_specific):
                track_label = ttk.Label(
                    self._inner,
                    text=f"  {track.upper()} track:",
                    style="Subheading.TLabel",
                )
                track_label.pack(anchor="w", pady=(8, 2))
                for skill, count in track_specific[track][:10]:
                    self._render_skill_row(
                        skill=skill,
                        count=count,
                        extra="",
                        is_learning=skill in learning_set,
                    )

        total = sum(skill_counts.values())
        self._status_label.configure(
            text=f"{len(skill_counts)} unique skill(s) across {total} gap(s)",
        )

    def _render_section_header(self, title: str, subtitle: str) -> None:
        block = tk.Frame(self._inner, bg=BG)
        block.pack(fill="x", pady=(PAD_Y, 4))
        ttk.Label(block, text=title, style="Heading.TLabel").pack(anchor="w")
        ttk.Label(block, text=subtitle, style="Small.TLabel").pack(anchor="w")

    def _render_skill_row(
        self, skill: str, count: int, extra: str, is_learning: bool,
    ) -> None:
        card = tk.Frame(self._inner, bg=BG_CARD, bd=1, relief="solid",
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=2, padx=2)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=12, pady=8)

        # Top row: count badge + skill name + learning badge
        row = tk.Frame(inner, bg=BG_CARD)
        row.pack(fill="x")

        count_lbl = tk.Label(
            row, text=f" {count} ", bg=PRIMARY, fg="white",
            font=FONT_SUBHEADING, padx=4,
        )
        count_lbl.pack(side="left")

        ttk.Label(
            row, text=skill, style="CardSubheading.TLabel",
        ).pack(side="left", padx=(8, 0))

        if is_learning:
            ttk.Label(
                row, text="[learning]", style="Warning.TLabel",
            ).pack(side="left", padx=(8, 0))

        if extra:
            ttk.Label(
                inner, text=extra, style="CardSmall.TLabel",
            ).pack(anchor="w", pady=(2, 0))

        # Buttons
        btns = tk.Frame(inner, bg=BG_CARD)
        btns.pack(fill="x", pady=(6, 0))
        if not is_learning:
            ttk.Button(
                btns, text="I'm learning this",
                command=lambda s=skill: self._mark_state(s, "learning"),
            ).pack(side="left")
        ttk.Button(
            btns, text="I know this (certified)",
            command=lambda s=skill: self._mark_state(s, "certified"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            btns, text="Not interested",
            command=lambda s=skill: self._mark_state(s, "not_interested"),
        ).pack(side="left", padx=(8, 0))

    def _mark_state(self, skill: str, state: str) -> None:
        from auto_applier.analysis import learning_goals
        try:
            learning_goals.set_state(skill, state)
        except ValueError as exc:
            self._status_label.configure(text=f"Error: {exc}")
            return
        self._status_label.configure(text=f"Marked '{skill}' as {state}.")
        self._reload()
