"""Main wizard window and step controller."""

import tkinter as tk
from tkinter import ttk

from auto_applier.gui.styles import apply_styles
from auto_applier.gui.steps.welcome import WelcomeStep
from auto_applier.gui.steps.credentials import CredentialsStep
from auto_applier.gui.steps.resume import ResumeStep
from auto_applier.gui.steps.personal import PersonalInfoStep
from auto_applier.gui.steps.preferences import PreferencesStep
from auto_applier.gui.steps.ready import ReadyStep

STEPS = [
    ("Welcome", WelcomeStep),
    ("Credentials", CredentialsStep),
    ("Resume", ResumeStep),
    ("Personal Info", PersonalInfoStep),
    ("Job Prefs", PreferencesStep),
    ("Ready", ReadyStep),
]


class WizardApp:
    """Multi-step setup wizard for Auto Applier."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Auto Applier Setup")
        self.root.geometry("700x580")
        self.root.resizable(False, False)
        self.root.configure(bg="#F5F7FA")

        apply_styles(self.root)

        # Shared state across all steps
        self.data: dict[str, tk.Variable] = {
            "email": tk.StringVar(),
            "password": tk.StringVar(),
            "resume_path": tk.StringVar(),
            "first_name": tk.StringVar(),
            "last_name": tk.StringVar(),
            "phone": tk.StringVar(),
            "city": tk.StringVar(),
            "linkedin": tk.StringVar(),
            "website": tk.StringVar(),
            "keywords": tk.StringVar(),
            "location": tk.StringVar(),
        }

        self.current_step = 0

        # Layout: header, content, footer
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_content()
        self._build_footer()

        self._show_step(0)
        self.root.bind("<Return>", lambda e: self._on_next())

    # ── Header with step dots ────────────────────────────────────

    def _build_header(self) -> None:
        self.header = tk.Frame(self.root, bg="#FFFFFF", height=90)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_propagate(False)

        self.step_label = tk.Label(
            self.header,
            text="",
            font=("Segoe UI", 10),
            fg="#64748B",
            bg="#FFFFFF",
        )
        self.step_label.pack(pady=(12, 4))

        # Dot row
        self.dot_frame = tk.Frame(self.header, bg="#FFFFFF")
        self.dot_frame.pack(pady=(0, 8))

        self.dots: list[tk.Canvas] = []
        self.dot_labels: list[tk.Label] = []
        self.lines: list[tk.Canvas] = []

        for i, (name, _) in enumerate(STEPS):
            if i > 0:
                line = tk.Canvas(self.dot_frame, width=50, height=4, bg="#FFFFFF", highlightthickness=0)
                line.create_rectangle(0, 1, 50, 3, fill="#CBD5E1", outline="", tags="line")
                line.grid(row=0, column=i * 2 - 1, padx=0, pady=(0, 14))
                self.lines.append(line)

            dot = tk.Canvas(self.dot_frame, width=24, height=24, bg="#FFFFFF", highlightthickness=0)
            dot.grid(row=0, column=i * 2, padx=2, pady=(0, 14))
            self.dots.append(dot)

            lbl = tk.Label(
                self.dot_frame, text=name, font=("Segoe UI", 7),
                fg="#94A3B8", bg="#FFFFFF",
            )
            lbl.grid(row=1, column=i * 2, padx=0)
            self.dot_labels.append(lbl)

        # Divider line
        tk.Frame(self.header, bg="#E2E8F0", height=1).pack(fill="x", side="bottom")

    def _update_dots(self) -> None:
        for i, dot in enumerate(self.dots):
            dot.delete("all")
            if i < self.current_step:
                # Completed
                dot.create_oval(2, 2, 22, 22, fill="#10B981", outline="")
                dot.create_text(12, 12, text="✓", fill="white", font=("Segoe UI", 9, "bold"))
                self.dot_labels[i].configure(fg="#10B981")
            elif i == self.current_step:
                # Active
                dot.create_oval(2, 2, 22, 22, fill="#2563EB", outline="")
                dot.create_text(12, 12, text=str(i + 1), fill="white", font=("Segoe UI", 9, "bold"))
                self.dot_labels[i].configure(fg="#2563EB")
            else:
                # Upcoming
                dot.create_oval(2, 2, 22, 22, fill="#CBD5E1", outline="")
                dot.create_text(12, 12, text=str(i + 1), fill="#94A3B8", font=("Segoe UI", 9))
                self.dot_labels[i].configure(fg="#94A3B8")

        for i, line in enumerate(self.lines):
            line.delete("line")
            color = "#10B981" if i < self.current_step else "#CBD5E1"
            line.create_rectangle(0, 1, 50, 3, fill=color, outline="", tags="line")

        name = STEPS[self.current_step][0]
        self.step_label.configure(text=f"Step {self.current_step + 1} of {len(STEPS)} — {name}")

    # ── Content area ─────────────────────────────────────────────

    def _build_content(self) -> None:
        self.content = tk.Frame(self.root, bg="#F5F7FA")
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.step_frames: list = []
        for i, (_, StepClass) in enumerate(STEPS):
            frame = StepClass(self.content, self)
            frame.grid(row=0, column=0, sticky="nsew")
            self.step_frames.append(frame)

    # ── Footer with Back / Next ──────────────────────────────────

    def _build_footer(self) -> None:
        tk.Frame(self.root, bg="#E2E8F0", height=1).grid(row=2, column=0, sticky="ew")

        self.footer = tk.Frame(self.root, bg="#FFFFFF", height=60)
        self.footer.grid(row=3, column=0, sticky="ew")
        self.footer.grid_propagate(False)
        self.footer.grid_columnconfigure(1, weight=1)

        self.back_btn = ttk.Button(
            self.footer, text="← Back", style="Ghost.TButton", command=self._on_back,
        )
        self.back_btn.grid(row=0, column=0, padx=32, pady=14, sticky="w")

        self.next_btn = ttk.Button(
            self.footer, text="Next →", style="Primary.TButton", command=self._on_next,
        )
        self.next_btn.grid(row=0, column=2, padx=32, pady=14, sticky="e")

    # ── Navigation ───────────────────────────────────────────────

    def _show_step(self, index: int) -> None:
        self.current_step = index
        self.step_frames[index].tkraise()
        self._update_dots()

        # Hide back/next on welcome (0) and ready (last)
        if index == 0 or index == len(STEPS) - 1:
            self.back_btn.grid_remove()
            self.next_btn.grid_remove()
        else:
            self.back_btn.grid()
            self.next_btn.grid()

    def _on_next(self) -> None:
        step = self.step_frames[self.current_step]
        if hasattr(step, "validate") and not step.validate():
            return
        if self.current_step < len(STEPS) - 1:
            self._show_step(self.current_step + 1)
            # Let the new step refresh if needed
            new_step = self.step_frames[self.current_step]
            if hasattr(new_step, "on_show"):
                new_step.on_show()

    def _on_back(self) -> None:
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def go_to_step(self, index: int) -> None:
        """Jump to a specific step (used by welcome and ready screens)."""
        self._show_step(index)
        new_step = self.step_frames[index]
        if hasattr(new_step, "on_show"):
            new_step.on_show()

    def fill_dummy_data(self) -> None:
        """Populate all fields with dummy data for dry-run testing."""
        from auto_applier.config import RESUMES_DIR

        # Create a real dummy resume file on disk
        dummy_resume = RESUMES_DIR / "dummy_resume.docx"
        if not dummy_resume.exists():
            self._create_dummy_resume(dummy_resume)

        self.data["email"].set("jane.doe@example.com")
        self.data["password"].set("DummyPassword123!")
        self.data["resume_path"].set(str(dummy_resume))
        self.data["first_name"].set("Jane")
        self.data["last_name"].set("Doe")
        self.data["phone"].set("+1 (555) 123-4567")
        self.data["city"].set("San Francisco, CA")
        self.data["linkedin"].set("https://linkedin.com/in/janedoe")
        self.data["website"].set("https://janedoe.dev")
        self.data["keywords"].set("Software Engineer, Backend Developer, Python Developer")
        self.data["location"].set("Remote")

    @staticmethod
    def _create_dummy_resume(path) -> None:
        """Create a minimal dummy PDF resume for testing."""
        # Minimal valid PDF with resume-like text content
        content = (
            "Jane Doe\n"
            "Software Engineer\n"
            "jane.doe@example.com | +1 (555) 123-4567 | San Francisco, CA\n"
            "linkedin.com/in/janedoe | janedoe.dev\n\n"
            "SKILLS\n"
            "Python, JavaScript, TypeScript, React, Node.js, Django, Flask,\n"
            "PostgreSQL, MongoDB, Docker, AWS, Git, Agile, CI/CD\n\n"
            "EXPERIENCE\n"
            "Senior Software Engineer — Acme Corp (2020-Present)\n"
            "- Built scalable REST APIs serving 1M+ requests/day\n"
            "- Led migration from monolith to microservices architecture\n\n"
            "Software Engineer — StartupCo (2017-2020)\n"
            "- Developed full-stack web applications with React and Django\n"
            "- Implemented automated testing pipeline reducing bugs by 40%\n\n"
            "EDUCATION\n"
            "B.S. Computer Science — University of California, Berkeley (2017)\n"
        )
        # Write as a .docx since python-docx is already a dependency
        # and it's trivial to create a valid one
        from docx import Document
        doc = Document()
        for line in content.strip().split("\n"):
            doc.add_paragraph(line)
        # Change extension to .docx
        path = path.with_suffix(".docx")
        doc.save(str(path))

    def run(self) -> None:
        self.root.mainloop()


def launch_wizard() -> None:
    app = WizardApp()
    app.run()
