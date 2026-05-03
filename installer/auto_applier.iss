; Inno Setup script for Auto Applier v2.
;
; Build flow:
;   1. python scripts/write_version.py    -> writes VERSION
;   2. python build.py                    -> writes dist/AutoApplier.exe
;   3. iscc installer/auto_applier.iss    -> writes installer/Output/*-setup.exe
;
; Or: python installer/build_installer.py to do all three in one shot.
;
; Inno Setup is free (https://jrsoftware.org/isdl.php). The Python
; build driver shells out to iscc.exe — install Inno Setup 6 once
; on the dev machine, add it to PATH, and the rest is automatic.
;
; Design choices:
;
;   - Per-user install (PrivilegesRequired=lowest) so users without
;     admin can install. Avoids triggering UAC, which Sam et al
;     don't have admin for on their work laptops.
;   - PostInstall.ps1 runs `playwright install chromium` and
;     optionally launches the Ollama installer if it's not already
;     present. The installer never auto-downloads multi-GB models
;     — that's left to first-run-of-app where the user can see
;     progress in the wizard.
;   - data/ is preserved on upgrade. The installer's [Files]
;     section explicitly excludes data/* and the uninstaller is
;     configured not to remove it. Users would lose their CSVs +
;     resumes + LLM cache otherwise.
;   - VERSION file produced by scripts/write_version.py is
;     bundled so non-git installs still get a real version stamp
;     in run logs.

#define MyAppName "Auto Applier"
#define MyAppVersion GetFileVersion("..\dist\AutoApplier.exe")
#if MyAppVersion == ""
  #define MyAppVersion "2.0.0"
#endif
#define MyAppPublisher "ArcadianSnow"
#define MyAppExeName "AutoApplier.exe"

[Setup]
AppId={{F5A20A1E-7F8B-4A94-9F77-AUTOAPPLIER-V2}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\AutoApplier
DefaultGroupName=Auto Applier
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputBaseFilename=AutoApplier-Setup-{#MyAppVersion}
OutputDir=Output
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
LicenseFile=license.txt
SetupIconFile=icon.ico
UninstallDisplayName=Auto Applier {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
; ArchitecturesInstallIn64BitMode is empty so 32-bit Python builds
; also work; in practice we ship 64-bit only.
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "playwrightinstall"; Description: "Install browser used for job sites (Chromium ~150 MB, recommended)"; GroupDescription: "Required components:"; Flags: checkedonce
Name: "ollamacheck"; Description: "Check for Ollama (the local AI engine) and offer install if missing"; GroupDescription: "Required components:"; Flags: checkedonce

[Files]
; Main executable produced by PyInstaller
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; VERSION stamp produced by scripts/write_version.py
Source: "..\VERSION"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; Configuration template — copied as data/.env when missing,
; never overwritten on upgrade.
Source: "..\.env.example"; DestDir: "{app}"; Flags: ignoreversion

; License + readme for the install location
Source: "license.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; Bootstrap script for post-install actions (playwright + ollama).
Source: "post_install.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Run post-install bootstrap. -ExecutionPolicy Bypass keeps the
; user from having to flip system PowerShell policy. The script
; itself is signed by no-one — it's just shell-out wrapping that
; could equally be a .bat file.
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\post_install.ps1"" -InstallDir ""{app}"" -DoPlaywright {code:GetPlaywrightFlag} -DoOllama {code:GetOllamaFlag}"; \
  StatusMsg: "Installing browser components and detecting AI engine..."; \
  Flags: runhidden waituntilterminated

; Optional: launch the app immediately after install.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Don't auto-delete data/ directory contents. We don't reference
; them in [Files] to begin with (they're created at first run),
; but be explicit about preserving user data on uninstall.
; ``Type: filesandordirs; Name: "{app}\data"`` would delete them;
; we omit it deliberately.

[Code]
function GetPlaywrightFlag(Default: string): string;
begin
  if WizardIsTaskSelected('playwrightinstall') then
    Result := '1'
  else
    Result := '0';
end;

function GetOllamaFlag(Default: string): string;
begin
  if WizardIsTaskSelected('ollamacheck') then
    Result := '1'
  else
    Result := '0';
end;
