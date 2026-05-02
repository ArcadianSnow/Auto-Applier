"""Step 4: Personal information form."""
import re
import tkinter as tk
from tkinter import ttk, messagebox

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y, make_scrollable,
)


class PersonalStep(ttk.Frame):
    """Personal info form with scrollable content."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading (outside scroll area)
        ttk.Label(
            self, text="Personal Information", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="This information is used to fill out application forms.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Scrollable area
        scroll_container = ttk.Frame(self)
        scroll_container.pack(fill="both", expand=True, padx=PAD_X, pady=(0, PAD_Y))
        _canvas, inner = make_scrollable(scroll_container)

        # Card
        card = tk.Frame(
            inner, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=20, pady=16,
        )
        card.pack(fill="x", padx=4, pady=4)

        # Field definitions: (label, key, placeholder, required)
        # Address fields added so the form filler doesn't have to
        # invent (or LLM-guess) values when an apply form asks
        # for ZIP / state / country. Country defaults to "United
        # States" because that's the current audience.
        fields = [
            ("First Name", "first_name", "Jane", True),
            ("Last Name", "last_name", "Doe", True),
            ("Email", "email", "jane.doe@email.com", True),
            ("Phone", "phone", "+1 (555) 123-4567", True),
            ("Street Address", "street_address", "123 Main St", False),
            ("City", "city", "New York", True),
            ("State / Province", "state", "NY", True),
            ("ZIP / Postal Code", "zip_code", "10001", True),
            ("Country", "country", "United States", True),
            ("LinkedIn Profile URL", "linkedin_url", "https://linkedin.com/in/janedoe", False),
            ("Website / Portfolio URL", "website", "https://janedoe.dev", False),
        ]

        self._entries: dict[str, ttk.Entry] = {}

        for i, (label_text, key, placeholder, required) in enumerate(fields):
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill="x", pady=(0, 12))

            display = label_text
            if not required:
                display += "  (optional)"

            tk.Label(
                row, text=display, font=FONT_BODY,
                fg=TEXT, bg=BG_CARD, anchor="w",
            ).pack(anchor="w")

            entry = ttk.Entry(
                row, textvariable=self.wizard.data[key],
                font=FONT_BODY, width=50,
            )
            entry.pack(fill="x", pady=(4, 0))
            self._entries[key] = entry

            # Placeholder behavior
            self._setup_placeholder(entry, self.wizard.data[key], placeholder)

    def _setup_placeholder(
        self, entry: ttk.Entry, var: tk.StringVar, placeholder: str
    ) -> None:
        """Show placeholder text when field is empty and unfocused."""
        def on_focus_in(_event=None):
            if var.get() == placeholder:
                var.set("")
                entry.configure(foreground=TEXT)

        def on_focus_out(_event=None):
            if not var.get().strip():
                var.set("")
                # Don't set placeholder into the variable -- just leave empty

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    def validate(self) -> bool:
        """Require first name, last name, email + save what's filled.

        Persists personal_info to user_config.json on every advance,
        not just on the final Ready step. Otherwise users who fill
        the form but close the wizard before reaching Ready (e.g.
        to verify with `cli doctor`) would lose their input — and
        doctor would still report the address fields as missing
        because nothing made it to disk.
        """
        missing = []
        for key, label in [
            ("first_name", "First Name"),
            ("last_name", "Last Name"),
            ("email", "Email"),
            ("city", "City"),
            ("state", "State / Province"),
            ("zip_code", "ZIP / Postal Code"),
            ("country", "Country"),
        ]:
            if not self.wizard.data[key].get().strip():
                missing.append(label)

        if missing:
            messagebox.showwarning(
                "Required Fields",
                f"Please fill in: {', '.join(missing)}",
                parent=self.wizard,
            )
            return False

        email_value = self.wizard.data["email"].get().strip()
        if email_value and not _EMAIL_RE.match(email_value):
            messagebox.showwarning(
                "Invalid Email",
                "That doesn't look like a valid email — typo?",
                parent=self.wizard,
            )
            return False

        # Save the personal_info section now so a partial wizard run
        # (e.g. user clicks Next from here, then closes the window)
        # still leaves a usable user_config.json for the CLI to read.
        try:
            self.wizard.save_personal_info_only()
        except Exception:
            # Save failures shouldn't block navigation — the final
            # Ready step's save_config will surface a clear error
            # if the data folder is genuinely unwritable.
            pass
        return True
