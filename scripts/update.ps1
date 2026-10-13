#Requires -Version 5.1
<#
muze update: update code + dependencies. NEVER touches the running service
(no stop, no start, no restart). Flipping the service to the new code is
yours: send /restart (console / WebUI / `muze cli`), or use scripts\start.ps1
for lifecycle (start / -Stop / -Restart).

Usage (from anywhere, absolute paths are derived):
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1            # git: latest tag
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Tag v0.1.0-alpha.57
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Extras rag-local
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -SkipPull  # deps only (no git)
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel C:\dist\copilot_muze-0.1.0a57-py3-none-any.whl
  powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Wheel https://host/xxx.whl -Extras rag-local

Notes:
- `uv sync` runs while the service may still be running. Pure-python updates
  are fine (already-loaded .py files are not locked), but a NATIVE dependency
  update can hit a locked DLL. If that happens: scripts\start.ps1 -Stop,
  rerun this script, then scripts\start.ps1.
- Extras are remembered in data\.update-extras: the first -Extras rag-local
  sticks, later plain runs keep it (uv sync is exact and would otherwise
  uninstall them). Pass -Extras again to change.
- git checkout --force DISCARDS local edits to tracked files. A deployment
  checkout must not carry code changes; config.yaml and data/ are untracked
  and never touched.
- -Wheel installs a built wheel (uv build) into the venv instead of git
  pulling -- handy when the box has no git access. The wheel contains ONLY
  the muze package: scripts\ and the repo-level skills\ are not inside, so
  keep this folder (scripts) from the original checkout and let skills live
  in data\skills. -Tag/-SkipPull are meaningless with -Wheel.
  WARNING (wheel mode): the local checkout's pyproject/uv.lock stay at the
  old version, so the script PARKS them as *.wheeloff after installing the
  wheel. That makes plain 'uv run muze ...' safe (no project -> no implicit
  sync -> .venv used as-is). Git-mode updates restore them automatically.
#>
param(
    [string]$Tag = "",          # tag to deploy; empty = latest semver tag
    [string]$Extras = "",       # comma-separated uv extras, e.g. "rag-local"; remembered between runs
    [switch]$SkipPull,          # skip fetch/checkout
    [string]$Wheel = "",        # local .whl path or URL; replaces git+uv sync entirely
    [string]$Config = "config.yaml"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
$ExtrasFile = Join-Path $Repo "data\.update-extras"

function Write-Step($msg)  { Write-Host "[muze-update] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[muze-update] $msg" -ForegroundColor Green }

if (-not (Test-Path $Py)) { throw "venv python not found: $Py (run scripts\deploy.ps1 first)" }

$CfgAbs = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Repo $Config }
$cfgBase = [IO.Path]::GetFileName($CfgAbs)

function Get-MuzeProc([switch]$All) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' or Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+muze\s+(daemon|serve)' }
    if ($All) { return @($procs) }
    @($procs | Where-Object { $_.CommandLine.Contains($Py) -or $_.CommandLine.Contains($Repo) })
}

# extras: explicit -Extras wins (and is remembered); otherwise reuse the last
$extraList = @($Extras -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($PSBoundParameters.ContainsKey('Extras')) {
    $dir = Split-Path -Parent $ExtrasFile
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Set-Content -Path $ExtrasFile -Value (($extraList -join ',') -as [string]) -Encoding ASCII
} elseif (Test-Path $ExtrasFile) {
    $saved = @(Get-Content $ExtrasFile -Raw | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($saved) {
        $extraList = @($saved -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        Write-Step "reusing extras from data\.update-extras: $($extraList -join ',')"
    }
}

$oldVersion = & $Py -m muze version
Write-Step "current version: $oldVersion"
$wasRunning = @(Get-MuzeProc).Count -gt 0

# ---------------------------------------------------------------- update code
if ($Wheel) {
    Write-Step "code update via wheel: $Wheel (git skipped)"
} elseif (-not $SkipPull) {
    if (Test-Path (Join-Path $Repo ".git")) {
        Write-Step "git fetch --tags..."
        # NB: no 2>&1 on native git calls -- under PS 5.1 + EAP=Stop a
        # redirected stderr line becomes an ErrorRecord that kills the
        # script mid-update (git checkout's "HEAD is now at ..." goes to
        # stderr). Unredirected, stderr just prints.
        git -C $Repo fetch --tags --force --prune
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
        # coming back from wheel mode: drop parked project files so the
        # checkout --force below restores the real ones
        foreach ($f in @("pyproject.toml", "uv.lock")) {
            Remove-Item (Join-Path $Repo "$f.wheeloff") -Force -ErrorAction SilentlyContinue
        }

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
        git -C $Repo checkout --force $Tag
        if ($LASTEXITCODE -ne 0) { throw "git checkout $Tag failed" }
    } else {
        Write-Warning "not a git checkout; skipping code update (deps only)"
    }
}

# ---------------------------------------------------------------- deps
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
    if ($LASTEXITCODE -ne 0) {
        throw "wheel install failed -- likely a locked .venv file (os error 5): a service started via 'uv run muze ...' or muze.exe keeps that exe locked. Recover with: scripts\start.ps1 -Stop, rerun this script, then scripts\start.ps1 (updates while running are normally fine when the service was started by start.ps1, which uses 'python -m muze')"
    }
    # de-project the deployment so plain `uv run muze ...` is safe: uv run
    # syncs implicitly whenever it finds a pyproject.toml, which would revert
    # this wheel back to the stale checkout version. With the project files
    # renamed away, uv run just uses .venv as-is. Git-mode updates restore
    # them via checkout --force (they are tracked).
    foreach ($f in @("pyproject.toml", "uv.lock")) {
        $p = Join-Path $Repo $f
        if (Test-Path $p) {
            Move-Item $p "$p.wheeloff" -Force
            Write-Step "parked $f -> $f.wheeloff (uv run safe)"
        }
    }
} else {
    $uvArgs = @("sync")
    foreach ($e in $extraList) { $uvArgs += @("--extra", $e) }
    Write-Step "uv $($uvArgs -join ' ')"
    Push-Location $Repo
    try {
        & uv @uvArgs
        if ($LASTEXITCODE -ne 0) {
            if ($wasRunning) {
                throw "uv sync failed -- possibly a DLL locked by the running service: scripts\start.ps1 -Stop, rerun update, then scripts\start.ps1"
            }
            throw "uv sync failed"
        }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------- done (no restart, ever)
$newVersion = & $Py -m muze version
Write-Ok "updated on disk: $oldVersion -> $newVersion"
if ($wasRunning) {
    Write-Ok "the RUNNING service still uses the old code; flip it with /restart (console / WebUI / muze cli) or scripts\start.ps1 -Restart"
} else {
    Write-Step "service is not running; start it with: scripts\start.ps1"
}
exit 0
