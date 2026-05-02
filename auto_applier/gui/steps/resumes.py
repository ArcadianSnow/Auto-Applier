"""Step 3: Multi-resume manager."""
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path

from auto_applier.gui.styles import (
    BG, BG_CARD, PRIMARY, ACCENT, DANGER, TEXT, TEXT_LIGHT, BORDER,
    FONT_HEADING, FONT_SUBHEADING, FONT_BODY, FONT_SMALL,
    PAD_X, PAD_Y,
)


class ResumesStep(ttk.Frame):
    """Multi-resume management step."""

    def __init__(self, parent: tk.Widget, wizard) -> None:
        super().__init__(parent, style="TFrame")
        self.wizard = wizard
        self._build()

    def _build(self) -> None:
        # Heading
        ttk.Label(
            self, text="Your Resumes", style="Heading.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(PAD_Y, 4))

        ttk.Label(
            self,
            text="Add one or more resumes. The AI will pick the best one for each job.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Main card
        card = tk.Frame(
            self, bg=BG_CARD, highlightbackground=BORDER,
            highlightthickness=1, padx=16, pady=16,
        )
        card.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 8))

        # Treeview with status column. The status icon is the at-a-glance
        # signal the user asked for: ✓ when the resume is on disk and
        # readable, ✗ when something went wrong. ✗ rows can be retried
        # via the Reprocess button instead of remove-and-re-add.
        list_frame = tk.Frame(card, bg=BG_CARD)
        list_frame.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.tree = ttk.Treeview(
            list_frame,
            columns=("status", "label", "filename"),
            show="headings",
            height=10,
            yscrollcommand=scrollbar.set,
        )
        self.tree.heading("status", text="Status")
        self.tree.heading("label", text="Label")
        self.tree.heading("filename", text="File")
        self.tree.column("status", width=80, anchor="center", stretch=False)
        self.tree.column("label", width=200, anchor="w", stretch=False)
        self.tree.column("filename", width=380, anchor="w", stretch=True)
        # Color-code by status. tag-based styling.
        self.tree.tag_configure("ok", foreground=ACCENT)
        self.tree.tag_configure("bad", foreground=DANGER)

        scrollbar.configure(command=self.tree.yview)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Button row
        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(12, 0))

        ttk.Button(
            btn_row, text="Add Resume", style="Primary.TButton",
            command=self._add_resume,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Reprocess Selected",
            command=self._reprocess_resume,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Remove Selected", style="Danger.TButton",
            command=self._remove_resume,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row, text="Open Resumes Folder",
            command=self._open_resumes_folder,
        ).pack(side="right")

        # Note + writes-to caption (helps users / friends spot
        # PROJECT_ROOT mismatches when something looks wrong).
        ttk.Label(
            self,
            text="Minimum 1 resume required. Supported formats: PDF, DOCX.",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(8, 0))

        from auto_applier.config import RESUMES_DIR
        ttk.Label(
            self,
            text=f"Files saved to: {RESUMES_DIR}",
            style="Small.TLabel",
        ).pack(anchor="w", padx=PAD_X, pady=(0, 4))

        # Persistent status line — every Add / Reprocess / Remove writes
        # here so the user always sees what happened, even if a modal
        # popup gets dismissed too quickly. Stays visible until the
        # next action overrides it.
        self._status_var = tk.StringVar(value="")
        self._status_label = ttk.Label(
            self,
            textvariable=self._status_var,
            style="Small.TLabel",
            wraplength=720,
            justify="left",
        )
        self._status_label.pack(anchor="w", padx=PAD_X, pady=(0, PAD_Y))

        # Populate from saved state
        self._refresh_list()

    def _set_status(self, message: str, ok: bool = True) -> None:
        """Update the persistent status line below the table.

        Called by every action (add / reprocess / remove / open folder)
        so a user always has on-screen confirmation of what just
        happened — modal popups can be dismissed too fast to read,
        and pythonw under run.bat eats stderr, so a label is the
        only reliable feedback channel.
        """
        from auto_applier.gui.styles import ACCENT, DANGER
        # Tkinter ttk.Label foreground via style override:
        try:
            self._status_label.configure(
                foreground=ACCENT if ok else DANGER,
            )
        except Exception:
            pass
        self._status_var.set(message)
        # Force the UI to render before we move on (the worker may
        # immediately do more work that blocks the main thread).
        try:
            self.update_idletasks()
        except Exception:
            pass

    def on_show(self) -> None:
        """Refresh list when step is shown (may have changed externally)."""
        self._refresh_list()

    def _refresh_list(self) -> None:
        """Sync the treeview with wizard.resume_list + disk state.

        Each row's status comes from a fresh disk check — was the
        resume actually copied into RESUMES_DIR and is the profile
        present? That's exactly what doctor reads, so a green ✓
        in the wizard means doctor will agree.
        """
        from auto_applier.config import RESUMES_DIR, PROFILES_DIR
        # Clear existing rows
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for idx, (label, path) in enumerate(self.wizard.resume_list):
            filename = Path(path).name
            ext = Path(path).suffix
            on_disk = (RESUMES_DIR / f"{label}{ext}").exists()
            profile_present = (PROFILES_DIR / f"{label}.json").exists()
            if on_disk and profile_present:
                status = "✓ Ready"
                tag = "ok"
            elif on_disk:
                status = "✓ File / no profile"
                tag = "ok"
            else:
                status = "✗ Not saved"
                tag = "bad"
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(status, label, filename),
                tags=(tag,),
            )

    def _selected_index(self) -> int | None:
        """Return the index of the highlighted treeview row, or None."""
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _add_resume(self) -> None:
        """Open file dialog, prompt for label, add to list."""
        path = filedialog.askopenfilename(
            title="Select Resume",
            filetypes=[
                ("Resume files", "*.pdf *.docx"),
                ("PDF files", "*.pdf"),
                ("Word documents", "*.docx"),
                ("All files", "*.*"),
            ],
            parent=self.wizard,
        )
        if not path:
            return

        # Suggest a label from the filename
        stem = Path(path).stem.replace("_", " ").replace("-", " ").title()
        label = simpledialog.askstring(
            "Resume Label",
            "Enter a label for this resume (e.g., 'Data Analyst', 'Backend Dev'):",
            initialvalue=stem,
            parent=self.wizard,
        )
        if not label:
            return

        # Normalize label for use as a key
        label_key = label.strip()
        if not label_key:
            return

        # Check for duplicate labels
        existing_labels = [lbl for lbl, _ in self.wizard.resume_list]
        if label_key in existing_labels:
            messagebox.showwarning(
                "Duplicate Label",
                f"A resume with the label '{label_key}' already exists.\n"
                "Please use a different label.",
                parent=self.wizard,
            )
            return

        self.wizard.resume_list.append((label_key, path))
        self._refresh_list()

        # Eagerly copy the file into data/resumes/ + write a minimal
        # profile JSON so `cli doctor` sees the resume as loaded
        # immediately. The dashboard's add_resume() call later does
        # the LLM-powered skill extraction (idempotent — the manager
        # detects the file's already in place and skips the copy).
        self._set_status(f"Saving '{label_key}'...", ok=True)
        try:
            ok = self._materialize_resume(path, label_key, verbose=True)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self._set_status(
                f"Save crashed for '{label_key}': {exc}", ok=False,
            )
            messagebox.showerror(
                "Save crashed",
                (
                    f"Resume: {label_key}\n"
                    f"Path: {path}\n\n"
                    f"Exception: {exc}\n\n"
                    f"Full traceback:\n{tb}"
                ),
                parent=self.wizard,
            )
            self._refresh_list()
            return
        self._refresh_list()
        if ok:
            self._set_status(
                f"'{label_key}' is saved and ready.", ok=True,
            )
        else:
            self._set_status(
                f"'{label_key}' couldn't be fully saved — see the popup.",
                ok=False,
            )

    def _materialize_resume(
        self,
        source_path: str,
        label: str,
        verbose: bool = True,
    ) -> bool:
        """Copy the user's resume file into data/resumes/ and write a
        minimal profile JSON. Returns True on full success, False if
        anything went wrong.

        Failures are now SURFACED to the user via messagebox (when
        ``verbose`` is True) instead of silently swallowed. A
        previous version's silent ``except: pass`` produced the
        confusing "I added a resume but the wizard says it's not
        loaded" UX — the user had no breadcrumb to follow.
        """
        import shutil
        import json
        from datetime import datetime, timezone
        from auto_applier.config import RESUMES_DIR, PROFILES_DIR
        from auto_applier.resume.parser import extract_text

        source = Path(source_path).resolve()
        if not source.exists():
            if verbose:
                messagebox.showerror(
                    "Resume file not found",
                    (
                        f"Couldn't find:\n  {source}\n\n"
                        "Did you move or delete it after picking it? "
                        "Click 'Add Resume' again and re-select."
                    ),
                    parent=self.wizard,
                )
            return False

        # Sanitize the label so it produces a valid Windows filename.
        # Earlier versions used the label verbatim; a label containing
        # / \ : * ? " < > | makes shutil.copy2 throw OSError 22 on
        # Windows and the silent except swallowed it.
        safe_label = "".join(
            c if c.isalnum() or c in "-_ " else "_"
            for c in label
        ).strip() or "resume"

        try:
            RESUMES_DIR.mkdir(parents=True, exist_ok=True)
            PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if verbose:
                messagebox.showerror(
                    "Couldn't create resumes folder",
                    (
                        f"Auto Applier couldn't create:\n  {RESUMES_DIR}\n\n"
                        f"Error: {exc}\n\n"
                        "Most likely cause: the project folder is on a "
                        "read-only drive, or your antivirus is blocking "
                        "writes. Try running setup.bat from a folder "
                        "outside of Downloads or OneDrive."
                    ),
                    parent=self.wizard,
                )
            return False

        dest = RESUMES_DIR / f"{safe_label}{source.suffix}"
        # Copy if dest is missing OR dest points to a different file.
        copy_needed = True
        if dest.exists():
            try:
                copy_needed = dest.resolve() != source
            except OSError:
                copy_needed = True
        if copy_needed:
            try:
                shutil.copy2(source, dest)
            except shutil.SameFileError:
                pass  # already at dest — fine
            except OSError as exc:
                if verbose:
                    messagebox.showerror(
                        "Couldn't copy resume file",
                        (
                            f"Source: {source}\n"
                            f"Destination: {dest}\n\n"
                            f"Error: {exc}\n\n"
                            "Most likely cause: Windows Defender or "
                            "OneDrive has the source file locked, or "
                            "the destination is read-only. Move the "
                            "source resume out of Downloads first, or "
                            "exclude this folder in Defender."
                        ),
                        parent=self.wizard,
                    )
                return False

        profile_path = PROFILES_DIR / f"{safe_label}.json"
        if profile_path.exists():
            return True  # already parsed; nothing more to do

        try:
            raw_text = extract_text(str(dest))
        except Exception as exc:
            # Profile gets written with empty raw_text — better than
            # nothing; dashboard's later add_resume will retry the parse.
            raw_text = ""
            if verbose:
                messagebox.showwarning(
                    "Resume couldn't be parsed",
                    (
                        f"File: {dest}\n\n"
                        f"Error: {exc}\n\n"
                        "The file was saved, but the AI couldn't read "
                        "the text from it. Common causes: password-"
                        "protected PDFs, scanned-image PDFs (no OCR), "
                        "corrupted DOCX. Try saving the resume as plain "
                        "PDF/DOCX from your editor and re-adding."
                    ),
                    parent=self.wizard,
                )

        try:
            profile = {
                "label": safe_label,
                "source_file": dest.name,
                "parsed_at": datetime.now(timezone.utc).isoformat(),
                "raw_text": raw_text,
                "summary": "",
                "skills": [],
                "confirmed_skills": [],
            }
            profile_path.write_text(
                json.dumps(profile, indent=2), encoding="utf-8",
            )
        except OSError as exc:
            if verbose:
                messagebox.showerror(
                    "Couldn't save profile",
                    (
                        f"Path: {profile_path}\n\n"
                        f"Error: {exc}"
                    ),
                    parent=self.wizard,
                )
            return False

        return True

    def _reprocess_resume(self) -> None:
        """Retry materialize on the highlighted row.

        Surrounded by a top-level try/except so an unhandled exception
        (like the time my code imported a function that didn't exist)
        produces a visible error in the status line + a popup, instead
        of silently doing nothing because tk + pythonw swallowed it.
        """
        idx = self._selected_index()
        if idx is None:
            self._set_status(
                "Click a resume row first, then 'Reprocess Selected'.",
                ok=False,
            )
            return
        try:
            label, path = self.wizard.resume_list[idx]
        except IndexError:
            self._set_status(
                "Selection lost — try clicking the row again.", ok=False,
            )
            return

        self._set_status(f"Reprocessing '{label}'...", ok=True)
        try:
            ok = self._materialize_resume(path, label, verbose=True)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self._set_status(
                f"Reprocess crashed for '{label}': {exc}", ok=False,
            )
            messagebox.showerror(
                "Reprocess crashed",
                (
                    f"Resume: {label}\n"
                    f"Path: {path}\n\n"
                    f"Exception: {exc}\n\n"
                    f"Full traceback:\n{tb}\n\n"
                    "Please send this to whoever maintains the app."
                ),
                parent=self.wizard,
            )
            self._refresh_list()
            return

        self._refresh_list()
        if ok:
            self._set_status(
                f"'{label}' is saved and ready.", ok=True,
            )
        else:
            # Specific failure already showed a messagebox from inside
            # _materialize_resume; status line just summarizes.
            self._set_status(
                f"Reprocess of '{label}' didn't complete — see the popup.",
                ok=False,
            )

    def _open_resumes_folder(self) -> None:
        """Open the resumes folder in Windows Explorer.

        Useful when the user wants to verify what's actually on disk
        — and useful for diagnosing PROJECT_ROOT mismatches between
        the wizard process and the doctor process.
        """
        from auto_applier.config import RESUMES_DIR
        try:
            RESUMES_DIR.mkdir(parents=True, exist_ok=True)
            import os, sys, subprocess
            if sys.platform == "win32":
                os.startfile(str(RESUMES_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(RESUMES_DIR)], check=False)
            else:
                subprocess.run(["xdg-open", str(RESUMES_DIR)], check=False)
        except Exception as exc:
            messagebox.showerror(
                "Couldn't open folder",
                (
                    f"Path: {RESUMES_DIR}\n\n"
                    f"Error: {exc}"
                ),
                parent=self.wizard,
            )

    def _remove_resume(self) -> None:
        """Remove the selected resume from the list."""
        index = self._selected_index()
        if index is None:
            messagebox.showinfo(
                "No Selection",
                "Click a resume in the list first, then 'Remove Selected'.",
                parent=self.wizard,
            )
            return
        try:
            label, _ = self.wizard.resume_list[index]
        except IndexError:
            return
        confirm = messagebox.askyesno(
            "Remove Resume",
            f"Remove '{label}' from the list?",
            parent=self.wizard,
        )
        if confirm:
            self.wizard.resume_list.pop(index)
            self._refresh_list()

    def validate(self) -> bool:
        """At least one resume is required.

        Persists the resume list on advance so users who add resumes
        and then close the wizard (e.g. to verify with `cli doctor`)
        don't lose what they uploaded.
        """
        if not self.wizard.resume_list:
            messagebox.showwarning(
                "No Resumes",
                "Please add at least one resume before continuing.",
                parent=self.wizard,
            )
            return False
        try:
            self.wizard.save_resumes_only()
        except Exception:
            pass  # Final Ready step will surface a writable error.
        return True
