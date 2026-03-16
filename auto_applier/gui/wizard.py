"""Main wizard window and step controller — Animal Crossing theme."""

import tkinter as tk
from tkinter import ttk

from auto_applier.gui.styles import (
    apply_styles,
    SANDY_SHORE, CREAM, DRIFTWOOD, WARM_WHITE, BORDER_LIGHT, NOOK_TAN,
    SOIL_BROWN, DRIFTWOOD_GRAY, FOGGY,
    STEP_DONE, STEP_ACTIVE, STEP_UPCOMING, NOOK_GREEN_DARK,
    HEADING_FONT, BODY_FONT,
)
from auto_applier.gui.steps.welcome import WelcomeStep
from auto_applier.gui.steps.sites import SitesStep
from auto_applier.gui.steps.resume import ResumeStep
from auto_applier.gui.steps.personal import PersonalInfoStep
from auto_applier.gui.steps.preferences import PreferencesStep
from auto_applier.gui.steps.ready import ReadyStep

STEPS = [
    ("Welcome", WelcomeStep),
    ("Platforms", SitesStep),
    ("Resume", ResumeStep),
    ("Personal Info", PersonalInfoStep),
    ("Job Prefs", PreferencesStep),
    ("Ready", ReadyStep),
]

PLATFORM_KEYS = ["linkedin", "indeed", "dice", "ziprecruiter"]


class WizardApp:
    """Multi-step setup wizard for Auto Applier."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Auto Applier — Tom Nook's Job Agency")
        self.root.geometry("700x600")
        self.root.resizable(False, False)
        self.root.configure(bg=SANDY_SHORE)

        apply_styles(self.root)

        # Shared state
        self.data: dict[str, tk.Variable] = {
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

        for key in PLATFORM_KEYS:
            self.data[f"{key}_enabled"] = tk.BooleanVar(value=(key == "linkedin"))
            self.data[f"{key}_email"] = tk.StringVar()
            self.data[f"{key}_password"] = tk.StringVar()

        self.current_step = 0

        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_content()
        self._build_footer()

        self._show_step(0)
        self.root.bind("<Return>", lambda e: self._on_next())

    # ── Header ───────────────────────────────────────────────────

    def _build_header(self) -> None:
        # Accent strip (gradient-like with three colors)
        accent = tk.Frame(self.root, bg=DRIFTWOOD, height=4)
        accent.grid(row=0, column=0, sticky="ew")
        accent.grid_propagate(False)
        # Simple three-color bar
        for i, color in enumerate(["#4CAF7D", "#7BB8D4", "#E8B84B"]):
            seg = tk.Frame(accent, bg=color, height=4)
            seg.place(relx=i/3, rely=0, relwidth=1/3, relheight=1)

        self.header = tk.Frame(self.root, bg=DRIFTWOOD, height=86)
        self.header.grid(row=1, column=0, sticky="ew")
        self.header.grid_propagate(False)

        self.step_label = tk.Label(
            self.header, text="",
            font=(BODY_FONT, 10), fg=DRIFTWOOD_GRAY, bg=DRIFTWOOD,
        )
        self.step_label.pack(pady=(10, 4))

        self.dot_frame = tk.Frame(self.header, bg=DRIFTWOOD)
        self.dot_frame.pack(pady=(0, 6))

        self.dots: list[tk.Canvas] = []
        self.dot_labels: list[tk.Label] = []
        self.lines: list[tk.Canvas] = []

        for i, (name, _) in enumerate(STEPS):
            if i > 0:
                line = tk.Canvas(self.dot_frame, width=50, height=4, bg=DRIFTWOOD, highlightthickness=0)
                line.create_rectangle(0, 1, 50, 3, fill=STEP_UPCOMING, outline="", tags="line")
                line.grid(row=0, column=i * 2 - 1, padx=0, pady=(0, 14))
                self.lines.append(line)

            dot = tk.Canvas(self.dot_frame, width=24, height=24, bg=DRIFTWOOD, highlightthickness=0)
            dot.grid(row=0, column=i * 2, padx=2, pady=(0, 14))
            self.dots.append(dot)

            lbl = tk.Label(
                self.dot_frame, text=name, font=(BODY_FONT, 7),
                fg=FOGGY, bg=DRIFTWOOD,
            )
            lbl.grid(row=1, column=i * 2, padx=0)
            self.dot_labels.append(lbl)

        tk.Frame(self.header, bg=NOOK_TAN, height=2).pack(fill="x", side="bottom")

    def _update_dots(self) -> None:
        for i, dot in enumerate(self.dots):
            dot.delete("all")
            if i < self.current_step:
                dot.create_oval(2, 2, 22, 22, fill=STEP_DONE, outline=NOOK_GREEN_DARK)
                dot.create_text(12, 12, text="✓", fill="white", font=(BODY_FONT, 9, "bold"))
                self.dot_labels[i].configure(fg=NOOK_GREEN_DARK)
            elif i == self.current_step:
                dot.create_oval(2, 2, 22, 22, fill=STEP_ACTIVE, outline="#C89030")
                dot.create_text(12, 12, text=str(i + 1), fill="white", font=(BODY_FONT, 9, "bold"))
                self.dot_labels[i].configure(fg=SOIL_BROWN)
            else:
                dot.create_oval(2, 2, 22, 22, fill=STEP_UPCOMING, outline=FOGGY)
                dot.create_text(12, 12, text=str(i + 1), fill=FOGGY, font=(BODY_FONT, 9))
                self.dot_labels[i].configure(fg=FOGGY)

        for i, line in enumerate(self.lines):
            line.delete("line")
            color = STEP_DONE if i < self.current_step else STEP_UPCOMING
            line.create_rectangle(0, 1, 50, 3, fill=color, outline="", tags="line")

        name = STEPS[self.current_step][0]
        self.step_label.configure(text=f"Step {self.current_step + 1} of {len(STEPS)} — {name}")

    # ── Content ──────────────────────────────────────────────────

    def _build_content(self) -> None:
        self.content = tk.Frame(self.root, bg=SANDY_SHORE)
        self.content.grid(row=2, column=0, sticky="nsew")
        self.root.grid_rowconfigure(2, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.step_frames: list = []
        for i, (_, StepClass) in enumerate(STEPS):
            frame = StepClass(self.content, self)
            frame.grid(row=0, column=0, sticky="nsew")
            self.step_frames.append(frame)

    # ── Footer ───────────────────────────────────────────────────

    def _build_footer(self) -> None:
        tk.Frame(self.root, bg=NOOK_TAN, height=1).grid(row=3, column=0, sticky="ew")

        self.footer = tk.Frame(self.root, bg=DRIFTWOOD, height=60)
        self.footer.grid(row=4, column=0, sticky="ew")
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
            new_step = self.step_frames[self.current_step]
            if hasattr(new_step, "on_show"):
                new_step.on_show()

    def _on_back(self) -> None:
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def go_to_step(self, index: int) -> None:
        self._show_step(index)
        new_step = self.step_frames[index]
        if hasattr(new_step, "on_show"):
            new_step.on_show()

    def get_enabled_platforms(self) -> list[str]:
        return [k for k in PLATFORM_KEYS if self.data[f"{k}_enabled"].get()]

    def fill_dummy_data(self) -> None:
        from auto_applier.config import RESUMES_DIR

        dummy_resume = RESUMES_DIR / "dummy_resume.docx"
        if not dummy_resume.exists():
            self._create_dummy_resume(dummy_resume)

        self.data["linkedin_enabled"].set(True)
        self.data["linkedin_email"].set("jane.doe@example.com")
        self.data["linkedin_password"].set("DummyPassword123!")
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
        content = (
            "Jane Doe\nSoftware Engineer\n"
            "jane.doe@example.com | +1 (555) 123-4567 | San Francisco, CA\n"
            "linkedin.com/in/janedoe | janedoe.dev\n\nSKILLS\n"
            "Python, JavaScript, TypeScript, React, Node.js, Django, Flask,\n"
            "PostgreSQL, MongoDB, Docker, AWS, Git, Agile, CI/CD\n\nEXPERIENCE\n"
            "Senior Software Engineer — Acme Corp (2020-Present)\n"
            "- Built scalable REST APIs serving 1M+ requests/day\n\n"
            "EDUCATION\nB.S. Computer Science — UC Berkeley (2017)\n"
        )
        from docx import Document
        doc = Document()
        for line in content.strip().split("\n"):
            doc.add_paragraph(line)
        path = path.with_suffix(".docx")
        doc.save(str(path))

    def run(self) -> None:
        self.root.mainloop()


def launch_wizard() -> None:
    app = WizardApp()
    app.run()
