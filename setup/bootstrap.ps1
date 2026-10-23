# bootstrap.ps1 -- setup helper used by the launcher and by the in-app installer.
#
# Two very different modes:
#
#   (no switch)   Launch mode: make sure the *main* window environment exists
#                 (.venv + PyQt5 + mido + pretty_midi). It never installs the
#                 AI stack and never downloads model weights, so double-clicking
#                 the launcher cannot start a multi-GB download.
#
#   -Force        Full install / repair: main env, AI inference env, model
#                 weights, patched model code. Used by the in-app
#                 "one-click install" and by setup\ one-click bat.
#
# This file is intentionally ASCII-only.

param([switch]$Force)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PS = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Invoke-PsFile([string]$Script, [string[]]$Arguments = @()) {
    & $PS -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments | Out-Host
    return $LASTEXITCODE
}

$Heal = Join-Path $PSScriptRoot 'heal_venv.ps1'
if (Test-Path $Heal) { Invoke-PsFile $Heal | Out-Null }

$MainPy = Join-Path $Root '.venv\Scripts\python.exe'
$Setup = Join-Path $PSScriptRoot 'setup_env.ps1'

# ------------------------------------------------------------------ launch mode
if (-not $Force) {
    Write-Host '[bootstrap] checking the main window environment...'
    $rc = Invoke-PsFile $Setup @('-SkipSheetSage2')
    if ($rc -ne 0) {
        Write-Host '[bootstrap] main Python environment setup failed.'
        Write-Host '  See logs\setup_env.log for details.'
        exit $rc
    }
    if (Test-Path $Heal) { Invoke-PsFile $Heal | Out-Null }
    if (Test-Path $MainPy) {
        & $MainPy -c "import PyQt5, mido, pretty_midi" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '[bootstrap] main environment import check failed.'
            exit 1
        }
    }
    Write-Host '[bootstrap] main environment ready.'
    exit 0
}

# ------------------------------------------------------------------ full install
Write-Host '[bootstrap] full install: Python environments...'
$rc = Invoke-PsFile $Setup
if ($rc -ne 0) {
    Write-Host '[bootstrap] Python environment setup failed.'
    Write-Host '  See logs\setup_env.log for details.'
    exit $rc
}

if (Test-Path $Heal) { Invoke-PsFile $Heal | Out-Null }

# Put the project's model code patches back (fast incremental decoding +
# hardware routing); model repos / mirrors ship the slower official files.
$PatchDir = Join-Path $Root 'vendor\sheetsage2\patched_model_py'
$ModelDir = Join-Path $Root 'vendor\sheetsage2\models\SheetSage2'
if ((Test-Path $PatchDir) -and (Test-Path $ModelDir)) {
    $n = 0
    Get-ChildItem -LiteralPath $PatchDir -Filter '*.py' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ModelDir $_.Name) -Force
        $n++
    }
    Write-Host ('[bootstrap] restored ' + $n + ' patched model code file(s)')
}

$Models = @(
    (Join-Path $Root 'vendor\sheetsage2\models\SheetSage2\model.safetensors'),
    (Join-Path $Root 'vendor\sheetsage2\models\MERT-v2-FullSong\model.safetensors')
)
$Missing = @($Models | Where-Object { -not (Test-Path $_) })
if ($Missing.Count -gt 0) {
    Write-Host '[bootstrap] model weights missing: downloading (~2.6 GB, no login needed)...'
    $rc = Invoke-PsFile (Join-Path $PSScriptRoot 'fetch_models.ps1')
    if ($rc -ne 0) {
        Write-Host ''
        Write-Host '[bootstrap] model download failed.'
        Write-Host '  Check the network, then run setup\fetch_models.ps1 again.'
        Write-Host '  If the mirror is blocked, run the fallback login once:'
        Write-Host '    vendor\sheetsage2\.venv\Scripts\huggingface-cli.exe login'
        exit $rc
    }
}

if (Test-Path $MainPy) {
    & $MainPy -c "import PyQt5, mido, pretty_midi" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[bootstrap] main environment import check failed.'
        exit 1
    }
}

Write-Host '[bootstrap] environment ready.'
exit 0
