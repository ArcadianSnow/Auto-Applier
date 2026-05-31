# Auto Applier — Installer Build Notes

Per Tier 4 research: a one-click Windows installer is the single
biggest user-facing onboarding fix available. This directory
contains everything needed to build one.

## What gets shipped

End users double-click `AutoApplier-Setup-<version>.exe` and walk
through a standard Windows install wizard. The installer:

1. Installs `AutoApplier.exe` (the PyInstaller bundle) to
   `%LOCALAPPDATA%\AutoApplier\` (no admin required)
2. Creates Start Menu and optional Desktop shortcuts
3. Runs `post_install.ps1` which:
   - Installs Playwright's Chromium build (~150 MB) if a system
     Python is available — otherwise this happens at first app run
   - Detects whether Ollama is installed; if not, offers to open
     the Ollama download page (we never silently download the
     ~600 MB Ollama installer or the ~9.6 GB Gemma 4 model)
4. Optionally launches the app at the end

User data (`data/*`) is preserved on upgrade and uninstall —
the installer never overwrites or removes the user's CSVs, resumes,
browser profile, or LLM cache.

## Build prerequisites

On the **build machine** (Windows; only Windows can build a
Windows installer):

- **Python 3.11+** with project deps: `pip install -e ".[dev]"`
- **Inno Setup 6**: https://jrsoftware.org/isdl.php — install once,
  then `iscc.exe` is found automatically by the build driver
- The repo must be a git checkout (so `write_version.py` can stamp
  the build) OR a `VERSION` file already exists
- An icon at `installer/icon.ico` (256×256 .ico). Inno Setup will
  fail loudly if it's missing — drop in any 256×256 .ico file. The
  current commit ships **without** a real icon to keep the image
  file out of git; create one before building or comment out the
  `SetupIconFile` line in `auto_applier.iss`.

## One-shot build

```cmd
python installer\build_installer.py
```

This runs `write_version.py` → `build.py` → `iscc` in order. The
finished installer lands in `installer\Output\`.

## Subset builds

```cmd
:: Just rebuild the PyInstaller exe (skip version + iscc)
python installer\build_installer.py --skip-version --skip-iscc

:: Re-run iscc only (after editing the .iss script)
python installer\build_installer.py --skip-version --skip-pyinstaller

:: Custom output dir
python installer\build_installer.py --output-dir C:\Releases\v2.0
```

## File layout in this directory

| File | Purpose |
|------|---------|
| `auto_applier.iss` | Inno Setup script. The build target. |
| `post_install.ps1` | Post-install bootstrap (playwright + ollama). |
| `build_installer.py` | Driver: version → exe → iscc. |
| `license.txt` | Personal-use license notice shown by the wizard. |
| `icon.ico` | **Not in git.** 256×256 setup icon. Provide before build. |
| `Output/` | **Generated.** Where installers land. Gitignored. |

## What lives outside this directory

| File | Read-by | Purpose |
|------|---------|---------|
| `../scripts/write_version.py` | `build_installer.py` | Stamps `VERSION` at root. |
| `../build.py` | `build_installer.py` | PyInstaller driver. |
| `../dist/AutoApplier.exe` | `auto_applier.iss` | The bundled application. |
| `../.env.example` | `auto_applier.iss` | Template config bundled with install. |
| `../README.md` | `auto_applier.iss` | Bundled with install if present. |

## Distribution

Friends download the installer via:
- A GitHub release page (recommended — automatic SHA hashes,
  signed if Cosign is configured)
- A direct download link the user posts in chat
- The existing `update.bat` flow (which downloads the zip — the
  installer makes this redundant for new installs but kept for
  surgical updates)

Never email the .exe. Email gateways block unsigned executables
and the bounce experience is bad.

## SmartScreen and signing

The installer is **unsigned** as of 2026-05-03. Windows SmartScreen
will show a "publisher unknown" warning that users have to
"Run anyway" through. This is friction we should fix:

- **Quickest fix:** sign with a self-signed cert. Removes the
  "unknown publisher" line but still gets a warning.
- **Real fix:** buy an EV code-signing cert (~$300/year). SmartScreen
  trust accumulates after a few installs and the warning goes away.

Out of scope for the personal-use posture; revisit if we ever
distribute beyond the original three to four friends.

## Testing the installer

1. Build it (above).
2. Copy to a clean Windows VM or a friend's machine.
3. Walk through the wizard with **all** components selected.
4. Confirm the app launches from the Start Menu shortcut.
5. Confirm `cli doctor` reports green-ish (some FAIL items are
   first-run-expected: `data/answers.json` missing until the
   wizard runs).
6. Run an upgrade install over an existing v2 → confirm
   `%LOCALAPPDATA%\AutoApplier\data\` was preserved.
7. Uninstall → confirm `data/` survives (we do not delete it).

## Troubleshooting build failures

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `iscc.exe not found` | Inno Setup 6 not installed | Install from jrsoftware.org/isdl.php |
| `dist\AutoApplier.exe not found` | PyInstaller step skipped or failed | Re-run without `--skip-pyinstaller` |
| `icon.ico missing` (Inno Setup error) | No icon shipped | Place a 256×256 .ico at `installer/icon.ico` or remove `SetupIconFile` from .iss |
| Friend installs OK but Chrome won't launch | Playwright install skipped | `python -m playwright install chromium` after install |
| Friend installs OK but Ollama not detected | Ollama installer needs admin on some machines | User clicks through Ollama installer themselves |
