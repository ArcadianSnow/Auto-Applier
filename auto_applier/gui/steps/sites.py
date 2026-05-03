"""Step 2: Platform selection.

Two new categories shipped 2026-05-03:

  - ``linkedin_nodriver`` — experimental anti-detect backend for
    LinkedIn discovery. Optional pip dependency; the card includes
    an "Install now" helper that pip-installs nodriver in a
    background thread so users don't have to drop into a shell.
  - ``ats_greenhouse``, ``ats_lever``, ``ats_ashby`` — ATS public-
    API discovery. Each card expands when checked to reveal a
    multi-line slug entry box plus a "Test slug" button that does
    a one-shot HTTP probe against the ATS endpoint and reports
    ``found, NN open jobs`` or ``404 — wrong slug`` inline.

The step still validates that at least one platform is selected,
plus a new check: ``linkedin`` and ``linkedin_nodriver`` are
mutually exclusive (running both would 2× the LinkedIn rate-limit
hit for the same listings, and competing browsers fight over the
profile dir).
"""
import logging
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Starter-pack slugs for the "Try popular companies" quick-start button.
# Curated for tech-leaning candidates — these are well-known boards on
# each ATS that we've manually verified produce real jobs in 2026.
#
# Updating this list: pick companies most relevant to the user base
# (right now: tech / data / SWE) and verify the slug works by visiting
# ``boards.greenhouse.io/<slug>`` etc. One-line companies only — long
# lists hide the curation. If a friend asks "why isn't X here", they
# can add it themselves; the starter pack is for "I have no idea what
# to type, give me jobs."
# ----------------------------------------------------------------------
STARTER_PACK_SLUGS: dict[str, list[str]] = {
    "greenhouse": [
        "stripe", "github", "airbnb", "plaid", "robinhood",
        "discord", "anthropic", "dropbox",
    ],
    "lever": [
        "netflix", "shopify", "palantir",
    ],
    "ashby": [
        "openai", "ramp", "linear", "vanta",
    ],
}


# ATS URL patterns. Tuple is (ats_id, regex). The regex captures the
# slug as group 1. We keep this list module-level so the pure parser
# is testable without instantiating Tk.
#
# Pattern notes:
#   - greenhouse: boards.greenhouse.io/<slug>[/jobs/...]
#                 boards-api.greenhouse.io/v1/boards/<slug>/jobs
#                 job-boards.greenhouse.io/<slug>/jobs/...
#   - lever:      jobs.lever.co/<slug>[/<post-id>]
#                 api.lever.co/v0/postings/<slug>?...
#   - ashby:      jobs.ashbyhq.com/<slug>[/<post-id>]
#                 api.ashbyhq.com/posting-api/job-board/<slug>
_ATS_URL_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("greenhouse", re.compile(
        r"https?://boards(?:-api)?\.greenhouse\.io/(?:v\d+/boards/)?([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )),
    ("greenhouse", re.compile(
        r"https?://job-boards\.greenhouse\.io/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )),
    ("lever", re.compile(
        r"https?://(?:jobs|api)\.lever\.co/(?:v\d+/postings/)?([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )),
    ("ashby", re.compile(
        r"https?://(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )),
)


def detect_ats_from_url(url: str) -> tuple[str, str] | None:
    """Parse a careers-page URL and return ``(ats_id, slug)`` if
    recognized, ``None`` otherwise.

    Recognized hosts (any path on these is fair game):
      - boards.greenhouse.io / boards-api.greenhouse.io /
        job-boards.greenhouse.io
      - jobs.lever.co / api.lever.co
      - jobs.ashbyhq.com / api.ashbyhq.com

    The slug is the first ``[A-Za-z0-9_-]+`` segment after the host.
    Some patterns nest the slug after a versioned API prefix (e.g.
    ``boards-api.greenhouse.io/v1/boards/<slug>/jobs``); the regex
    handles that.

    Pure function, no I/O. Returns ``None`` for unrecognized URLs
    so the caller can show "we don't know that site yet" rather
    than misclassify.
    """
    if not url:
        return None
    cleaned = url.strip()
    # Tolerate users pasting the URL with surrounding whitespace,
    # quote chars from a copy/paste, or trailing punctuation.
    cleaned = cleaned.strip("\"'<> \t\n\r")
    for ats_id, pattern in _ATS_URL_PATTERNS:
        m = pattern.match(cleaned)
        if m:
            slug = m.group(1).strip()
            if slug:
                return (ats_id, slug)
    return None


# Platform metadata. Tuple shape is intentional; new fields opt-in
# via the trailing ``options`` dict so we don't have to update every
# entry when a new platform-type capability appears.
#
# Fields:
#   key       — registry id, also the wizard data var prefix
#   name      — header text on the card
#   desc      — body copy under the checkbox
#   options   — dict of extras: {"ats": True, "ats_id": "greenhouse"}
#               for ATS API platforms; {"nodriver": True} for the
#               LinkedIn-Nodriver card
PLATFORMS = [
    (
        "indeed",
        "Indeed  (recommended for your first run)",
        "High-volume job board covering every industry and level. "
        "The friendliest to automation, so start here while you're "
        "still learning the tool.",
        {},
    ),
    (
        "dice",
        "Dice",
        "Specialized in technology and engineering roles. Also "
        "automation-friendly.",
        {},
    ),
    (
        "ziprecruiter",
        "ZipRecruiter",
        "AI-powered matching, broad employer range. Moderate "
        "anti-bot — works well once you've confirmed the basics.",
        {},
    ),
    (
        "linkedin",
        "LinkedIn  (discovery only — Auto Applier will NOT apply for you)",
        "LinkedIn's anti-automation blocks direct job-page navigation, "
        "so Auto Applier only SCANS LinkedIn listings and scores them "
        "for you. Matches show up under 'Almost — apply manually' so "
        "you can open each one in your normal browser and click Easy "
        "Apply yourself. No auto-apply, no submissions, no risk to "
        "your account.",
        {},
    ),
    (
        "linkedin_nodriver",
        "LinkedIn — experimental engine (Nodriver)  \U0001F9EA",
        "Same discovery-only flow as the standard LinkedIn option, "
        "but uses a different anti-detect backend (Nodriver). "
        "May get past the soft-block that defeats the standard "
        "engine. Requires a one-time install of the 'nodriver' "
        "Python package (~50 MB). Pick EITHER this OR the standard "
        "LinkedIn — never both.",
        {"nodriver": True},
    ),
    (
        "ats_greenhouse",
        "Greenhouse boards (Stripe, GitHub, Airbnb, Plaid, …)  ⚡",
        "Greenhouse hosts careers pages for thousands of companies. "
        "Use the Quick-start card above to add boards in one click, "
        "or list company slugs below (one per line). Tip: the slug "
        "is the company name in URLs like boards.greenhouse.io/<slug>.",
        {"ats": True, "ats_id": "greenhouse"},
    ),
    (
        "ats_lever",
        "Lever boards (Netflix, Shopify, Palantir, …)  ⚡",
        "Lever hosts careers pages for many tech companies. Use the "
        "Quick-start card above to add boards in one click, or list "
        "company slugs below (one per line). Tip: the slug is the "
        "company name in URLs like jobs.lever.co/<slug>.",
        {"ats": True, "ats_id": "lever"},
    ),
    (
        "ats_ashby",
        "Ashby boards (OpenAI, Ramp, Linear, Vanta, …)  ⚡",
        "Ashby is a newer ATS used by many AI-era startups. Use the "
        "Quick-start card above to add boards in one click, or list "
        "company slugs below (one per line). Tip: the slug is the "
        "company name in URLs like jobs.ashbyhq.com/<slug>.",
        {"ats": True, "ats_id": "ashby"},
    ),
]


class SitesStep(ttk.Frame):
    """Platform selection with checkboxes, descriptions, and per-
    platform configuration sub-cards (ATS slug entry, Nodriver
    install helper).
    """

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        # Keep references to the per-platform "extra" frames so we
        # can show / hide them as the checkbox state changes.
        self._extras: dict[str, tk.Widget] = {}
        # Per-ATS status labels for the "Test slug" button feedback.
        self._ats_status: dict[str, tk.Label] = {}
        # Nodriver install button + status label.
        self._nodriver_status: tk.Label | None = None
        self._nodriver_install_btn: ttk.Button | None = None
        self._build()

    def _build(self) -> None:
        # Heading (outside the scroll area so it stays pinned)
        ttk.Label(
            self, text="Pick which job sites to use", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text=(
                "Check the boxes for sites where you already have an "
                "account. Not sure? Start with just Indeed for your first "
                "run — it has the most jobs and the smoothest auto-apply "
                "flow."
            ),
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Scroll container — 8 platform cards now overflow the wizard at
        # default size, especially on smaller monitors. Mirror the
        # personal/answers/ready steps' pattern so the user can always
        # reach the cards below the fold.
        scroll_container = ttk.Frame(self)
        scroll_container.pack(
            fill="both", expand=True, padx=PAD_X, pady=(0, PAD_Y),
        )
        _canvas, inner = make_scrollable(scroll_container)

        # Quick-start card for the ATS API discovery channels. Comes
        # BEFORE the platform cards because the user has to understand
        # the ATS concept before deciding whether to enable Greenhouse
        # / Lever / Ashby. Without this card the cards below were
        # presenting "type slugs here" with no context — friends had
        # no idea what a slug was or how to find one.
        self._build_ats_quickstart_card(inner)

        # Platform cards
        for key, name, desc, options in PLATFORMS:
            card = tk.Frame(
                inner, bg=BG_CARD, highlightbackground=BORDER,
                highlightthickness=1, padx=16, pady=12,
            )
            card.pack(fill="x", padx=4, pady=4)

            var = self.wizard.data[f"{key}_enabled"]

            top_row = tk.Frame(card, bg=BG_CARD)
            top_row.pack(fill="x")

            cb = ttk.Checkbutton(
                top_row, text=name, variable=var,
                style="Card.TCheckbutton",
                command=lambda k=key: self._on_toggle(k),
            )
            cb.pack(side="left")

            tk.Label(
                card, text=desc, font=FONT_SMALL,
                fg=TEXT_LIGHT, bg=BG_CARD, anchor="w",
                wraplength=720, justify="left",
            ).pack(anchor="w", padx=(24, 0), pady=(2, 0))

            # Per-platform extras — only ATS, Nodriver, or
            # ZipRecruiter cards have them. Hidden by default;
            # revealed when the platform's checkbox is toggled on.
            if options.get("ats"):
                extra = self._build_ats_extra(card, key, options["ats_id"])
                self._extras[key] = extra
            elif options.get("nodriver"):
                extra = self._build_nodriver_extra(card)
                self._extras[key] = extra
            elif key == "ziprecruiter":
                extra = self._build_zr_ack_extra(card)
                self._extras[key] = extra

            # Show the extra at build time if the checkbox is already
            # set (loaded from saved config).
            if key in self._extras and var.get():
                self._extras[key].pack(fill="x", padx=(24, 0), pady=(8, 0))

        # Note — packed inside `inner` (not `self`) so it scrolls with
        # the cards above it. On small monitors the note is part of
        # what the user has to scroll to reach.
        note_frame = tk.Frame(inner, bg=BG)
        note_frame.pack(fill="x", padx=4, pady=(PAD_Y, 4))

        tk.Label(
            note_frame,
            text=(
                "ℹ  When you start a run, Auto Applier opens a real "
                "browser window and asks you to log in yourself the first "
                "time. This is by design — it keeps your passwords safe "
                "and avoids triggering site security. You only have to log "
                "in once per site; after that, the browser remembers you."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG,
            wraplength=700, justify="left",
        ).pack(anchor="w")

    # ------------------------------------------------------------------
    # Per-card extras
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # ATS quick-start card (above the platform cards)
    # ------------------------------------------------------------------

    def _build_ats_quickstart_card(self, parent: tk.Widget) -> None:
        """Friendly intro to the ATS API discovery feature.

        Three blocks:

          1. **What this is** — one-paragraph plain-English
             explanation of "ATS API discovery" so users without a
             recruiting background know what they're enabling.
          2. **Try popular companies** — single button that loads
             curated starter slugs into all three ATS StringVars
             AND auto-enables the corresponding platform cards.
             Lets a user with zero research see real jobs on the
             first run.
          3. **Add by URL** — paste any careers-page URL (Greenhouse,
             Lever, or Ashby), we auto-detect the ATS and slug. No
             knowledge of "what a slug is" required.

        Both 2 and 3 are fail-soft — they never crash on a malformed
        input; they show a friendly message and leave existing slug
        lists alone.
        """
        card = tk.Frame(
            parent, bg=BG_CARD,
            highlightbackground=PRIMARY, highlightthickness=1,
            padx=16, pady=12,
        )
        card.pack(fill="x", padx=4, pady=(0, 8))

        # --- Block 1: explainer ---
        tk.Label(
            card, text="✨ Quick-start: get jobs from company boards",
            font=FONT_SUBHEADING, fg=PRIMARY, bg=BG_CARD,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            card,
            text=(
                "Many companies post jobs through one of three "
                "Applicant Tracking Systems — Greenhouse, Lever, or "
                "Ashby. Auto Applier can pull jobs straight from "
                "those systems with no browser, no rate limits, and "
                "no anti-bot risk. You just need to tell us WHICH "
                "companies to watch (their identifier on each system, "
                "called a \"slug\")."
            ),
            font=FONT_SMALL, fg=TEXT, bg=BG_CARD,
            anchor="w", justify="left", wraplength=720,
        ).pack(anchor="w", pady=(0, 8))

        # --- Block 2: starter pack button ---
        starter_row = tk.Frame(card, bg=BG_CARD)
        starter_row.pack(fill="x", pady=(0, 8))
        ttk.Button(
            starter_row, text="Try popular companies",
            style="Primary.TButton",
            command=self._apply_starter_pack,
        ).pack(side="left")
        tk.Label(
            starter_row,
            text=(
                f"  Loads {sum(len(v) for v in STARTER_PACK_SLUGS.values())} "
                "well-known boards (Stripe, GitHub, Netflix, OpenAI, …) "
                "into all three ATSes at once."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left",
        ).pack(side="left")

        # --- Block 3: paste-URL helper ---
        url_label = tk.Label(
            card, text="Or paste a company's careers URL:",
            font=FONT_SMALL, fg=TEXT, bg=BG_CARD,
            anchor="w",
        )
        url_label.pack(anchor="w", pady=(4, 2))

        url_row = tk.Frame(card, bg=BG_CARD)
        url_row.pack(fill="x")
        self._url_entry = ttk.Entry(url_row, width=64, font=FONT_BODY)
        self._url_entry.pack(side="left", fill="x", expand=True)
        # Hitting Return runs the same handler as the button.
        self._url_entry.bind(
            "<Return>", lambda _e: self._add_slug_from_url(),
        )
        ttk.Button(
            url_row, text="Add",
            command=self._add_slug_from_url,
        ).pack(side="left", padx=(8, 0))

        # Status line for paste-URL feedback. Shows "✓ Added stripe
        # to Greenhouse" or "✗ Couldn't recognize this URL" inline.
        self._url_status = tk.Label(
            card, text="",
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left", wraplength=720,
        )
        self._url_status.pack(anchor="w", pady=(2, 0))

        # Help footnote — tells users where to look for slugs if
        # they want to expand beyond the starter pack.
        tk.Label(
            card,
            text=(
                "Need to find a slug yourself? Visit the company's "
                "careers page. If the URL contains "
                "\"boards.greenhouse.io/X\", \"jobs.lever.co/X\", or "
                "\"jobs.ashbyhq.com/X\", that X is the slug."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left", wraplength=720,
        ).pack(anchor="w", pady=(8, 0))

    def _apply_starter_pack(self) -> None:
        """Load the curated starter slugs into all three ATS
        StringVars and auto-enable the matching platform cards.

        Idempotent — adds slugs without removing whatever the user
        already typed, dedups case-insensitively, refreshes the
        text widgets to reflect the new list, and reveals the now-
        enabled extras.
        """
        added_total = 0
        for ats_id, slugs in STARTER_PACK_SLUGS.items():
            var_name = f"ats_{ats_id}_slugs"
            var = self.wizard.data.get(var_name)
            if var is None:
                continue
            existing_text = var.get()
            existing_set = {
                line.strip().lower() for line in existing_text.splitlines()
                if line.strip()
            }
            new_lines = list(existing_text.splitlines()) if existing_text else []
            for slug in slugs:
                if slug.lower() in existing_set:
                    continue
                new_lines.append(slug)
                existing_set.add(slug.lower())
                added_total += 1
            joined = "\n".join(line for line in new_lines if line.strip())
            var.set(joined)

            # Sync the text widget (the StringVar isn't bidirectional
            # with Text widgets — we built our own one-way sync).
            text_widget = getattr(self, f"_ats_text_{ats_id}", None)
            if text_widget is not None:
                try:
                    text_widget.delete("1.0", "end")
                    text_widget.insert("1.0", joined)
                except tk.TclError:
                    pass

            # Auto-enable the corresponding platform card.
            enabled_var = self.wizard.data.get(f"ats_{ats_id}_enabled")
            if enabled_var is not None and not enabled_var.get():
                enabled_var.set(True)
                # Reveal the extras frame the same way a manual
                # toggle would.
                self._on_toggle(f"ats_{ats_id}")

        if hasattr(self, "_url_status") and self._url_status is not None:
            try:
                self._url_status.configure(
                    text=(
                        f"✓ Loaded the starter pack — "
                        f"{added_total} new board(s) added across "
                        f"Greenhouse, Lever, and Ashby. Click a card "
                        f"below to see / edit each list."
                    ),
                )
            except tk.TclError:
                pass

    def _add_slug_from_url(self) -> None:
        """Parse the URL in the entry box and add its slug to the
        right ATS list (auto-enabling the platform if needed).

        Recognized URLs: Greenhouse / Lever / Ashby boards. Any
        other URL gets a friendly "we don't recognize that site"
        message — no crash.
        """
        if not hasattr(self, "_url_entry") or not hasattr(self, "_url_status"):
            return
        try:
            raw_url = self._url_entry.get()
        except tk.TclError:
            return
        url = (raw_url or "").strip()
        if not url:
            self._url_status.configure(
                text="(paste a URL like https://boards.greenhouse.io/stripe)",
            )
            return

        detected = detect_ats_from_url(url)
        if detected is None:
            self._url_status.configure(
                text=(
                    "✗ We don't recognize that URL. It needs to be a "
                    "Greenhouse, Lever, or Ashby board (host like "
                    "boards.greenhouse.io, jobs.lever.co, or "
                    "jobs.ashbyhq.com). For other companies, you'll "
                    "have to apply manually for now."
                ),
            )
            return

        ats_id, slug = detected
        var_name = f"ats_{ats_id}_slugs"
        var = self.wizard.data.get(var_name)
        if var is None:
            self._url_status.configure(
                text=f"(internal error: no var for {ats_id})",
            )
            return

        existing_text = var.get()
        existing_set = {
            line.strip().lower() for line in existing_text.splitlines()
            if line.strip()
        }
        if slug.lower() in existing_set:
            self._url_status.configure(
                text=(
                    f"⚠ '{slug}' is already in your "
                    f"{ats_id.title()} list — no change."
                ),
            )
            # Still auto-enable the ATS card if it's off — user
            # might have added the slug manually but forgotten to
            # tick the platform.
            self._auto_enable_ats(ats_id)
            return

        new_lines = list(existing_text.splitlines()) if existing_text else []
        new_lines.append(slug)
        joined = "\n".join(line for line in new_lines if line.strip())
        var.set(joined)

        # Sync the text widget so the user sees their slug appear
        # in the ATS card below.
        text_widget = getattr(self, f"_ats_text_{ats_id}", None)
        if text_widget is not None:
            try:
                text_widget.delete("1.0", "end")
                text_widget.insert("1.0", joined)
            except tk.TclError:
                pass

        self._auto_enable_ats(ats_id)

        # Clear the entry so the user can paste another URL.
        try:
            self._url_entry.delete(0, "end")
        except tk.TclError:
            pass
        self._url_status.configure(
            text=(
                f"✓ Added '{slug}' to your {ats_id.title()} list. "
                f"Add more URLs above, or scroll down to see the "
                f"{ats_id.title()} card."
            ),
        )

    def _auto_enable_ats(self, ats_id: str) -> None:
        """If the ATS platform isn't enabled yet, enable it AND
        reveal its slug-list extras frame. Idempotent."""
        enabled_var = self.wizard.data.get(f"ats_{ats_id}_enabled")
        if enabled_var is not None and not enabled_var.get():
            enabled_var.set(True)
            self._on_toggle(f"ats_{ats_id}")

    def _build_ats_extra(
        self, parent: tk.Widget, platform_key: str, ats_id: str,
    ) -> tk.Widget:
        """Multi-line slug entry + Test button for one ATS card.

        The Text widget is bound to ``ats_<ats_id>_slugs`` in
        ``wizard.data`` (a StringVar holding newline-separated
        slugs — Tk's Text widget can't bind to a variable directly,
        so we sync via callbacks at save time).
        """
        frame = tk.Frame(parent, bg=BG_CARD)

        tk.Label(
            frame, text="Company slugs (one per line):",
            font=FONT_SMALL, fg=TEXT, bg=BG_CARD,
        ).pack(anchor="w")

        # Text widget — 4 visible lines so the user has room to
        # paste a list without scrolling, but doesn't dominate the
        # wizard if they only have one or two slugs.
        text_widget = tk.Text(
            frame, height=4, width=40,
            font=("Consolas", 10),
            bg="#FFFFFF", fg=TEXT,
            highlightbackground=BORDER, highlightthickness=1, bd=0,
            wrap="none",
        )
        text_widget.pack(fill="x", pady=(2, 4))
        # Pre-populate from saved StringVar.
        saved = self.wizard.data.get(f"ats_{ats_id}_slugs")
        if saved is not None:
            text_widget.insert("1.0", saved.get())
        # Persist back on every keystroke so get_config() reads
        # current state without us needing a separate "Save" button.
        text_widget.bind(
            "<KeyRelease>",
            lambda _e, w=text_widget, a=ats_id: self._sync_ats_slugs(w, a),
        )
        # Stash a reference for tests / programmatic access.
        setattr(self, f"_ats_text_{ats_id}", text_widget)

        # Test-slugs button
        btn_row = tk.Frame(frame, bg=BG_CARD)
        btn_row.pack(fill="x")
        ttk.Button(
            btn_row, text="Test slugs",
            command=lambda a=ats_id, w=text_widget: self._test_ats_slugs(a, w),
        ).pack(side="left")
        # Status line — updated by the test handler with one ✓/✗ per slug.
        status = tk.Label(
            btn_row, text="", font=FONT_SMALL,
            fg=TEXT_LIGHT, bg=BG_CARD, anchor="w", justify="left",
            wraplength=520,
        )
        status.pack(side="left", padx=(8, 0))
        self._ats_status[ats_id] = status

        return frame

    def _sync_ats_slugs(self, widget: tk.Text, ats_id: str) -> None:
        """Mirror the Text widget contents into the wizard StringVar."""
        var = self.wizard.data.get(f"ats_{ats_id}_slugs")
        if var is None:
            return
        try:
            content = widget.get("1.0", "end-1c")
        except tk.TclError:
            return
        var.set(content)

    def _test_ats_slugs(self, ats_id: str, widget: tk.Text) -> None:
        """Probe each non-empty slug against the ATS endpoint.

        Runs in a background thread so the UI stays responsive.
        Reports per-slug ✓/✗ inline via the status label. Doesn't
        modify the slug list — purely diagnostic, the user fixes
        typos themselves based on the feedback.
        """
        try:
            content = widget.get("1.0", "end-1c")
        except tk.TclError:
            return
        slugs = [s.strip() for s in content.splitlines() if s.strip()]
        status = self._ats_status.get(ats_id)
        if status is None:
            return
        if not slugs:
            status.configure(text="(no slugs to test)")
            return
        status.configure(text="Testing...")

        def worker() -> None:
            results = []
            try:
                import httpx
                # Dedicated client per probe — safer than reusing
                # the wizard's other clients (we may not have any).
                with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                    for slug in slugs:
                        url = self._ats_probe_url(ats_id, slug)
                        try:
                            resp = client.get(
                                url,
                                headers={
                                    "User-Agent": "AutoApplier/2 wizard probe",
                                    "Accept": "application/json",
                                },
                            )
                        except Exception as exc:
                            results.append((slug, f"net error: {exc}"))
                            continue
                        if resp.status_code == 200:
                            count = self._count_jobs_in_response(ats_id, resp)
                            results.append((slug, f"✓ {count} jobs"))
                        elif resp.status_code == 404:
                            results.append((slug, "✗ not found"))
                        else:
                            results.append(
                                (slug, f"✗ HTTP {resp.status_code}")
                            )
            except Exception as exc:
                # Catch-all so the worker never crashes the GUI.
                logger.warning("ATS slug test worker raised: %s", exc)
                results = [(s, "error") for s in slugs]
            # Marshal back to UI thread. Wrap in TclError guard —
            # user may have closed the wizard while we were probing.
            def render() -> None:
                lines = [f"  {slug}: {msg}" for slug, msg in results]
                try:
                    status.configure(text="\n".join(lines))
                except tk.TclError:
                    pass
            try:
                self.after(0, render)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _ats_probe_url(ats_id: str, slug: str) -> str:
        """Build the JSON-API probe URL for a given ATS + slug."""
        if ats_id == "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        if ats_id == "lever":
            return f"https://api.lever.co/v0/postings/{slug}?mode=json"
        if ats_id == "ashby":
            return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        return ""

    @staticmethod
    def _count_jobs_in_response(ats_id: str, response) -> int:
        """Best-effort job count from the ATS response. Different
        ATSes wrap their list differently; we try the common shapes
        and return ``-1`` to signal "couldn't parse" rather than
        misreporting zero.
        """
        try:
            data = response.json()
        except Exception:
            return -1
        if ats_id == "lever":
            # Lever returns a top-level list.
            return len(data) if isinstance(data, list) else -1
        # Greenhouse + Ashby wrap as {"jobs": [...]}.
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if isinstance(jobs, list):
                return len(jobs)
        return -1

    def _build_zr_ack_extra(self, parent: tk.Widget) -> tk.Widget:
        """ZipRecruiter manual-verification ack checkbox.

        ZR's silent-rejection failure mode (CSV says ``applied`` while
        ZR's dashboard shows ``Application Incomplete``) requires the
        user to fill in their profile on ziprecruiter.com itself —
        we can't do that from the bot side. Doctor flags this as a
        WARN every run, which is informational but bloats the log.

        This checkbox lets users acknowledge they've verified their
        remote ZR profile is populated; toggling it on writes
        ``ziprecruiter_profile_verified: true`` to user_config.json
        and the doctor check downgrades to PASS.

        We never *imply* verification — toggling the box is purely
        the user's claim about a state we can't observe.
        """
        frame = tk.Frame(parent, bg=BG_CARD)
        ttk.Checkbutton(
            frame,
            text="I've verified my ZipRecruiter profile is filled in",
            variable=self.wizard.data["ziprecruiter_profile_verified"],
            style="Card.TCheckbutton",
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=(
                "Tick this once you've checked ziprecruiter.com -> "
                "Account -> Profile and confirmed your name, phone, "
                "address, and resume are saved on ZR's site. Silences "
                "the recurring 'verify ZR profile' warning in the "
                "preflight check."
            ),
            font=FONT_SMALL, fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left", wraplength=700,
        ).pack(anchor="w", padx=(24, 0))
        return frame

    def _build_nodriver_extra(self, parent: tk.Widget) -> tk.Widget:
        """Install-helper sub-card for the Nodriver LinkedIn engine.

        Detects whether ``nodriver`` is importable; if not, surfaces
        an "Install now" button that runs ``pip install nodriver``
        in a background thread with a short status line.
        """
        frame = tk.Frame(parent, bg=BG_CARD)

        from auto_applier.browser.nodriver_session import (
            is_nodriver_available,
        )

        installed = is_nodriver_available()
        status_text = (
            "✓ Nodriver is installed."
            if installed
            else "Nodriver is not installed yet — click below to install."
        )
        status = tk.Label(
            frame, text=status_text, font=FONT_SMALL,
            fg=TEXT_LIGHT, bg=BG_CARD,
            anchor="w", justify="left",
        )
        status.pack(anchor="w")
        self._nodriver_status = status

        if not installed:
            btn_row = tk.Frame(frame, bg=BG_CARD)
            btn_row.pack(fill="x", pady=(4, 0))
            install_btn = ttk.Button(
                btn_row, text="Install Nodriver",
                command=self._install_nodriver,
            )
            install_btn.pack(side="left")
            self._nodriver_install_btn = install_btn

        return frame

    def _install_nodriver(self) -> None:
        """Run ``pip install nodriver`` in a background thread.

        Uses the current Python interpreter (sys.executable) so the
        install always lands in the same environment the wizard is
        running in. Friends typically launch via the installer's
        AutoApplier.exe — for the PyInstaller-bundled case, sys.
        executable is the bundle, and pip won't be available.
        We detect that and surface a friendly message pointing to
        a manual install path instead.
        """
        if self._nodriver_install_btn is not None:
            self._nodriver_install_btn.configure(state="disabled")
        if self._nodriver_status is not None:
            self._nodriver_status.configure(text="Installing nodriver — this can take 30-60 seconds...")

        def worker() -> None:
            ok, msg = _pip_install_nodriver()
            def render() -> None:
                if self._nodriver_status is None:
                    return
                try:
                    if ok:
                        self._nodriver_status.configure(
                            text="✓ Nodriver installed. Restart Auto Applier to use it."
                        )
                        if self._nodriver_install_btn is not None:
                            self._nodriver_install_btn.configure(state="disabled")
                    else:
                        self._nodriver_status.configure(text=f"✗ {msg}")
                        if self._nodriver_install_btn is not None:
                            self._nodriver_install_btn.configure(state="normal")
                except tk.TclError:
                    pass
            try:
                self.after(0, render)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Toggle handler
    # ------------------------------------------------------------------

    def _on_toggle(self, key: str) -> None:
        """Show / hide the per-platform extra frame on toggle."""
        extra = self._extras.get(key)
        if extra is None:
            return
        var = self.wizard.data[f"{key}_enabled"]
        if var.get():
            extra.pack(fill="x", padx=(24, 0), pady=(8, 0))
        else:
            try:
                extra.pack_forget()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> bool:
        """At least one platform must be selected, and the two
        LinkedIn engines are mutually exclusive."""
        any_enabled = False
        for key, _, _, _ in PLATFORMS:
            if self.wizard.data[f"{key}_enabled"].get():
                any_enabled = True
                break
        if not any_enabled:
            messagebox.showwarning(
                "No Platforms Selected",
                "Please select at least one job platform.",
                parent=self.wizard,
            )
            return False

        # Mutual-exclusion check for the two LinkedIn engines.
        if (
            self.wizard.data["linkedin_enabled"].get()
            and self.wizard.data["linkedin_nodriver_enabled"].get()
        ):
            messagebox.showwarning(
                "Pick One LinkedIn Engine",
                "Only one of 'LinkedIn' and 'LinkedIn — experimental "
                "engine (Nodriver)' can run at a time. Uncheck whichever "
                "you don't want before continuing.",
                parent=self.wizard,
            )
            return False

        return True


# ----------------------------------------------------------------------
# Module-level helper (extracted for testability)
# ----------------------------------------------------------------------

def _pip_install_nodriver() -> tuple[bool, str]:
    """Run ``pip install nodriver`` against the current interpreter.

    Returns ``(ok, message)``. Module-level so tests can patch
    ``subprocess.run`` without instantiating Tk.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "nodriver"],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        return (
            False,
            "Couldn't find pip. If you're using the bundled installer, "
            "see the project README for manual nodriver install steps.",
        )
    except subprocess.TimeoutExpired:
        return False, "Install timed out (>3 min). Check your network."
    except Exception as exc:
        return False, f"Install raised: {exc}"

    if result.returncode != 0:
        # Truncate pip's stderr to a single user-readable line.
        stderr = (result.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else "unknown pip error"
        return False, f"Install failed: {last[:160]}"
    return True, "ok"
