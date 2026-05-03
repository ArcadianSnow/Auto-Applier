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
        "Greenhouse boards (no browser, JSON API)  ⚡",
        "Pulls jobs directly from companies hosted on Greenhouse "
        "(boards.greenhouse.io). No browser, no anti-bot, no rate "
        "limit. Add the company slugs below — find a slug by "
        "visiting a posting URL like 'boards.greenhouse.io/STRIPE/...' "
        "where STRIPE is the slug.",
        {"ats": True, "ats_id": "greenhouse"},
    ),
    (
        "ats_lever",
        "Lever boards (no browser, JSON API)  ⚡",
        "Pulls jobs from companies hosted on Lever (jobs.lever.co). "
        "Same fast, anti-bot-free flow as Greenhouse. Slug is the "
        "second URL segment, e.g. 'jobs.lever.co/NETFLIX/...' "
        "where NETFLIX is the slug.",
        {"ats": True, "ats_id": "lever"},
    ),
    (
        "ats_ashby",
        "Ashby boards (no browser, JSON API)  ⚡",
        "Pulls jobs from companies hosted on Ashby (jobs.ashbyhq.com). "
        "Slug is the path segment, e.g. 'jobs.ashbyhq.com/OPENAI' "
        "where OPENAI is the slug.",
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

            # Per-platform extras — only ATS or Nodriver cards have
            # them. Hidden by default; revealed when the checkbox
            # is toggled on.
            if options.get("ats"):
                extra = self._build_ats_extra(card, key, options["ats_id"])
                self._extras[key] = extra
            elif options.get("nodriver"):
                extra = self._build_nodriver_extra(card)
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
