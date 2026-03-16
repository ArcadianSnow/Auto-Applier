"""Build Auto Applier into a standalone .exe using PyInstaller."""

import subprocess
import sys


def build():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "AutoApplier",
        "--onefile",
        "--windowed",
        "--add-data", "auto_applier;auto_applier",
        "--add-data", ".env.example;.",
        "run.py",
    ]

    print("Building AutoApplier.exe ...")
    print(f"Command: {' '.join(cmd)}\n")
    subprocess.run(cmd, check=True)
    print("\nDone! Executable is in the dist/ folder.")


if __name__ == "__main__":
    build()
