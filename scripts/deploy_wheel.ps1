<#deploy_wheel.ps1 -- operator-side wheel deployment to the live box.

Automates the exact manual procedure used since alpha.136:
  1. (optionally) build the wheel offline
  2. scp it to the box through the relay jump host
  3. offline reinstall into the box venv (uv pip --reinstall --no-deps --offline)
  4. ask the box's OWN webui /restart API to flip the process (never kill)
  5. verify /api/info reports the expected version

The ONLY restart mechanism used here is the box's /restart command (the
owner-sanctioned path); this script never stops or kills any process.
The DB schema migrates automatically on boot.

Usage:
  powershell -File scripts\deploy_wheel.ps1                  # latest version
  powershell -File scripts\deploy_wheel.ps1 -Version 0.1.0a143
  powershell -File scripts\deploy_wheel.ps1 -SkipBuild       # wheel exists

NOTE: pure ASCII on purpose (Windows PowerShell 5.1 reads no-BOM as ANSI).
#>
param(
    [string]$Version = "",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"

$SshKey  = "$env:USERPROFILE\.ssh\id_ed25519"
$SshOpts = @("-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes")  # array: each token is its own argv
$Jump    = "root@hangzhou.aliyun.h-sr.cn"
$Box     = "11385@127.0.0.1"
$BoxPort = "18301"
$BoxDir  = "D:/copilot_muze"
$RelayApiBase = "http://127.0.0.1:18300/api"

# ---- 1. resolve version (src/muze/version.py -> pyproject form) ----------
if (-not $Version) {
    $verLine = Select-String -Path "src\muze\version.py" -Pattern 'BASE_VERSION = "(.+)"' |
        Select-Object -First 1
    if (-not $verLine) { throw "cannot read BASE_VERSION from src/muze/version.py" }
    $Version = $verLine.Matches[0].Groups[1].Value -replace '-alpha\.', 'a'
}
$WheelName = "copilot_muze-$Version-py3-none-any.whl"
$Wheel = "dist\$WheelName"
Write-Host "[deploy] version=$Version wheel=$WheelName"

if (-not (Test-Path $Wheel)) {
    if ($SkipBuild) { throw "wheel $Wheel not found and -SkipBuild given" }
    Write-Host "[deploy] building offline..."
    uv build --offline | Out-Null
    if (-not (Test-Path $Wheel)) { throw "build did not produce $Wheel" }
}

# ---- 2. copy to the box through the relay --------------------------------
Write-Host "[deploy] scp wheel -> box"
$scpOk = $false
foreach ($attempt in 1..2) {
    scp @SshOpts -i $SshKey -J $Jump -P $BoxPort "$Wheel" "${Box}:$BoxDir/dist/"
    if ($LASTEXITCODE -eq 0) { $scpOk = $true; break }
    Write-Host "[deploy] scp attempt $attempt failed; retrying in 5s"
    Start-Sleep -Seconds 5
}
if (-not $scpOk) { throw "scp failed" }

# ---- 3. offline reinstall in the box venv --------------------------------
$inner = @"
Set-Location $BoxDir
& "`$env:USERPROFILE\scoop\shims\uv.exe" pip install --python .venv\Scripts\python.exe --reinstall --no-deps --offline dist\$WheelName 2>`$null | Out-Null
"rc=`$LASTEXITCODE"
"@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
Write-Host "[deploy] installing wheel on box"
$out = ssh $SshOpts -i $SshKey -J $Jump -p $BoxPort $Box "powershell -NoProfile -EncodedCommand $enc" 2>$null |
    Where-Object { $_ -match '^rc=' }
if ($out -ne 'rc=0') { throw "box install failed: $out" }
Write-Host "[deploy] install ok"

# ---- 4. flip via the box's own /restart API ------------------------------
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"text":"/restart"}'))
Write-Host "[deploy] requesting /restart (graceful flip)"
$null = ssh -o BatchMode=yes $Jump "echo $b64 | base64 -d | curl -s -m 20 -X POST '$RelayApiBase/command?token=copilot' --data @- -H Content-Type:application/json"

# ---- 5. wait and verify ---------------------------------------------------
Write-Host "[deploy] waiting 85s for boot + schema migration..."
Start-Sleep -Seconds 85
$info = ssh -o BatchMode=yes $Jump "curl -s -m 12 '$RelayApiBase/info?token=copilot'" 2>$null |
    Select-Object -First 1
if (-not $info) { throw "no /api/info response" }
if ($info -notmatch [regex]::Escape($Version)) {
    throw "version mismatch after restart: $info"
}
Write-Host "[deploy] OK -- box runs $Version"
Write-Host $info.Substring(0, [Math]::Min(120, $info.Length))
