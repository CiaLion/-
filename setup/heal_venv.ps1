# heal_venv.ps1 -- make both virtual environments follow the current folder.
#
# Runs before every launch (called by the .bat / .vbs launchers) and by
# Called by the launcher .bat. It rewrites .venv\pyvenv.cfg and
# vendor\sheetsage2\.venv\pyvenv.cfg so they point at the portable
# Python 3.11 runtime shipped inside this folder:
#
#     <project>\runtime\python311
#
# When the whole folder is moved to D:\... or another computer, the next
# launch fixes both configs automatically -- no manual editing.
#
# This file is intentionally ASCII-only (PowerShell 5.1 + non-BOM UTF-8 would
# garble Chinese text).

$ErrorActionPreference = 'SilentlyContinue'

$Root    = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root 'runtime\python311'
$Py      = Join-Path $Runtime 'python.exe'

if (-not (Test-Path $Py)) {
    Write-Host '[heal] portable runtime not found, skip (run setup first)'
    exit 0
}

$DllNames = @('python311.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
$Cfg = "home = $Runtime`r`n" +
       "include-system-site-packages = false`r`n" +
       "version = 3.11.9`r`n" +
       "executable = $Py`r`n"

foreach ($Rel in @('.venv', 'vendor\sheetsage2\.venv')) {
    $Venv = Join-Path $Root $Rel
    $VenvPy = Join-Path $Venv 'Scripts\python.exe'
    if (-not (Test-Path $VenvPy)) { continue }

    foreach ($Dll in $DllNames) {
        $Src = Join-Path $Runtime $Dll
        if (Test-Path $Src) {
            Copy-Item -LiteralPath $Src -Destination (Join-Path $Venv 'Scripts\' + $Dll) -Force
        }
    }

    $CfgPath = Join-Path $Venv 'pyvenv.cfg'
    [System.IO.File]::WriteAllText($CfgPath, $Cfg, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ('[heal] ' + $Rel + ' -> ' + $Runtime)
}

exit 0
