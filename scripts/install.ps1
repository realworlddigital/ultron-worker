<#
.SYNOPSIS
  Install the Ultron local job runner on this machine (David runs this, on Ultron).

.DESCRIPTION
  1. Creates a virtualenv under %ProgramData%\Ultron\venv and installs this repo into it
     (add -Transcribe to also install faster-whisper).
  2. Copies config.example.toml to %ProgramData%\Ultron\config.toml if none exists.
  3. Locks the key file down to the current user + SYSTEM. It never writes a key: David pastes
     the runner key he issued into runner.key himself.
  4. With -RegisterTask (and David's approval at the UAC prompt), registers a Scheduled Task
     "Ultron Runner" that starts the runner at logon, restarts it on failure and is allowed to
     run on battery, so it comes back after sleep, reboot and crashes. Nothing listens on the
     LAN: the runner polls out over HTTPS and its GUI binds 127.0.0.1 only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Transcribe -RegisterTask
#>
[CmdletBinding()]
param(
  [switch]$Transcribe,
  [switch]$RegisterTask,
  [string]$Python = "py",
  [string]$Root = (Join-Path $env:ProgramData "Ultron")
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

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
$Extra = if ($Transcribe) { "[transcribe]" } else { "" }
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
if ($RegisterTask) {
  $Action = New-ScheduledTaskAction -Execute $Runner -Argument "--config `"$Config`"" -WorkingDirectory $Root
  $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName "Ultron Runner" -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Ultron local job runner (outbound polling; GUI on 127.0.0.1)" -Force | Out-Null
  Write-Host "Registered Scheduled Task 'Ultron Runner' (starts at logon)." -ForegroundColor Green
}

Write-Host ""
Write-Host "Next (David):" -ForegroundColor Red
Write-Host "  1. Paste the ultron-runner key into $Key (one line)."
Write-Host "  2. Edit $Config (backbone.url, allowed_roots)."
Write-Host "  3. ollama pull <vision model>   e.g. ollama pull llava:13b"
Write-Host "  4. & `"$Runner`" --config `"$Config`" --check"
Write-Host "  5. Start: Start-ScheduledTask 'Ultron Runner'  (or run step 4 without --check)"
Write-Host "  GUI: http://127.0.0.1:8765/"
