# Post-install bootstrap for Auto Applier.
#
# Runs once at install time, invoked by Inno Setup's [Run] section.
# Two jobs:
#
#   1. Install Playwright's Chromium build, which the apply paths
#      need for headed automation.
#   2. Detect Ollama. If present, log the version and exit. If not,
#      offer to download the official installer (the user clicks
#      through it themselves).
#
# Designed to fail SOFTLY. If anything goes wrong, the script writes
# a note to %LOCALAPPDATA%\AutoApplier\install.log and exits 0 — we
# don't want a transient network blip during install to leave the
# user with a broken-looking error dialog. The app itself runs
# `cli doctor` on first launch and surfaces actionable errors there.
#
# Parameters mirror the Inno Setup [Run] line:
#   -InstallDir   The {app} directory that was just installed to
#   -DoPlaywright "1" / "0" — whether to run `playwright install`
#   -DoOllama     "1" / "0" — whether to detect/offer Ollama

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $InstallDir,
    [Parameter(Mandatory = $true)] [string] $DoPlaywright,
    [Parameter(Mandatory = $true)] [string] $DoOllama,
    # Optional component: when "1", attempt `pip install nodriver`
    # so the experimental LinkedIn engine works out of the box.
    # Default "0" so older installers (pre-2026-05-03) still satisfy
    # the contract.
    [Parameter(Mandatory = $false)] [string] $DoNodriver = "0"
)

$ErrorActionPreference = 'Continue'  # Don't bail on first error
$LogPath = Join-Path $InstallDir 'install.log'

function Write-Log {
    param([string] $Message)
    $stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    "[$stamp] $Message" | Out-File -FilePath $LogPath -Append -Encoding utf8
}

function Test-OllamaInstalled {
    # Try the binary path first (covers the standard installer
    # location), then fall back to PATH lookup. Either is fine.
    $defaultPath = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $defaultPath) {
        return $defaultPath
    }
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

Write-Log "Auto Applier post-install bootstrap starting."
Write-Log "InstallDir=$InstallDir, DoPlaywright=$DoPlaywright, DoOllama=$DoOllama, DoNodriver=$DoNodriver"

# ----------------------------------------------------------------------
# 1. Playwright Chromium install
# ----------------------------------------------------------------------
if ($DoPlaywright -eq '1') {
    Write-Log "Installing Playwright Chromium..."
    $exe = Join-Path $InstallDir 'AutoApplier.exe'
    if (-not (Test-Path $exe)) {
        Write-Log "WARNING: $exe not found; skipping playwright install."
    }
    else {
        # PyInstaller-bundled exes can shell out to themselves with a
        # subcommand-style argv. We don't currently expose `playwright
        # install` through our CLI — the standard path is to run
        # the Python module directly. PyInstaller-onefile bundles
        # don't have that module available externally, so we shell out
        # to system Python if it exists; otherwise we leave a note in
        # the log and let the app's first-run doctor surface the
        # missing-browser error to the user.
        $py = Get-Command python -ErrorAction SilentlyContinue
        if (-not $py) {
            $py = Get-Command python3 -ErrorAction SilentlyContinue
        }
        if (-not $py) {
            $py = Get-Command py -ErrorAction SilentlyContinue
        }
        if ($py) {
            try {
                Write-Log "Using $($py.Source) -m playwright install chromium"
                & $py.Source -m playwright install chromium 2>&1 |
                    Out-File -FilePath $LogPath -Append -Encoding utf8
                Write-Log "Playwright install completed (exit=$LASTEXITCODE)."
            }
            catch {
                Write-Log "Playwright install raised: $($_.Exception.Message)"
            }
        }
        else {
            Write-Log "No system Python found; Playwright will be installed on first app run."
        }
    }
}
else {
    Write-Log "Skipping Playwright install (user opted out)."
}

# ----------------------------------------------------------------------
# 2. Ollama detection
# ----------------------------------------------------------------------
if ($DoOllama -eq '1') {
    $ollamaPath = Test-OllamaInstalled
    if ($ollamaPath) {
        Write-Log "Ollama already installed at $ollamaPath."
        try {
            $version = & $ollamaPath --version 2>&1
            Write-Log "Ollama version: $version"
        }
        catch {
            Write-Log "Ollama present but --version failed: $($_.Exception.Message)"
        }
    }
    else {
        Write-Log "Ollama not detected."
        # Offer the user a one-click install. We don't auto-download
        # silently — Ollama's installer is ~600 MB, that's a real
        # commitment we want consent for. The dialog is shown via a
        # WScript.Shell popup so the install flow doesn't get stuck.
        $url = "https://ollama.com/download/OllamaSetup.exe"
        try {
            $msg = "Auto Applier uses Ollama to run AI locally (free, offline).`n`n" +
                   "Ollama isn't installed yet. Open the official download page now?"
            $shell = New-Object -ComObject WScript.Shell
            # 0x4 = Yes/No, 0x40 = Question icon
            $answer = $shell.Popup($msg, 0, "Auto Applier — Install Ollama?", 0x4 -bor 0x40)
            if ($answer -eq 6) {  # 6 = Yes
                Write-Log "User opted to download Ollama from $url"
                Start-Process $url
            }
            else {
                Write-Log "User declined Ollama download. Cloud fallback (Gemini) will be used."
            }
        }
        catch {
            Write-Log "Could not show Ollama install prompt: $($_.Exception.Message)"
        }
    }
}
else {
    Write-Log "Skipping Ollama detection (user opted out)."
}

# ----------------------------------------------------------------------
# 3. Optional Nodriver install
# ----------------------------------------------------------------------
if ($DoNodriver -eq '1') {
    Write-Log "Installing nodriver (experimental LinkedIn engine)..."
    # Same Python-discovery logic as Playwright. PyInstaller-onefile
    # bundles can't pip-install into themselves, so we shell out to
    # system Python. If absent, the wizard's Sites step has its own
    # in-app install button as a fallback.
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
    if ($py) {
        try {
            Write-Log "Using $($py.Source) -m pip install nodriver"
            & $py.Source -m pip install nodriver 2>&1 |
                Out-File -FilePath $LogPath -Append -Encoding utf8
            Write-Log "Nodriver install completed (exit=$LASTEXITCODE)."
        }
        catch {
            Write-Log "Nodriver install raised: $($_.Exception.Message)"
        }
    }
    else {
        Write-Log "No system Python found; user can install nodriver later via the wizard's Sites step."
    }
}
else {
    Write-Log "Skipping Nodriver install (user opted out)."
}

Write-Log "Post-install bootstrap done."
exit 0
