# Exe distribution viability + onboarding restructure (research, 2026-06-20)

> **UPDATE 2026-06-21 — Phase 1 (the onboarding/setup restructure below) SHIPPED.** Terminal-free
> first-run setup is live (in-app model pull + browser install + readiness panel) for both the exe
> and pip paths. See [onboarding-setup-restructure.md](onboarding-setup-restructure.md). The
> packaging work (Phase 2: embedded-Python + Inno installer) remains deferred/unbuilt, and the
> owner decisions in §@open below are still open.

**Status:** RESEARCH / pre-build. The owner asked: for non-technical friends the program "must be
startable from an exe" — then redirected to *"research viability of all of this and consider
restructuring the onboarding/setup to facilitate an exe"* before building anything. This doc is that
research. Nothing here is built. When greenlit, the design moves into the spec + a build plan.

Companion: `research/mvp-readiness.md` (the pip-install path + first-run friction already shipped),
`research/observability-and-distribution.md` ("Distribution v2 — pip-from-GitHub"). [[project_future_directions]].

---

## The goal, stated precisely

A non-technical friend (gamer, capable PC, NOT comfortable with a terminal/pip) can **download one
file, double-click, and end up at the dashboard finding + scoring jobs** — with the AI engine and
browser set up along the way, ideally without ever seeing a command line.

Two things are emphatically NOT bundle-able and shape everything below:
- **Ollama** — a separate ~600 MB app + **multi-GB models** (`gemma4:e4b` ~9.6 GB + `nomic-embed-text`).
  Cannot live inside our exe. Must be installed + pulled separately, with visible progress.
- **Chromium** — already fetched on first run (not bundled); most applies use the user's real Chrome.

So "an exe" can only ever be *the Python app*. The AI engine + browser are first-run bootstrap steps.

---

## Current state — the scaffolding is ~85% there (inventory)

The exe pipeline was scaffolded in Phase 5 and is mostly present:

| Piece | State |
|---|---|
| `run.py` | ✅ EXISTS + correct. PyInstaller entry: no args → `av3 launch`; args → the full Click CLI. |
| `build.py` | ⚠️ EXISTS but stale/incomplete (see gaps). PyInstaller driver, onefile default. |
| `installer/auto_applier.iss` | ✅ Complete Inno Setup script: per-user install to `%LOCALAPPDATA%\AutoApplier` (no admin/UAC), Start-Menu + optional desktop shortcuts, runs `post_install.ps1`, offers "launch now". Expects `dist\AutoApplier.exe`. |
| `installer/build_installer.py` | ✅ Complete driver: `write_version.py` → `build.py` → `iscc`. |
| `installer/post_install.ps1` | ✅ Installs Playwright Chromium (if a Python is present) + detects Ollama, offers the download page. Already fixed the stale "Gemini fallback" string. Does **not** pull LLM models or init the DB. |
| `scripts/write_version.py` | ✅ EXISTS (stamps a `VERSION` file). |
| `launch_cmd` frozen handling | ✅ Already branches on `sys.frozen`: a frozen exe spawns `<exe> serve …`, source spawns `python -m auto_applier.cli.main serve …` (cli/main.py:2769). |
| Default data dir | ✅ Now `%LOCALAPPDATA%\AutoApplier\data` (the MVP pass) — aligns with the installer's install dir; an exe won't scatter `data/`. |
| **PyInstaller** | ✅ Installed on the build host (6.20.0). |
| **Inno Setup `iscc`** | ❌ NOT installed on the build host → cannot produce the final `AutoApplier-Setup.exe` here without a one-time Inno Setup 6 install. |

### Gaps in the current `build.py` (must fix before it produces a working exe)
1. **Wrong output name.** Produces `AutoApplierV3.exe`; the installer (`.iss` + `build_installer.py`)
   expects `dist/AutoApplier.exe`. Pick one name and make all three agree (recommend `AutoApplier`).
2. **Missing data file.** It bundles `web/templates`, `web/static`, `db`, `.env.example` — but NOT
   **`auto_applier/data/ats_companies.csv`** (the ~316 KB company directory). Without it,
   `seed-boards` / "find companies" breaks in the exe. (pip-install ships it via package-data; the
   exe build forgot it.)
3. **No Playwright/patchright collection.** It collects fastapi/uvicorn submodules but NOT
   **patchright / playwright / playwright-stealth**. The launch path imports patchright when the
   scheduler builds the apply worker → a fresh exe with a fact bank would crash on `import patchright`
   unless these are collected. **This is the highest-risk item** (see viability).
4. Cosmetic: `run.py` + `build.py` docstrings reference a `build_v3.py` that doesn't exist.

---

## ⚠️ The real blocker is NOT the exe — it's first-run setup orchestration (onboarding restructure)

**Verified:** there is **no first-run setup orchestration anywhere in the app.** `setup-llm` (pull the
~9.6 GB + embed models) and `install-browser` are **CLI-only, manual**; the onboarding wizard does
**not** run them, and nothing chains init-db. The pip-path docs (SETUP.md) tell the user to run
`av3 setup-llm && av3 install-browser && av3 init-db` by hand — which is exactly the terminal step a
non-technical exe user must never see.

So a friend who double-clicks today's exe would: launch → dashboard → onboarding wizard → save a fact
bank → … and then **scoring/discovery silently fail** because no models were ever pulled and (maybe)
no browser fetched. The exe is the easy 15%; **the onboarding/setup flow is the 85% that actually
needs redesign.** This is precisely what the owner intuited.

### What first-run must do (and currently doesn't), in a guided/visible way
1. **init-db** — trivial, idempotent; the app can just do it on first serve.
2. **Ollama present?** If not, guide to install it (post_install already offers the download; the app
   should also detect + surface "AI engine not installed → [Get Ollama]" in the dashboard).
3. **Pull models** (`gemma4:e4b` + `nomic-embed-text`, multi-GB) — the big one. Must be a **visible,
   resumable, in-app step with progress**, not a silent hang and not a hidden terminal. Today only
   `av3 setup-llm` does it (CLI, streams to a console the user won't see).
4. **Fetch the browser** (`install-browser`) — post_install does this if a Python is present; in a
   frozen-exe world there may be no system Python, so the **app's first run** must be able to do it.
5. **Onboarding wizard** (résumé/goals) — already exists + is good.

### Recommended onboarding restructure (the design to build)
- **A "Setup / readiness" gate on the dashboard** driven by `av3 doctor`'s existing checks
  (config / db / **llm+models** / **inbox** / browser). Render the not-ready items as an actionable
  checklist ("AI engine: models not downloaded → [Download now]", "Browser: not installed → [Install]").
  `doctor` already returns structured `CheckResult(status, fix)` — surface it as JSON + a panel.
- **In-app, progress-streamed bootstrap actions** reusing the seed-boards background-job pattern
  (`POST /start` + `/status` polling, `asyncio.to_thread`): a `POST /api/setup/pull-models` that
  shells `ollama pull` and streams probed/MB progress; a `POST /api/setup/install-browser`. So the
  multi-GB pulls happen **inside the dashboard with a progress bar**, never a console.
- **Sequence it as the first wizard step(s)** ("Step 0 — set up the AI engine") BEFORE profile/goals,
  so a fresh exe install walks: Ollama check → pull models (progress) → fetch browser → profile →
  goals → done → scheduler starts. The scheduler-ready gate is already fact-bank-only (MVP pass), so
  it starts as soon as the fact bank exists; readiness of models is surfaced, not a hard block.
- This restructure is **valuable for the pip path too** (today's pip user still runs 3 manual setup
  commands) — so it's not exe-only work; it's the missing "guided setup" layer under both.

---

## Packaging viability for THIS stack (web research, 2026-06-20; sources inline)

**Verdict: PyInstaller is *workable* on Windows but project-specifically risky; the recommended
packaging path is an Inno Setup installer that lays down a private/embedded Python + pip-installs the
app.** The deciding factor is `patchright`.

### The deciding finding — patchright has no PyInstaller hook
- Vanilla **playwright** bundles cleanly: it ships its own PyInstaller hook *inside the package*
  (`datas = collect_data_files("playwright")`), auto-discovered by PyInstaller 6.x; the node driver
  at `site-packages/playwright/driver/` (incl. `node.exe`) is swept in with no flags. (Windows is the
  supported platform; the in-package hook is known-broken on Linux/macOS under PyInstaller v6.)
- **patchright** (the stealth fork the app actually drives) vendors its own `patchright/driver/` but
  ships **no equivalent hook** → `--collect-all patchright` can't be assumed to grab the driver.
  Tracked as patchright **issue #45** ("Pyinstaller and Nuitka do not Bundle Chromium"), **open +
  `wontfix`**. So a PyInstaller build must **hand-bundle `patchright/driver/`** (custom hook or
  `--add-data ".../patchright/driver;patchright/driver"`) and you maintain that yourself against an
  upstream that declared it out of scope. The node driver is **mandatory even with `channel="chrome"`**
  (channel skips the browser *download*, not the ~30–40 MB driver).

### If staying on PyInstaller — the non-negotiables
1. **onedir, not onefile.** Onefile re-extracts the whole archive (driver included) to a temp `_MEI`
   dir on *every* launch (slow) and the self-extract pattern trips AV/SmartScreen heuristics. Onedir
   pays extraction once + "looks less suspicious"; wrap the folder in Inno Setup. (Maintainer:
   "onefile doesn't scale well on Windows.")
2. **Hand-bundle the patchright driver** (gotcha above).
3. **jinja2 templates + static via `--add-data`** (data, not imports) — and **`auto_applier/data/*.csv`**
   (the build.py gap). uvicorn needs its submodules collected (string-imported loops/protocols; the
   bundled hook handles it since hooks-contrib 2021.3, but add your web entry module if launching via
   import-string).
4. **Compiled deps auto-resolve IF `pyinstaller-hooks-contrib` ≥ 2025.1**: pypdfium2's pdfium DLL hook
   landed **2025.1**; pydantic-core hook fixed for v2 in **2023.5**; rapidfuzz needs nothing. Pin those
   minimums. (Past "rapidfuzz/PyInstaller fails" reports were AV deleting `--windowed` builds.)
5. **First-run `install-browser` from a frozen exe works only if the download targets a PERSISTENT
   path** — default `%LOCALAPPDATA%\ms-playwright`, never `_MEIPASS` (wiped on exit), never
   `PLAYWRIGHT_BROWSERS_PATH=0` (read-only bundle). The **#1 frozen-app failure is a
   `PLAYWRIGHT_BROWSERS_PATH` mismatch between install-time and run-time** ("executable doesn't exist")
   → pin it identically in both. There's no `python -m playwright install` in a frozen exe — invoke via
   the API/bundled driver (the app's `av3 install-browser` already wraps this; verify it frozen).

### SmartScreen / signing reality (2026, primary MS Learn sources)
- An unsigned downloaded exe trips the blue "Windows protected your PC" dialog; **"More info → Run
  anyway" still works** on normal consumer Windows. (Managed/enterprise policy or Win11 **Smart App
  Control** — off by default on upgrades — can hard-block.)
- **EV certs no longer bypass SmartScreen** (changed 2024; MS: paying a premium for EV solely to avoid
  SmartScreen "is no longer justified"). Self-signed = no help. SmartScreen trust is **reputation-based
  with no fixed threshold** and needs "hundreds of clean installs from a wide audience" → **at 3–4
  downloads you can't accumulate it even if you pay.**
- **Recommendation: don't sign.** Document the two-click "Run anyway"; optionally ship a **.zip**
  (extracting often strips Mark-of-the-Web from the inner exe → no prompt; test it). Inno Setup is
  format-neutral here. Only consider **Azure Trusted/Artifact Signing (~$10/mo, no HW token, now open
  to self-employed individuals US/CA)** if a user hits a hard block — and even then it's still
  reputation-based, not instant. (Two snippet-only claims — a "~15,000 downloads" threshold and a
  "CVE MOTW bypass" — were NOT in authoritative sources; treat as false.)

### Alternatives
- **Embedded/private Python + pip + Inno Setup — RECOMMENDED.** The installer lays down a private
  interpreter and pip-installs the app (from the public repo, OR pre-staged wheels bundled at build
  time so no network/toolchain at install), then `patchright install chromium` post-install (or real
  Chrome). **Only approach that avoids the bundler-discovery gotcha class entirely** — patchright's
  driver + pydantic-core/pypdfium2/rapidfuzz are just normal wheels in a real `site-packages`,
  identical to a dev box. Lowest AV exposure (stock, often-signed `python.exe` + a thin launcher).
  Maintenance = `pip install -U` / re-run installer, never a recompile. **Reuses the pip path already
  built + verified this session** (`auto-applier[v3] @ <github zip>`, `av3 update --apply`). Tooling:
  Inno Setup + the Windows embeddable-zip (or `python-embedded-launcher`).
- **Nuitka — AVOID.** Best AV profile, but its Playwright plugin assumes Nuitka-managed browsers and
  **conflicts with patchright + real-Chrome-via-channel** (Nuitka #3225/#2852); every compiled dep is
  a per-package plugin that can break; slowest builds; needs a C toolchain.
- **BeeWare Briefcase / Pynsist / py2exe / cx_Freeze — not suitable** (Playwright story
  undocumented/absent; Pynsist unmaintained since 2021; the freezers share PyInstaller's gotcha class).

### Top risks → mitigations (carry into any build)
1. patchright driver won't auto-bundle → hand-collect it, or use embedded-Python (removes the risk).
2. `PLAYWRIGHT_BROWSERS_PATH` install≠run mismatch → pin to `%LOCALAPPDATA%\ms-playwright` both sides.
3. onefile slow-start + AV → onedir.
4. stale hooks-contrib breaks compiled deps → pin ≥ 2025.1.
5. SmartScreen unavoidable at this scale → document "Run anyway" / ship a zip; don't buy a cert.

---

## Architecture decision

**Recommended: Inno Setup installer + embedded/private Python + pip (NOT PyInstaller).** Rationale:
patchright (the actual stealth driver) has no PyInstaller hook (#45, wontfix), so the PyInstaller path
means hand-maintaining the driver collection forever; the embedded-Python path makes patchright +
every compiled dep "just normal wheels in a real site-packages," reuses the **already-built-and-verified
pip install** (`auto-applier[v3] @ <github zip>` + `av3 update --apply`), has the lowest AV exposure,
and is the lowest-maintenance for a 3–4 person tool (update = `pip install -U`, never a recompile).

| Option | Verdict |
|---|---|
| Embedded-Python + pip + Inno | ✅ **Recommended.** No frozen-bundler gotchas; reuses pip path; lowest maintenance. |
| PyInstaller **onedir** + Inno | ⚠️ Viable fallback. Needs the patchright-driver hand-bundle + onedir + hooks-contrib≥2025.1 + the `build.py` fixes. Use only if embedded-Python proves awkward. |
| PyInstaller onefile | ❌ Slow start + AV-prone; don't. |
| Nuitka / Briefcase / others | ❌ Playwright-fork conflict / undocumented / unmaintained. |

The existing `build.py` + `.iss` are written for PyInstaller-onefile (the ❌ option). Keep them as the
fallback reference, but the embedded-Python installer is a new `.iss` flow (lay down python, pip-install,
post-install browser, shortcut to `pythonw -m auto_applier.cli.main launch`).

**Note:** whichever packaging wins, **the onboarding/setup restructure below is required and is the
bigger job.** Packaging only delivers the bytes; the restructure is what makes first-run terminal-free.

---

## Recommended phasing

- **Phase 1 — onboarding/setup restructure (do FIRST; helps the pip path too).** A dashboard
  **readiness panel** from the existing `doctor` checks (config / db / **llm+models** / browser /
  inbox), rendering not-ready items as an actionable checklist, + **in-app, progress-streamed**
  bootstrap actions (`POST /api/setup/pull-models` shelling `ollama pull`; `POST /api/setup/install-browser`)
  reusing the seed-boards background-job pattern, sequenced as the first wizard step ("Set up the AI
  engine" → pull models w/ progress → fetch browser → profile → goals). Makes first-run terminal-free
  for BOTH the exe and the current pip users. **This is the highest-value slice.**
- **Phase 2 — embedded-Python + Inno installer.** Install Inno Setup 6 (one-time; not on the build
  host now). New `.iss` flow: stage the Windows embeddable Python + pre-staged `[v3]` wheels (all
  compiled deps ship Windows wheels → no network/toolchain at install), pip-install into it,
  post-install `patchright install chromium` (or rely on real Chrome) + the model-pull deferred to
  Phase-1's in-app step, drop Start-Menu/desktop shortcuts to launch. Pin
  `PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright` consistently. Output:
  `AutoApplier-Setup.exe`.
- **Phase 2-alt (fallback) — fix `build.py` for PyInstaller onedir** (name→AutoApplier, add
  `data/*.csv`, hand-bundle `patchright/driver`, jinja2 `--add-data`, onedir, pin hooks-contrib≥2025.1)
  only if embedded-Python is rejected.
- **Phase 3 — distribution polish.** Ship as the installer (+ optionally a .zip to dodge MOTW);
  document the SmartScreen "More info → Run anyway"; do NOT buy a cert (won't help at this scale).
  Optionally publish a GitHub Release for a stable download URL + to light up `av3 update`'s check.

**Effort:** Phase 1 medium (real feature work; reuses the seed-boards async pattern + `doctor`).
Phase 2 medium (embedded-Python + pip bootstrap is fiddly but well-trodden; reuses the verified pip
install). **Owner dependencies:** install Inno Setup 6 (Phase 2); accept the unsigned/SmartScreen
posture (Phase 3).
