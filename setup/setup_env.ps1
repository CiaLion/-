# setup_env.ps1 -- create / verify the two Python virtual environments.
#
#   .venv                          main program  (main.py, gui_main.py, player, transcriber)
#   vendor\sheetsage2\.venv        SheetSage2    (torch + transformers + librosa + imageio-ffmpeg)
#
# This file is 100% pure ASCII so Windows PowerShell 5.1 never garbles it.

[CmdletBinding()]
param(
    [ValidateSet('auto', 'xpu', 'cuda', 'rocm', 'cpu')]
    [string]$Backend = 'auto',
    [string]$TorchIndex = '',
    [switch]$Check,
    [switch]$Recreate,
    [switch]$SkipMain,
    [switch]$SkipSheetSage2,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PYTHONNOUSERSITE = '1'
# CRITICAL: Clear inherited PYTHONPATH from parent GUI process so .venv (numpy 2.1)
# never leaks into vendor\sheetsage2\.venv (numpy 1.26 / numba)!
Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue

$Root     = Split-Path -Parent $PSScriptRoot
$MainVenv = Join-Path $Root '.venv'
$S2Venv   = Join-Path $Root 'vendor\sheetsage2\.venv'
$MainReq  = Join-Path $Root 'requirements.txt'
$S2Req    = Join-Path $Root 'vendor\sheetsage2\requirements-base.txt'
$S2SepReq = Join-Path $Root 'vendor\sheetsage2\requirements-sep.txt'
$script:CudaMajor  = 0
$script:RocmRepo   = 'https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/'
$script:PyPIMirror = $null
$script:SavedProxy = $null
$script:TorchHint  = [regex]::Unescape('[\u63d0\u793a] \u6b63\u5728\u4ece\u56fd\u5185\u9ad8\u901f\u955c\u50cf\u4e0b\u8f7d\u663e\u5361\u63a8\u7406\u6838\u5fc3 (\u7ea6 2.4 GB)\uff0c\u8bf7\u52ff\u7528\u9f20\u6807\u5de6\u952e\u70b9\u51fb\u9ed1\u6846\u5185\u90e8...')

$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$SetupLog = Join-Path $LogDir 'setup_env.log'

function Write-LogLine([string]$Text) {
    try { Add-Content -LiteralPath $SetupLog -Value $Text -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
}
function Stop-SetupLog { }

Write-Host ('[setup] log file: ' + $SetupLog)
Write-LogLine ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] setup started')

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('=== ' + $Text) -ForegroundColor Cyan
    Write-LogLine ('=== ' + $Text)
}
function Write-Ok([string]$Text) {
    Write-Host ('  [ok]   ' + $Text) -ForegroundColor Green
    Write-LogLine ('[ok] ' + $Text)
}
function Write-Warn2([string]$Text) {
    Write-Host ('  [warn] ' + $Text) -ForegroundColor Yellow
    Write-LogLine ('[warn] ' + $Text)
}
function Write-Bad([string]$Text) {
    Write-Host ('  [miss] ' + $Text) -ForegroundColor Red
    Write-LogLine ('[miss] ' + $Text)
}

# ---------------------------------------------------------------- network probe
function Test-UrlHead([string]$Url, [int]$TimeoutSec = 2) {
    $params = @{
        Uri                = $Url
        Method             = 'Head'
        UseBasicParsing    = $true
        TimeoutSec         = $TimeoutSec
        MaximumRedirection = 5
        ErrorAction        = 'Stop'
    }
    $envProxy = $env:HTTPS_PROXY
    if (-not $envProxy) { $envProxy = $env:HTTP_PROXY }
    if (-not $envProxy) { $envProxy = $env:ALL_PROXY }
    if ($envProxy) { $params['Proxy'] = $envProxy }
    try {
        $resp = Invoke-WebRequest @params
        $code = 0
        try { $code = [int]$resp.StatusCode } catch { }
        return ($code -ge 200 -and $code -lt 400)
    } catch {
        $r = $null
        try { $r = $_.Exception.Response } catch { }
        if ($r -ne $null) {
            $code = 0
            try { $code = [int]$r.StatusCode } catch { }
            if ($code -eq 405) { return $true }
        }
        return $false
    }
}

function Test-ProxyEnvSet {
    foreach ($n in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')) {
        if (Test-Path ('env:' + $n)) { return $true }
    }
    return $false
}

function Clear-ProxyEnv {
    $saved = @{}
    foreach ($n in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')) {
        if (Test-Path ('env:' + $n)) {
            $saved[$n] = (Get-Item ('env:' + $n)).Value
            Remove-Item ('env:' + $n) -ErrorAction SilentlyContinue
        }
    }
    Write-Warn2 'proxy environment variables found; cleared for this run (dead proxy / accelerator)'
    Write-LogLine '[proxy] cleared HTTP_PROXY / HTTPS_PROXY for this run'
    return $saved
}

function Restore-ProxyEnv {
    if ($script:SavedProxy -eq $null) { return }
    foreach ($k in $script:SavedProxy.Keys) {
        try { Set-Item -Path ('env:' + $k) -Value $script:SavedProxy[$k] } catch { }
    }
    $script:SavedProxy = $null
}

function Get-PyPIMirrorList {
    return @(
        @{ Name = 'tsinghua'; Index = 'https://pypi.tuna.tsinghua.edu.cn/simple/';       Host = 'pypi.tuna.tsinghua.edu.cn' },
        @{ Name = 'tencent';  Index = 'https://mirrors.cloud.tencent.com/pypi/simple/';  Host = 'mirrors.cloud.tencent.com' },
        @{ Name = 'aliyun';   Index = 'https://mirrors.aliyun.com/pypi/simple/';         Host = 'mirrors.aliyun.com' },
        @{ Name = 'huawei';   Index = 'https://repo.huaweicloud.com/repository/pypi/simple/'; Host = 'repo.huaweicloud.com' }
    )
}

function Select-BestPyPIMirror {
    $list = Get-PyPIMirrorList
    $m0 = Test-UrlHead $list[0].Index
    $m1 = Test-UrlHead $list[1].Index
    if ((-not $m0) -and (-not $m1) -and (Test-ProxyEnvSet)) {
        $script:SavedProxy = Clear-ProxyEnv
        $m0 = Test-UrlHead $list[0].Index
        $m1 = Test-UrlHead $list[1].Index
    }
    if ($m0) {
        $best = $list[0]
    } elseif ($m1) {
        $best = $list[1]
    } else {
        $best = $null
        foreach ($m in @($list[2], $list[3])) {
            if (Test-UrlHead $m.Index) { $best = $m; break }
        }
        if (-not $best) {
            $best = @{ Name = 'pypi'; Index = 'https://pypi.org/simple/'; Host = 'pypi.org' }
        }
    }
    Write-Host ('  [pypi] mirror: ' + $best.Name + '  ' + $best.Index) -ForegroundColor DarkCyan
    Write-LogLine ('[pypi] selected mirror: ' + $best.Name + ' ' + $best.Index)
    return $best
}

function Get-TorchWheelCandidates([string]$subdir) {
    return @(
        @{ Name = 'nju';      Base = ('https://mirror.nju.edu.cn/pytorch/whl/' + $subdir + '/');      Host = 'mirror.nju.edu.cn' },
        @{ Name = 'sjtu';     Base = ('https://mirror.sjtu.edu.cn/pytorch-wheels/' + $subdir + '/');  Host = 'mirror.sjtu.edu.cn' },
        @{ Name = 'aliyun';   Base = ('https://mirrors.aliyun.com/pytorch-wheels/' + $subdir + '/');  Host = 'mirrors.aliyun.com' },
        @{ Name = 'official'; Base = ('https://download.pytorch.org/whl/' + $subdir + '/');           Host = 'download.pytorch.org' }
    )
}

function Select-BestTorchWheelUrl([string]$subdir) {
    $cands = Get-TorchWheelCandidates $subdir
    foreach ($c in $cands) {
        if (Test-UrlHead $c.Base) { return $c }
    }
    return $cands[-1]
}

function Get-TorchSubdir([string]$Which) {
    switch ($Which) {
        'cuda' { if ($script:CudaMajor -ge 10) { return 'cu128' } else { return 'cu121' } }
        'xpu'  { return 'xpu' }
        'rocm' { return 'rocm' }
        default { return 'cpu' }
    }
}

# ROCm wheels on Windows are published for Python 3.12 only, so the AI venv
# interpreter decides whether the AMD GPU can be used.
function Get-PythonMinorVersion([string]$Py) {
    try {
        $out = & $Py -E -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        return ([string]$out).Trim()
    } catch {
        return ''
    }
}

# ---------------------------------------------------------------- backend
function Get-DisplayAdapters {
    $names = @()

    # 1) WMI/CIM: primary, reliable, returns all video controllers
    try {
        $cim = Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop
        foreach ($c in $cim) {
            if ($c.Name) { $names += [string]$c.Name }
        }
    } catch { }

    # 2) Registry fallback (only when CIM returned nothing)
    if ($names.Count -eq 0) {
        try {
            $cls = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
            $names += Get-ChildItem $cls -ErrorAction Stop |
                      ForEach-Object { (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DriverDesc } |
                      Where-Object { $_ }
        } catch { }
        try {
            $video = 'HKLM:\SYSTEM\CurrentControlSet\Control\Video'
            $names += Get-ChildItem $video -ErrorAction Stop |
                      ForEach-Object {
                          Get-ChildItem $_.PSPath -ErrorAction SilentlyContinue |
                          ForEach-Object { (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DriverDesc }
                      } | Where-Object { $_ }
        } catch { }
    }

    return @($names | Where-Object { $_ } | Select-Object -Unique)
}

# Optional: drop obvious virtual display adapters before backend selection
function Get-PhysicalAdapters([string[]]$Names) {
    $virtual = 'ToDesk|Sunlogin|Oray|Parsec|Splashtop|AnyDesk|TeamViewer|Rainway|Moonlight|' +
           'Virtual Display|Indirect Display|IddCx|USB Display|DisplayLink|' +
           'VMware|VirtualBox|Hyper-V|Citrix|Basic Display|Remote Display'
    return @($Names | Where-Object { $_ -notmatch $virtual })
}

function Get-NvidiaSmiPath {
    foreach ($p in @(
        (Join-Path $env:WINDIR 'System32\nvidia-smi.exe'),
        (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    )) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    $cmd = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-NvidiaSmi { return [bool](Get-NvidiaSmiPath) }

function Get-NvidiaComputeMajor {
    $smi = Get-NvidiaSmiPath
    if (-not $smi) {
        $adapters = (@(Get-DisplayAdapters) -join ' ')
        if ($adapters -match 'RTX\s*50|Blackwell|B[12]000|GB[12]00') { return 12 }
        return 0
    }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $caps = & $smi --query-gpu=compute_cap --format=csv,noheader 2>$null
        foreach ($line in @($caps)) {
            if ($line -match '^\s*(\d+)\.') { return [int]$Matches[1] }
        }
    } catch { } finally {
        $ErrorActionPreference = $oldEap
    }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $names = & $smi --query-gpu=name --format=csv,noheader 2>$null
        if (($names -join ' ') -match 'RTX\s*50|Blackwell|B[12]000|GB[12]00') { return 12 }
    } catch { } finally {
        $ErrorActionPreference = $oldEap
    }
    return 0
}

function Select-Backend([string]$Adapters, [bool]$NvidiaSmi, [bool]$ZeLoader) {
    if ($Adapters -match 'NVIDIA|GeForce|Quadro|Tesla' -or $NvidiaSmi) { return 'cuda' }
    if ($Adapters -match 'Intel.*Arc|Arc\(TM\)') { return 'xpu' }
    # AMD cards go through ROCm. Windows wheels only exist for Python 3.12,
    # so Install-Torch checks the venv interpreter and falls back to CPU.
    if ($Adapters -match 'AMD|Radeon') { return 'rocm' }
    return 'cpu'
}

function Detect-Backend {
    $adapters = @(Get-DisplayAdapters)
    $joined = ($adapters -join ' | ')
    Write-LogLine ('[gpu] all adapters: ' + $joined)

    $physical = @(Get-PhysicalAdapters $adapters)
    if ($physical.Count -eq 0) {
        # All adapters were classified as virtual; don't lose info -- fall back to full list
        Write-LogLine ('[gpu] all adapters classified as virtual, falling back to full list')
        $physical = $adapters
    }
    $physJoined = ($physical -join ' | ')
    if ($physical.Count -gt 0 -and $physical.Count -ne $adapters.Count) {
        Write-LogLine ('[gpu] physical adapters: ' + $physJoined)
    }

    $nvidiaSmi = Test-NvidiaSmi
    $zeLoader = Test-Path (Join-Path $env:WINDIR 'System32\ze_loader.dll')

    # Prefer physical list for selection; nvidia-smi / ze_loader still act as fallbacks
    $which = Select-Backend $physJoined $nvidiaSmi $zeLoader

    if ($which -eq 'cpu') {
        if ($physJoined -match 'AMD|Radeon') {
            Write-Host ('  [gpu] AMD detected: ' + $physJoined + ' -> CPU')
        } else {
            Write-Host ('  [gpu] no discrete GPU detected: ' + $physJoined)
        }
        Write-LogLine ('[gpu] backend=cpu adapters=' + $physJoined)
    } elseif ($which -eq 'rocm') {
        Write-Host ('  [gpu] AMD detected: ' + $physJoined + ' -> ROCm (Python 3.12 required)')
        Write-LogLine ('[gpu] backend=rocm adapters=' + $physJoined)
    } else {
        Write-Host ('  [gpu] ' + $which + ' detected: ' + $physJoined)
        Write-LogLine ('[gpu] backend=' + $which + ' adapters=' + $physJoined)
    }
    return $which
}

function Get-TorchIndex([string]$Which) {
    if ($TorchIndex) { return $TorchIndex }
    switch ($Which) {
        'xpu'  { return 'https://download.pytorch.org/whl/xpu' }
        'cuda' {
            if ($script:CudaMajor -ge 10) { return 'https://download.pytorch.org/whl/cu128' }
            return 'https://download.pytorch.org/whl/cu121'
        }
        'rocm' { return $script:RocmRepo }
        default { return 'https://download.pytorch.org/whl/cpu' }
    }
}

function Ensure-PyPIMirror {
    if ($script:PyPIMirror -eq $null) {
        Write-Host '  selecting the fastest PyPI mirror (2s probe)...'
        $script:PyPIMirror = Select-BestPyPIMirror
    }
    return $script:PyPIMirror
}

function Install-WithMirrorFallback([string]$ExePath, [string[]]$Arguments, [string]$Label) {
    $list = Get-PyPIMirrorList
    $primary = Ensure-PyPIMirror
    $candidates = @($primary)
    foreach ($item in $list) {
        if ($item.Name -ne $primary.Name) { $candidates += $item }
    }
    foreach ($m in $candidates) {
        try {
            Invoke-Py $ExePath (@($Arguments) + @(
                '-i', $m.Index, '--trusted-host', $m.Host,
                '--timeout', '120', '--retries', '5', '--disable-pip-version-check'
            ))
            return
        } catch {
            Write-Warn2 ($Label + ': mirror ' + $m.Name + ' failed, trying next mirror')
        }
    }
    Invoke-Py $ExePath (@($Arguments) + @(
        '-i', 'https://pypi.org/simple',
        '--timeout', '120', '--retries', '5', '--disable-pip-version-check'
    ))
}

# ---------------------------------------------------------------- helpers
function Test-NativePythonCmd([string]$Exe, [string[]]$ArgsList) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @ArgsList -c "import sys" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Find-BasePython {
    $runtimePy  = Join-Path $Root 'runtime\python311\python.exe'
    $runtimeLib = Join-Path $Root 'runtime\python311\Lib\os.py'
    if ((Test-Path -LiteralPath $runtimePy) -and (Test-Path -LiteralPath $runtimeLib)) {
        return [pscustomobject]@{ Exe = $runtimePy; Args = @() }
    }
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        foreach ($ver in @('-3.11', '-3.12', '-3.10')) {
            if (Test-NativePythonCmd 'py' @($ver)) {
                return [pscustomobject]@{ Exe = 'py'; Args = @($ver) }
            }
        }
        if (Test-NativePythonCmd 'py' @('-3')) {
            return [pscustomobject]@{ Exe = 'py'; Args = @('-3') }
        }
    }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) { return [pscustomobject]@{ Exe = $py.Source; Args = @() } }
    return $null
}

function Get-PythonVersion($Py) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Py.Exe @($Py.Args) -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        return ($out | Select-Object -First 1)
    } catch {
        return 'unknown'
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Invoke-Py($ExePath, [string[]]$Arguments) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    try {
        & $ExePath @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($code -ne 0) {
        $msg = 'command failed (exit ' + $code + '): ' + $ExePath + ' ' + ($Arguments -join ' ')
        Write-LogLine ('[fail] ' + $msg)
        throw $msg
    }
}

function New-Venv($Py, [string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $site = Join-Path $Path 'Lib\site-packages'
        $ready = (Test-Path -LiteralPath (Join-Path $Path 'Scripts\python.exe')) -and
                 (Test-Path -LiteralPath $site) -and
                 (@(Get-ChildItem -LiteralPath $site -ErrorAction SilentlyContinue).Count -gt 0)
        if ((-not $Recreate) -and $ready) {
            Write-Ok ('venv exists: ' + $Path)
            return
        }
        if (-not $Recreate) {
            Write-Warn2 ('venv incomplete (empty site-packages), recreating: ' + $Path)
        } else {
            Write-Warn2 ('removing old venv: ' + $Path)
        }
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    Write-Host ('  creating venv: ' + $Path)
    Invoke-Py $Py.Exe (@($Py.Args) + @('-m', 'venv', $Path))
    Write-Ok ('venv created: ' + $Path)
}

function Install-Requirements([string]$VenvPython, [string]$ReqFile, [string]$Label) {
    if (-not (Test-Path -LiteralPath $ReqFile)) {
        Write-Warn2 ('requirements not found, skipped: ' + $ReqFile)
        return
    }
    Write-Host ('  installing ' + $Label + ' from ' + (Split-Path -Leaf $ReqFile))
        Install-WithMirrorFallback $VenvPython @('-m', 'pip', 'install', '--upgrade-strategy', 'only-if-needed', '-r', $ReqFile) $Label
    Write-Ok ($Label + ' dependencies installed')
}

function Test-Imports([string]$VenvPython, [string[]]$Modules) {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    $code = 'import ctypes; ctypes.windll.kernel32.SetErrorMode(0x8003); import ' + ($Modules -join ', ') + '; print(12345)'
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $VenvPython -E -c $code 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Test-S2BackendReady([string]$VenvPython, [string]$Which) {
    if (-not (Test-Imports $VenvPython @('torch', 'torchaudio', 'torchaudio.transforms', 'transformers', 'librosa', 'scipy', 'numba', 'imageio_ffmpeg'))) {
        return $false
    }
    if ($Which -eq 'cuda') {
        $code = 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)'
    } elseif ($Which -eq 'rocm') {
        $code = "import sys, torch; sys.exit(0 if (torch.cuda.is_available() and getattr(torch.version, 'hip', None)) else 1)"
    } elseif ($Which -eq 'xpu') {
        $code = "import sys, torch; sys.exit(0 if (hasattr(torch, 'xpu') and torch.xpu.is_available()) else 1)"
    } else {
        return $true
    }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $VenvPython -E -c $code 2>&1
        if ($LASTEXITCODE -ne 0) { Write-LogLine ('[probe-fail] ' + $Which + ': ' + ($out -join ' ')) }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Install-Torch([string]$VenvPython, [string]$Which, [string]$Label) {
    Write-Host ('  installing torch for backend "' + $Which + '" into ' + $Label)
    switch ($Which) {
        'cuda' {
            if ($script:CudaMajor -ge 10) {
                $packages = @('torch==2.7.0+cu128', 'torchaudio==2.7.0+cu128', 'torchvision==0.22.0+cu128')
            } else {
                $packages = @('torch==2.5.1+cu121', 'torchaudio==2.5.1+cu121', 'torchvision==0.20.1+cu121')
            }
        }
        'xpu' {
            $packages = @('torch==2.7.0+xpu', 'torchaudio==2.7.0+xpu', 'torchvision==0.22.0+xpu')
        }
        'rocm' {
            $pyver = Get-PythonMinorVersion $VenvPython
            Write-LogLine ('[torch] rocm request, venv python=' + $pyver)
            if ($pyver -eq '3.12') {
                $repo = $script:RocmRepo
                $packages = @(
                    ($repo + 'rocm_sdk_core-7.2.1-py3-none-win_amd64.whl'),
                    ($repo + 'rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl'),
                    ($repo + 'rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl'),
                    ($repo + 'torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl'),
                    ($repo + 'torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl'),
                    ($repo + 'torchvision-0.24.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl')
                )
                Write-Host '  AMD ROCm 7.2.1 (Windows): installing ROCm SDK + torch/torchaudio/torchvision'
                Write-Host '  (~2.5 GB download, AMD driver 26.2.2 or newer required)'
                Write-Host $script:TorchHint -ForegroundColor Yellow
                Invoke-Py $VenvPython (@('-m', 'pip', 'install', '--no-cache-dir',
                    '--progress-bar', 'on', '--disable-pip-version-check') + $packages)
                Write-Ok 'torch installed (rocm 7.2.1)'
                return
            }
            Write-Warn2 ('AMD ROCm wheels for Windows exist only for Python 3.12; this venv is ' + $pyver + '.')
            Write-Host '  -> installing the CPU build so transcription still works.'
            Write-Host '  -> for AMD GPU speed, rebuild the AI venv with Python 3.12, then run:'
            Write-Host '       setup\setup_env.ps1 -Backend rocm'
            Write-LogLine ('[torch] rocm unavailable on python ' + $pyver + ' -> cpu fallback')
            $Which = 'cpu'
            $packages = @('torch==2.5.1+cpu', 'torchaudio==2.5.1+cpu')
        }
        default {
            $packages = @('torch==2.5.1+cpu', 'torchvision==0.20.1+cpu', 'torchaudio==2.5.1+cpu')
        }
    }

    $pypi = Ensure-PyPIMirror
    $subdir = Get-TorchSubdir $Which
    $cands = Get-TorchWheelCandidates $subdir
    $best = $null
    foreach ($c in $cands) {
        if (Test-UrlHead $c.Base) { $best = $c; break }
    }
    if ($best -eq $null) { $best = $cands[-1] }
    Write-Host ('  wheel index: ' + $best.Name + '  ' + $best.Base)
    Write-LogLine ('[torch] selected index: ' + $best.Name + ' ' + $best.Base)

    if ($Which -eq 'cuda' -or $Which -eq 'xpu') {
        Write-Host ''
        Write-Host $script:TorchHint -ForegroundColor Yellow
        Write-Host ''
    }

    $order = @($best)
    foreach ($c in $cands) {
        if ($c.Base -ne $best.Base) { $order += $c }
    }
    foreach ($c in $order) {
        try {
            Write-Host ('  downloading from ' + $c.Name + ' ...')
            Write-LogLine ('[torch] trying ' + $c.Name + ' ' + $c.Base)
            $pipArgs = @(
                '-m', 'pip', 'install', '--progress-bar', 'on',
                '--disable-pip-version-check', '--retries', '5', '--timeout', '120'
            ) + $packages + @(
                '--index-url', $c.Base,
                '--extra-index-url', $pypi.Index,
                '--trusted-host', $c.Host,
                '--trusted-host', $pypi.Host
            )
            Invoke-Py $VenvPython $pipArgs
            Write-Ok ('torch installed (' + $Which + ', ' + $c.Name + ')')
            return
        } catch {
            Write-Warn2 ('torch download failed from ' + $c.Name + ', trying next mirror')
        }
    }

    if ($Which -eq 'cpu') {
        Write-Warn2 'cpu wheels not on the mirrors, falling back to the official PyTorch CPU index'
        Invoke-Py $VenvPython (@('-m', 'pip', 'install') + $packages + @(
            '--index-url', 'https://download.pytorch.org/whl/cpu',
            '--disable-pip-version-check', '--retries', '5', '--timeout', '120'
        ))
        Write-Ok ('torch installed (fallback, ' + $Which + ')')
        return
    }
    Write-Bad ('PyTorch ' + $Which + ' wheel download failed on every mirror.')
    Write-Host '  Check the network / proxy, then rerun this script.'
    Write-Host ('  Manual command: pip install ' + ($packages -join ' ') + ' --index-url https://download.pytorch.org/whl/' + $subdir)
    throw ('torch install failed (' + $Which + ')')
}

# ---------------------------------------------------------------- check mode
function Show-Status {
    $Which = $Backend
    if ($Which -eq 'auto') { $Which = Detect-Backend }
    if ($Which -eq 'cuda') { $script:CudaMajor = Get-NvidiaComputeMajor }

    Write-Step 'Environment status'
    Write-Host ('  detected backend: ' + $Which + '   (torch index: ' + (Get-TorchIndex $Which) + ')')
	
    $allAdapters = @(Get-DisplayAdapters)
    $physAdapters = @(Get-PhysicalAdapters $allAdapters)
    Write-Host ('  display adapters (' + $allAdapters.Count + ' total, ' + $physAdapters.Count + ' physical):')
    foreach ($a in $allAdapters) {
        if ($physAdapters -contains $a) {
            Write-Host ('    - ' + $a)
        } else {
            Write-Host ('    - ' + $a + '   [virtual]') -ForegroundColor DarkGray
        }
    }
	
    foreach ($item in @(
        @{ Name = 'main venv       '; Path = Join-Path $MainVenv 'Scripts\python.exe' },
        @{ Name = 'sheetsage2 venv '; Path = Join-Path $S2Venv 'Scripts\python.exe' }
    )) {
        if (Test-Path -LiteralPath $item.Path) {
            Write-Ok ($item.Name + ' : ' + $item.Path)
        } else {
            Write-Bad ($item.Name + ' : missing (' + $item.Path + ')')
        }
    }

    $mainPy = Join-Path $MainVenv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $mainPy) {
        foreach ($pack in @(@('pretty_midi'), @('mido'), @('numpy'), @('PyQt5'))) {
            if (Test-Imports $mainPy $pack) { Write-Ok ('main venv import: ' + $pack[0]) }
            else { Write-Warn2 ('main venv import failed: ' + $pack[0]) }
        }
    }

    $s2Py = Join-Path $S2Venv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $s2Py) {
        foreach ($pack in @(@('torch'), @('transformers'), @('librosa'), @('imageio_ffmpeg'), @('audio_separator'))) {
            if (Test-Imports $s2Py $pack) { Write-Ok ('sheetsage2 venv import: ' + $pack[0]) }
            else { Write-Warn2 ('sheetsage2 venv import failed: ' + $pack[0]) }
        }
    }

    Write-Step 'Models'
    foreach ($m in @(
        'vendor\sheetsage2\models\SheetSage2\model.safetensors',
        'vendor\sheetsage2\models\MERT-v2-FullSong\model.safetensors'
    )) {
        $full = Join-Path $Root $m
        if (Test-Path -LiteralPath $full) {
            $sizeGb = [math]::Round((Get-Item -LiteralPath $full).Length / 1GB, 2)
            Write-Ok ($m + '  (' + $sizeGb + ' GB)')
        } else {
            Write-Bad ($m + '  (missing)')
        }
    }

    Write-Host ''
    Write-Host 'Weights are CC BY-NC 4.0 (non-commercial only) -- see THIRD_PARTY_NOTICES.md'
}

if ($Check) {
    Show-Status
    Stop-SetupLog
    exit 0
}

# ---------------------------------------------------------------- self test
if ($SelfTest) {
    Write-Step 'Self test (network only, nothing gets installed)'
    $m = Select-BestPyPIMirror
    Write-Ok ('pypi mirror = ' + $m.Name + '  ' + $m.Index + '  (trusted-host ' + $m.Host + ')')
    foreach ($sd in @('cu121', 'cu128', 'xpu', 'cpu')) {
        $c = Select-BestTorchWheelUrl $sd
        Write-Ok ('torch ' + $sd + ' -> ' + $c.Name + '  ' + $c.Base)
    }
    $mainPy = Join-Path $MainVenv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $mainPy) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $ok = Test-Imports $mainPy @('PyQt5', 'mido', 'pretty_midi', 'soundcard', 'PIL', 'numpy')
        $sw.Stop()
        $sec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        Write-Ok ('main venv imports=' + $ok + '  check time=' + $sec + 's (no network)')
    }
    Write-Ok ('download hint (encoding check): ' + $script:TorchHint)
    Restore-ProxyEnv
    Stop-SetupLog
    exit 0
}

# ---------------------------------------------------------------- install mode
$Which = $Backend
if ($Which -eq 'auto') { $Which = Detect-Backend }
if ($Which -eq 'cuda') {
    $script:CudaMajor = Get-NvidiaComputeMajor
    Write-Host ('  [gpu] CUDA compute major: ' + $script:CudaMajor + ' (>=10 uses cu128 for Blackwell incl. RTX 50)')
}

Write-Step 'Locating base Python'
$base = Find-BasePython
if (-not $base) {
    Write-Bad 'Python 3.10+ not found. Install 64-bit Python 3.11 first (python.org), then rerun.'
    Stop-SetupLog
    exit 1
}
$ver = Get-PythonVersion $base
Write-Ok ('base python: ' + $base.Exe + ' ' + ($base.Args -join ' ') + '  (version ' + $ver + ')')
Write-Ok ('torch backend: ' + $Which + '   index: ' + (Get-TorchIndex $Which))
Write-Host '  (override with: -Backend cuda   or   -TorchIndex <url>)'

if (-not $SkipMain) {
    Write-Step 'Main environment (.venv)'
    New-Venv $base $MainVenv
    $mainPy = Join-Path $MainVenv 'Scripts\python.exe'
    if ((-not $Recreate) -and (Test-Imports $mainPy @('PyQt5', 'mido', 'pretty_midi', 'soundcard', 'PIL', 'numpy'))) {
        Write-Ok 'Main environment already verified (offline fast-pass).'
    } else {
        Install-Requirements $mainPy $MainReq 'main program'
        if (Test-Imports $mainPy @('pretty_midi', 'mido', 'numpy', 'PyQt5')) {
            Write-Ok 'main program dependencies verified'
        } else {
            Write-Warn2 'some main dependencies are still missing -- check the pip output above'
        }
    }
}

if (-not $SkipSheetSage2) {
    Write-Step 'SheetSage2 environment (vendor\sheetsage2\.venv)'
    New-Venv $base $S2Venv
    $s2Py = Join-Path $S2Venv 'Scripts\python.exe'
    if ((-not $Recreate) -and (Test-S2BackendReady $s2Py $Which)) {
        Write-Ok 'SheetSage2 environment already verified (offline fast-pass).'
    } else {
        Install-Torch $s2Py $Which 'sheetsage2 venv'
        Install-Requirements $s2Py $S2Req 'SheetSage2'
        if (Test-Imports $s2Py @('torch', 'transformers', 'librosa', 'imageio_ffmpeg')) {
            Write-Ok 'SheetSage2 dependencies verified'
        } else {
            Write-Warn2 'torch / transformers / librosa still missing -- check the pip output above'
        }
        if (($Which -eq 'xpu') -or ($Which -eq 'cuda') -or ($Which -eq 'rocm')) {
            if (Test-S2BackendReady $s2Py $Which) {
                Write-Ok ($Which + ' acceleration is usable')
            } else {
                Write-Warn2 ($Which + ' runtime check returned non-zero (see logs\setup_env.log); keeping installed build (runtime will auto-fallback to CPU if needed)')
            }
        }
    }
    if (Test-Imports $s2Py @('audio_separator')) {
        Write-Ok 'separation dependencies already present (audio-separator)'
    } else {
        Write-Host '  installing optional separation deps (HT-Demucs / Mel-Band RoFormer)'
        try {
            Install-Requirements $s2Py $S2SepReq 'SheetSage2 separation'
        } catch {
            Write-Warn2 ('separation deps could not be installed: ' + $_.Exception.Message)
        }
        if (Test-Imports $s2Py @('audio_separator')) {
            Write-Ok 'separation dependencies verified (audio-separator)'
        } else {
            Write-Warn2 'separation deps still missing; the AI page will retry the install on first separate (needs network)'
        }
    }
}

Write-Step 'Done'
Write-Host '  Next:'
Write-Host '    1) weights:   powershell -NoProfile -ExecutionPolicy Bypass -File setup\fetch_models.ps1'
Write-Host '    2) self check: python pipeline.py paths'
Write-Host '    3) run:        python pipeline.py run <audio> --out <dir> --model large'
Write-Host '  Model weights are CC BY-NC 4.0 (non-commercial only).'
Restore-ProxyEnv
Stop-SetupLog
exit 0