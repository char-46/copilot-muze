#Requires -Version 5.1
<#
muze first-time deployment (Windows): dependencies, config bootstrap, optional
logon auto-start, then start the service via scripts\start.ps1.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Extras rag-local
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -InstallTask    # + auto-start at logon
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -NoStart       # install only

Update (never touches the running service) / lifecycle:
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Stop | -Restart]
#>
param(
    [string]$Config = "config.yaml",
    [string]$Extras = "",         # comma-separated uv extras, e.g. "rag-local"
    [switch]$InstallTask,         # register a logon scheduled task that starts muze
    [switch]$NoStart              # do not start the service now
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
$TaskName = "muze-autostart"

function Write-Step($msg) { Write-Host "[muze-deploy] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[muze-deploy] $msg" -ForegroundColor Green }

# ---------------------------------------------------------------- prerequisites
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $Repo ".git"))) {
    throw "git not found in PATH (needed for code updates)"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found in PATH; install it from https://docs.astral.sh/uv/"
}
Write-Step "prerequisites ok (git: $((Get-Command git).Source), uv: $((Get-Command uv).Source))"

# ---------------------------------------------------------------- dependencies
$extraList = @($Extras -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$uvArgs = @("sync")
foreach ($e in $extraList) { $uvArgs += @("--extra", $e) }
Write-Step "uv $($uvArgs -join ' ')"
Push-Location $Repo
try {
    & uv @uvArgs
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
} finally {
    Pop-Location
}
if (-not (Test-Path $Py)) { throw "venv python still missing: $Py" }

# ---------------------------------------------------------------- config
$CfgAbs = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Repo $Config }
if (-not (Test-Path $CfgAbs)) {
    Copy-Item (Join-Path $Repo "config.example.yaml") $CfgAbs
    Write-Warning "created $CfgAbs from config.example.yaml"
    Write-Warning "edit it (milky.base_url/access_token, control.controller, llm.chat.*) then run scripts\update.ps1 to start"
    exit 3
}

# ---------------------------------------------------------------- auto-start task
if ($InstallTask) {
    $startScript = Join-Path $PSScriptRoot "start.ps1"
    # -Background matters: the foreground form ties the daemon to a hidden
    # console that Windows ends together with the task (0x41306) and leaves
    # no log behind — a logon start then fails completely silently (live
    # incident 2026-10-23). -Background detaches, redirects daemon logs and
    # verifies startup before the script exits.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -Background"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Write-Ok "scheduled task '$TaskName' registered: muze starts at logon (start.ps1 -Background)"
    # watchdog: start.ps1 is idempotent ("already running" -> exit 0), so a
    # 5-minute repeat revives a killed/crashed daemon even though the user
    # session (and therefore the logon trigger) never fires again
    $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName "$TaskName-watch" -Action $action `
        -Trigger $watchTrigger -Settings $settings -Force | Out-Null
    Write-Ok "scheduled task '$TaskName-watch' registered: every 5 min, starts muze if it is down"
    Write-Step "remove them later with: Unregister-ScheduledTask -TaskName $TaskName,'$TaskName-watch' -Confirm:`$false"
}

# remember the extras for future update.ps1 runs (uv sync is exact)
if ($extraList) {
    $extrasFile = Join-Path $Repo "data\.update-extras"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $extrasFile) | Out-Null
    Set-Content -Path $extrasFile -Value ($extraList -join ',') -Encoding ASCII
}

# ---------------------------------------------------------------- start
if ($NoStart) {
    Write-Ok "install complete; start with: powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
    exit 0
}

Write-Step "starting the service via start.ps1 ..."
$startScript = Join-Path $PSScriptRoot "start.ps1"
$startArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $startScript, "-Config", $CfgAbs)
& powershell.exe @startArgs
exit $LASTEXITCODE
