# Setting up Auto Applier (no repo / git clone needed)

This is the install guide for **running the app**, not developing it — you don't need to clone the
repository. Everything installs from the public GitHub repo with one `pip` command, runs fully on
your machine, and costs nothing. Works on Windows (primary), macOS, and Linux.

> Updating later is one command: **`av3 update --apply`** (see [Updating](#updating)).

---

## What you need first

1. **Python 3.11 or newer.** Get it from <https://www.python.org/downloads/>.
   - **Windows:** on the first installer screen, tick **"Add python.exe to PATH"** before clicking
     Install. (If you forget, see [Troubleshooting](#troubleshooting).)
   - Check it worked — open a new terminal (PowerShell on Windows) and run: `python --version`
2. **Ollama** (the local AI engine — free, offline). Install from <https://ollama.com/download>.
   You'll pull the models in Step 2; you don't need to do anything else with Ollama by hand.

---

## Step 1 — Install Auto Applier

In a terminal, run (copy the whole line, quotes included):

```bash
pip install "auto-applier[v3] @ https://github.com/ArcadianSnow/Auto-Applier/archive/refs/heads/master.zip"
```

That's it — no git, no clone. It installs the app and its dependencies, and adds the **`av3`**
command.

<details>
<summary>Cleaner alternative: install in its own isolated environment (optional)</summary>

If you run other Python tools and want to avoid any dependency clashes, use
[pipx](https://pipx.pypa.io) instead — it isolates the app but still puts `av3` on your PATH:

```bash
pip install --user pipx        # once, if you don't have pipx
pipx install "auto-applier[v3] @ https://github.com/ArcadianSnow/Auto-Applier/archive/refs/heads/master.zip"
```

`av3 update --apply` still works the same way inside a pipx install.
</details>

---

## Step 2 — One-time setup

```bash
av3 setup-llm          # downloads the two local AI models (several GB — first run only)
av3 install-browser    # downloads the browser the bot uses
av3 init-db            # creates your local database + data folder
av3 doctor             # checks everything is ready (models, browser, DB)
```

`av3 doctor` should print mostly `PASS`. A `WARN` is fine to start (e.g. "no backups yet"); a
`FAIL` tells you exactly what to fix.

---

## Step 3 — Launch

```bash
av3 launch
```

This starts the worker and opens the dashboard in your browser. The first time, it walks you
through a short **setup wizard** (your résumé/contact details, what jobs you want, and optionally
connecting your email to track replies). After that, the app finds and scores jobs on its own —
new matches appear on the dashboard.

To stop it, close the launcher window (or press `Ctrl-C` in the terminal).

---

## Where your data lives

Everything stays on your machine. By default:

- **Windows:** `%LOCALAPPDATA%\AutoApplier\data`  (e.g. `C:\Users\<you>\AppData\Local\AutoApplier\data`)
- **macOS / Linux:** `~/.local/share/auto-applier`

That folder holds your profile, the job database, generated résumés/cover letters, and backups.
**Want it somewhere else** (e.g. a synced drive)? Set the `AV3_DATA_DIR` environment variable to a
folder of your choice before running `av3`, and it'll use that instead.

---

## Updating

When there's a new version, update in place with one command:

```bash
# stop the app first if it's running (close the launcher window), then:
av3 update --apply
```

This pulls the latest code from GitHub and reinstalls it. Restart with `av3 launch` afterwards.
(`av3 update` on its own just *checks* whether you're behind without changing anything.)

---

## Troubleshooting

- **`av3` is not recognized / command not found.** Python's scripts folder isn't on your PATH.
  Either reinstall Python with **"Add python.exe to PATH"** ticked, or run the app via the module
  form instead — anywhere you'd type `av3 X`, use `python -m auto_applier.cli.main X`
  (e.g. `python -m auto_applier.cli.main launch`).
- **`pip` is not recognized (Windows).** Use `py -m pip ...` instead of `pip ...`
  (and `py -m auto_applier.cli.main ...` instead of `av3 ...`).
- **`av3 doctor` says Ollama is unreachable or a model is missing.** Make sure Ollama is installed
  and running, then run `av3 setup-llm` again.
- **Anything else.** Run `av3 doctor` — each line that isn't `PASS` includes a `fix ->` hint.

---

## Uninstall

```bash
pip uninstall auto-applier        # or: pipx uninstall auto-applier
```

Your data folder (above) is left in place — delete it by hand if you also want to remove your
profile, database, and generated documents.
