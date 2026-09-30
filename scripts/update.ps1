#Requires -Version 5.1
<#
muze one-click update: stop service -> update code -> sync deps -> start -> verify.

Usage (from anywhere, absolute paths are derived):
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1            # git: latest tag
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Tag v0.1.0-alpha.56
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Extras rag-local
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -SkipPull  # restart only
                                                      ^ also used by the logon auto-start task
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel C:\dist\muze-0.1.0a57-py3-none-any.whl
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel https://host/muze-0.1.0a57-py3-none-any.whl -Extras rag-local

Notes:
- Stop is a hard kill (Windows cannot Ctrl-C a process attached to another
  console). muze is built crash-safe: SQLite WAL + per-conversation roaming
  watermarks, and the startup pull recovers anything missed while down.
- git checkout --force DISCARDS local edits to tracked files. A deployment
  checkout must not carry code changes; config.yaml and data/ are untracked
  and never touched.
- -Wheel installs a built wheel (uv build) into the venv instead of git
  pulling -- handy when the box has no git access. The wheel contains ONLY
  the muze package: scripts\ and the repo-level skills\ are not inside, so
  keep this folder (scripts) from the original checkout and let skills live
  in data\skills. -Tag/-SkipPull are meaningless with -Wheel.
#>
param(
    [string]$Tag = "",          # tag to deploy; empty = latest semver tag
    [string]$Extras = "",       # comma-separated uv extras, e.g. "rag-local" / "rag-local,rag-onnx"
    [switch]$SkipPull,          # skip fetch/checkout (restart-only)
    [string]$Wheel = "",        # local .whl path or URL; replaces git+uv sync entirely
    [string]$Config = "config.yaml"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"

function Write-Step($msg)  { Write-Host "[muze-update] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[muze-update] $msg" -ForegroundColor Green }

if (-not (Test-Path $Py)) { throw "venv python not found: $Py (run scripts\deploy.ps1 first)" }

$CfgAbs = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Repo $Config }
$cfgBase = [IO.Path]::GetFileName($CfgAbs)

# ---------------------------------------------------------------- stop
# Windows cannot Ctrl-C a process attached to another console, so stopping is
# a hard kill. muze is built crash-safe: SQLite WAL + per-conversation roaming
# watermarks, and the startup pull recovers anything missed while down.
function Get-MuzeProc([switch]$All) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' or Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+muze\s+(daemon|serve)' }
    if ($All) { return @($procs) }
    @($procs | Where-Object { $_.CommandLine.Contains($Py) -or $_.CommandLine.Contains($Repo) })
}

$oldVersion = & $Py -m muze version
Write-Step "current version: $oldVersion"

$running = Get-MuzeProc
if ($running.Count -gt 0) {
    Write-Step "stopping muze (daemon first; the job object reaps serve with it)..."
    $running |
        Where-Object { $_.CommandLine -match '-m\s+muze\s+daemon' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    # repo-scoped serve leftovers (e.g. re-exec'd before the daemon existed)
    Get-MuzeProc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Write-Ok "stopped ($($running.Count) process(es))"
} else {
    Write-Step "muze is not running"
}

# anything else serving OUR config (e.g. a serve started with another python)
# would keep the web port / DB and wedge the relaunch -- stop it too.
$orphans = Get-MuzeProc -All | Where-Object { $_.CommandLine -match [regex]::Escape($cfgBase) }
if ($orphans.Count -gt 0) {
    foreach ($o in $orphans) {
        Write-Warning "stopping process using this config outside the venv: pid $($o.ProcessId): $($o.CommandLine)"
        Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------- update code
if ($Wheel) {
    Write-Step "code update via wheel: $Wheel (git skipped)"
} elseif (-not $SkipPull) {
    if (Test-Path (Join-Path $Repo ".git")) {
        Write-Step "git fetch --tags..."
        git -C $Repo fetch --tags --force --prune 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

        if (-not $Tag) {
            # latest tag by semver order (release > rc > beta > alpha)
            $Tag = git -C $Repo tag --list "v*" | ForEach-Object {
                if ($_ -match '^v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$') {
                    $pre = $Matches[4]
                    $preN = if ($Matches[5]) { [int]$Matches[5] } else { 0 }
                    $rank = if (-not $pre) { "z" }
                            elseif ($pre -eq "alpha") { "a" }
                            elseif ($pre -eq "beta")  { "b" }
                            else { "r" }
                    [pscustomobject]@{
                        T = $_
                        K = "{0:D5}.{1:D5}.{2:D5}.{3}{4:D5}" -f `
                            [int]$Matches[1], [int]$Matches[2], [int]$Matches[3], $rank, $preN
                    }
                }
            } | Sort-Object K | Select-Object -Last 1 | ForEach-Object { $_.T }
            if (-not $Tag) { throw "no v* tags found in the repository" }
        }
        Write-Step "git checkout --force $Tag (local edits to tracked files are discarded)"
        git -C $Repo checkout --force $Tag 2>&1 | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "git checkout $Tag failed" }
    } else {
        Write-Warning "not a git checkout; skipping code update (sync + restart only)"
    }
}

# ---------------------------------------------------------------- deps
$extraList = @($Extras -split '[,，]' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($Wheel) {
    # install the wheel (with optional extras via a PEP 508 direct reference)
    $spec = $Wheel
    if ($extraList) {
        $uri = $Wheel
        if ($Wheel -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') {
            if (-not (Test-Path $Wheel)) { throw "wheel not found: $Wheel" }
            $uri = [Uri]::new((Resolve-Path $Wheel).Path).AbsoluteUri
        }
        $spec = "copilot-muze[$($extraList -join ',')] @ $uri"
    }
    Write-Step "uv pip install --reinstall $spec"
    & uv pip install --python $Py --reinstall $spec
    if ($LASTEXITCODE -ne 0) { throw "wheel install failed" }
} else {
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
}

# ---------------------------------------------------------------- start
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
$newVersion = & $Py -m muze version
Write-Step "new version: $newVersion"
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
    Write-Ok "updated and running: $oldVersion -> $newVersion"
    Get-Content $outLog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    exit 0
} else {
    Write-Warning "service is still starting after 60s; inspect data\logs\daemon.out.log and muze.log"
    exit 1
}
