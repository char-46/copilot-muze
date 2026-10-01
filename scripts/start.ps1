#Requires -Version 5.1
<#
muze service lifecycle (start / stop / restart). update.ps1 never touches the
service -- this script is the only one that does, and YOU run it.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1           # start (if not running)
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop     # stop daemon+serve (hard kill)
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Restart  # stop, then start

Notes:
- Stop is a hard kill (Windows cannot Ctrl-C a process attached to another
  console). muze is built crash-safe: SQLite WAL + per-conversation roaming
  watermarks, and the startup pull recovers anything missed while down.
  For a code/config flip WITHOUT downtime prefer /restart (console / WebUI /
  `muze cli`), which re-execs serve in place (or rc=100 relaunch via daemon).
#>
param(
    [switch]$Stop,
    [switch]$Restart,
    [string]$Config = "config.yaml"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"

function Write-Step($msg)  { Write-Host "[muze-start] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[muze-start] $msg" -ForegroundColor Green }

if (-not (Test-Path $Py)) { throw "venv python not found: $Py (run scripts\deploy.ps1 first)" }

$CfgAbs = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Repo $Config }
$cfgBase = [IO.Path]::GetFileName($CfgAbs)

function Get-MuzeProc([switch]$All) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' or Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+muze\s+(daemon|serve)' }
    if ($All) { return @($procs) }
    @($procs | Where-Object { $_.CommandLine.Contains($Py) -or $_.CommandLine.Contains($Repo) })
}

# ---------------------------------------------------------------- stop
if ($Stop -or $Restart) {
    $running = @(Get-MuzeProc)
    if ($running.Count -gt 0) {
        Write-Step "stopping muze (daemon first; the job object reaps serve with it)..."
        $running |
            Where-Object { $_.CommandLine -match '-m\s+muze\s+daemon' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 3
        @(Get-MuzeProc) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
        Write-Ok "stopped ($($running.Count) process(es))"
    } else {
        Write-Step "muze is not running"
    }
    # anything else serving OUR config (e.g. a serve started with another
    # python) would keep the web port / DB and wedge the relaunch.
    $orphans = @(Get-MuzeProc -All | Where-Object { $_.CommandLine -match [regex]::Escape($cfgBase) })
    if ($orphans.Count -gt 0) {
        foreach ($o in $orphans) {
            Write-Warning "stopping process using this config outside the venv: pid $($o.ProcessId): $($o.CommandLine)"
            Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
    }
    if ($Stop) { exit 0 }
}

# ---------------------------------------------------------------- start
if (@(Get-MuzeProc).Count -gt 0) {
    Write-Ok "muze is already running"
    exit 0
}

if (-not (Test-Path $CfgAbs)) {
    throw "config not found: $CfgAbs (copy config.example.yaml to config.yaml and edit it)"
}

$logDir = Join-Path $Repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir "daemon.out.log"
$errLog = Join-Path $logDir "daemon.err.log"

Write-Step "starting muze daemon (hidden)..."
$proc = Start-Process -FilePath $Py `
    -ArgumentList @("-m", "muze", "daemon", "-c", $CfgAbs) `
    -WorkingDirectory $Repo -WindowStyle Hidden `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
Write-Step "daemon pid $($proc.Id)"

# ---------------------------------------------------------------- verify
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        Write-Warning "daemon exited early (code $($proc.ExitCode)); last log lines:"
        Get-Content $errLog -Tail 20 -ErrorAction SilentlyContinue | Write-Host
        Get-Content $outLog -Tail 20 -ErrorAction SilentlyContinue | Write-Host
        exit 1
    }
    $serve = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ParentProcessId -eq $proc.Id }
    if ($serve) {
        $tail = (Get-Content $outLog -Tail 60 -ErrorAction SilentlyContinue) -join "`n"
        if ($tail -match "logged in as|running \d+ account") { $ok = $true; break }
    }
}

if ($ok) {
    Write-Ok "muze is up"
    Get-Content $outLog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    exit 0
} else {
    Write-Warning "service is still starting after 60s; inspect data\logs\daemon.out.log and muze.log"
    exit 1
}
