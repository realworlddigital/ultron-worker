<#
.SYNOPSIS
  Install the Ultron local job runner on this machine (David runs this, on Ultron).

.DESCRIPTION
  1. Creates a virtualenv under %ProgramData%\Ultron\venv and installs this repo into it
     (add -Transcribe to also install faster-whisper).
  2. Copies config.example.toml to %ProgramData%\Ultron\config.toml if none exists.
  3. Locks the key file down to the current user + SYSTEM. It never writes a key: David pastes
     the runner key he issued into runner.key himself.
  4. Builds the desktop app (unless -NoApp): Ultron.exe, a tiny PyInstaller launcher with the
     Ultron icon, plus a Start Menu shortcut. The app owns the runner (Start/Stop/Restart, tray,
     autostart at logon from its own toggle, health lights, check-for-update + one-click update).
     It writes install.json, which records the repo, venv and installed commit for the updater.
  5. Retires the old "Ultron Runner" Scheduled Task: if it is registered it is removed, so it
     can't start a second runner beside the app. -RegisterTask is kept only to say so.
  Nothing listens on the LAN: the runner polls out over HTTPS; GUI and app bind 127.0.0.1 only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Transcribe
#>
[CmdletBinding()]
param(
  [switch]$Transcribe,
  [switch]$NoApp,
  [switch]$RegisterTask,  # retired: the app's "Start Ultron when I sign in" toggle replaces it
  [string]$Python = "py",
  [string]$Root = (Join-Path $env:ProgramData "Ultron")
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

$Running = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", 8764); $c.Close(); $Running = $true } catch {}
if ($Running) { throw "The Ultron app is running. Quit it from the tray icon first (or use its Update button)." }

Write-Host "Ultron runner install -> $Root" -ForegroundColor Red
New-Item -ItemType Directory -Force -Path $Root, (Join-Path $Root "state") | Out-Null

$Venv = Join-Path $Root "venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  & $Python -3.12 -m venv $Venv
  if ($LASTEXITCODE -ne 0) { & $Python -3 -m venv $Venv }
  if ($LASTEXITCODE -ne 0) { throw "could not create a Python 3.11+ virtualenv (is Python installed?)" }
}
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --upgrade pip | Out-Null
$Extras = @()
if (-not $NoApp) { $Extras += "desktop" }
if ($Transcribe) { $Extras += "transcribe" }
$Extra = if ($Extras) { "[" + ($Extras -join ",") + "]" } else { "" }
& $Py -m pip install "$Repo$Extra"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$Config = Join-Path $Root "config.toml"
if (-not (Test-Path $Config)) {
  Copy-Item (Join-Path $Repo "config.example.toml") $Config
  Write-Host "Wrote $Config - set backbone.url and allowed_roots." -ForegroundColor Yellow
}

$Key = Join-Path $Root "runner.key"
if (-not (Test-Path $Key)) { New-Item -ItemType File -Path $Key | Out-Null }
& icacls $Key /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "SYSTEM:(R)" | Out-Null

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Warning "Ollama not found on PATH: install it from https://ollama.com (keep OLLAMA_HOST at 127.0.0.1)."
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Warning "ffmpeg not found: vision_tag will skip videos until it is installed (winget install Gyan.FFmpeg)."
}

$Runner = Join-Path $Venv "Scripts\ultron-runner.exe"
# The Scheduled Task route is retired (it exited with code 1 on its one observed run and was
# never diagnosed); the app's autostart toggle (HKCU Run key, no admin) replaces it.
if ($RegisterTask) {
  Write-Warning "-RegisterTask is retired: use the app's 'Start Ultron when I sign in' toggle."
}
if (Get-ScheduledTask -TaskName "Ultron Runner" -ErrorAction SilentlyContinue) {
  try {
    Stop-ScheduledTask -TaskName "Ultron Runner" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "Ultron Runner" -Confirm:$false
    Write-Host "Removed the old 'Ultron Runner' Scheduled Task (the app replaces it)." -ForegroundColor Yellow
  } catch {
    Write-Warning "Couldn't remove the old 'Ultron Runner' task ($_). From an admin prompt: Unregister-ScheduledTask 'Ultron Runner'"
  }
}

$Exe = Join-Path $Root "Ultron.exe"
$Ico = Join-Path $Root "ultron.ico"
$Launcher = $null
if (-not $NoApp) {
  & $Py -m ultron_worker.desktop.icon $Ico | Out-Null
  $BuildVenv = Join-Path $Root "build-venv"
  $Work = Join-Path $env:TEMP "ultron-app-build"
  try {
    if (-not (Test-Path (Join-Path $BuildVenv "Scripts\python.exe"))) { & $Py -m venv $BuildVenv }
    $BPy = Join-Path $BuildVenv "Scripts\python.exe"
    & $BPy -m pip install --upgrade pip pyinstaller | Out-Null
    & $BPy -m PyInstaller --noconfirm --onefile --windowed --name Ultron --icon $Ico `
      --distpath $Root --workpath $Work --specpath $Work (Join-Path $Repo "scripts\launcher.py") | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Exe)) { throw "PyInstaller exited $LASTEXITCODE" }
    $Launcher = $Exe
    Write-Host "Built $Exe" -ForegroundColor Green
  } catch {
    Write-Warning "Couldn't build Ultron.exe ($_); the shortcut starts the app with pythonw instead."
  }
  $Lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Ultron.lnk"
  $Shell = New-Object -ComObject WScript.Shell
  $Sc = $Shell.CreateShortcut($Lnk)
  if ($Launcher) {
    $Sc.TargetPath = $Launcher; $Sc.Arguments = ""
  } else {
    $Sc.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
    $Sc.Arguments = "-m ultron_worker.desktop --root `"$Root`""
  }
  $Sc.WorkingDirectory = $Root
  $Sc.IconLocation = $Ico
  $Sc.Description = "Ultron local job runner"
  $Sc.Save()
  Write-Host "Start Menu shortcut: $Lnk" -ForegroundColor Green
}
$RecordArgs = @("--root", $Root, "--repo", $Repo, "--venv", $Venv, "--extras", ($Extras -join ","))
if ($Launcher) { $RecordArgs += @("--launcher", $Launcher) }
& $Py -m ultron_worker.desktop.layout record @RecordArgs
if ($LASTEXITCODE -ne 0) { throw "could not write install.json" }

Write-Host ""
Write-Host "Next (David):" -ForegroundColor Red
Write-Host "  1. Paste the ultron-runner key into $Key (one line)."
Write-Host "  2. Edit $Config (backbone.url, allowed_roots)."
Write-Host "  3. ollama pull <vision model>   e.g. ollama pull llava:13b"
Write-Host "  4. & `"$Root\ultron-runner.cmd`" --config `"$Config`" --check"
if ($NoApp) {
  Write-Host "  5. Run: & `"$Runner`" --config `"$Config`""
} else {
  Write-Host "  5. Start Ultron from the Start Menu; turn on 'Start Ultron when I sign in' in the app."
}
Write-Host "  Status GUI: http://127.0.0.1:8765/ (also inside the app)"
