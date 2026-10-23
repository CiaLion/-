# fetch_models.ps1 -- download SheetSage2 / MERT-v2-FullSong weights.
#
# Default path uses public hf-mirror.com direct URLs (no HuggingFace login):
#   https://hf-mirror.com/m-a-p/SheetSage2/resolve/main/model.safetensors
#   https://hf-mirror.com/m-a-p/MERT-v2-FullSong/resolve/main/model.safetensors
#
# If curl direct download fails, it falls back to huggingface_hub (also routed
# through hf-mirror.com unless -NoMirror is set).
# Every downloaded weight is verified by exact byte size and SHA256.
# Local patched model .py files (patched_model_py) are always restored at the end.
#
# This file is 100% pure ASCII so Windows PowerShell 5.1 never garbles it.

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$SelfTest,
    [ValidateSet('all', 'sheetsage2', 'mert')]
    [string]$Only = 'all',
    [string]$Endpoint = '',
    [string]$VenvPython = '',
    [string]$Mirror = 'https://hf-mirror.com',
    [switch]$NoMirror
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root      = Split-Path -Parent $PSScriptRoot
$ModelsDir = Join-Path $Root 'vendor\sheetsage2\models'
$S2Dir     = Join-Path $ModelsDir 'SheetSage2'
$MertDir   = Join-Path $ModelsDir 'MERT-v2-FullSong'

$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$FetchLog = Join-Path $LogDir 'fetch_models.log'

function Write-FetchLog([string]$Text) {
    try { Add-Content -LiteralPath $FetchLog -Value $Text -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
}
function Stop-FetchLog { }

$Repos = @(
    @{
        Repo   = 'm-a-p/SheetSage2'
        Dir    = $S2Dir
        Tag    = 'sheetsage2'
        SizeGb = 0.22
        Bytes  = 228738564
        Sha256 = 'B235F68091A5F5B644000F2B5ACB57D1E70432ACA2B34AB1B9CF27236E1F4274'
        Files  = @(
            '__init__.py', 'config.json', 'configuration.json',
            'configuration_mert2.py', 'configuration_sheetsage2.py',
            'audio_sheetsage2.py', 'infer.py', 'modeling_mert2.py',
            'modeling_sheetsage2.py', 'pipeline_sheetsage2.py',
            'processing_sheetsage2.py', 'processor_config.json',
            'tokenization_sheetsage2.py', 'README.md', 'LICENSE',
            'THIRD_PARTY_NOTICES.md'
        )
    },
    @{
        Repo   = 'm-a-p/MERT-v2-FullSong'
        Dir    = $MertDir
        Tag    = 'mert'
        SizeGb = 2.36
        Bytes  = 2529812848
        Sha256 = 'E6DD2AB187D6DD62B6521CD7D8F932E237ACF0C5757745A7232082E28391350D'
        Files  = @(
            'config.json', 'configuration.json', 'configuration_mert2.py',
            'modeling_mert2.py', 'preprocessor_config.json',
            'weights_manifest.json', 'README.md', 'LICENSE',
            'THIRD_PARTY_NOTICES.md'
        )
    }
)

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('=== ' + $Text) -ForegroundColor Cyan
    Write-FetchLog ('=== ' + $Text)
}
function Write-Ok([string]$Text) {
    Write-Host ('  [ok]   ' + $Text) -ForegroundColor Green
    Write-FetchLog ('[ok] ' + $Text)
}
function Write-Warn2([string]$Text) {
    Write-Host ('  [warn] ' + $Text) -ForegroundColor Yellow
    Write-FetchLog ('[warn] ' + $Text)
}
function Write-Bad([string]$Text) {
    Write-Host ('  [miss] ' + $Text) -ForegroundColor Red
    Write-FetchLog ('[miss] ' + $Text)
}

# ------------------------------------------------------------------ mirrors & proxy
$script:PyPIMirror = $null
$script:SavedProxy = $null

$Py = $VenvPython
if (-not $Py) {
    $Py = Join-Path $Root 'vendor\sheetsage2\.venv\Scripts\python.exe'
}
$Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
$LoginExe = Join-Path $Root 'vendor\sheetsage2\.venv\Scripts\huggingface-cli.exe'

if ($Endpoint) {
    $Mirror = $Endpoint
    $env:HF_ENDPOINT = $Endpoint
}
$Mirror = $Mirror.TrimEnd('/')
if ((-not $NoMirror) -and $Mirror) {
    $env:HF_ENDPOINT = $Mirror
}

function Test-UrlHead([string]$Url, [int]$TimeoutSec = 3) {
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

function Test-UrlViaCurl([string]$Url, [int]$TimeoutSec = 10) {
    if (-not $Curl) { return $false }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $Curl.Source -s -I -L --fail -o NUL --max-time $TimeoutSec $Url 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
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
    return $saved
}

function Restore-ProxyEnv {
    if ($script:SavedProxy -eq $null) { return }
    foreach ($k in $script:SavedProxy.Keys) {
        try { Set-Item -Path ('env:' + $k) -Value $script:SavedProxy[$k] } catch { }
    }
    $script:SavedProxy = $null
}

function Ensure-ClearDeadProxyForMirror {
    if (-not (Test-ProxyEnvSet)) { return }
    $probeUrl = $Mirror + '/m-a-p/SheetSage2/resolve/main/config.json'
    if (-not (Test-UrlViaCurl $probeUrl 5)) {
        $script:SavedProxy = Clear-ProxyEnv
    }
}

function Select-BestPyPIMirror {
    $list = @(
        @{ Name = 'aliyun';   Index = 'https://mirrors.aliyun.com/pypi/simple/'; Host = 'mirrors.aliyun.com' },
        @{ Name = 'tencent';  Index = 'https://mirrors.cloud.tencent.com/pypi/simple/'; Host = 'mirrors.cloud.tencent.com' },
        @{ Name = 'huawei';   Index = 'https://repo.huaweicloud.com/repository/pypi/simple/'; Host = 'repo.huaweicloud.com' },
        @{ Name = 'tsinghua'; Index = 'https://pypi.tuna.tsinghua.edu.cn/simple/'; Host = 'pypi.tuna.tsinghua.edu.cn' }
    )
    $aliyun  = Test-UrlHead $list[0].Index
    $tencent = Test-UrlHead $list[1].Index
    if ((-not $aliyun) -and (-not $tencent) -and (Test-ProxyEnvSet)) {
        $script:SavedProxy = Clear-ProxyEnv
        $aliyun  = Test-UrlHead $list[0].Index
        $tencent = Test-UrlHead $list[1].Index
    }
    if ($aliyun) {
        $best = $list[0]
    } elseif ($tencent) {
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
    Write-FetchLog ('[pypi] selected mirror: ' + $best.Name + ' ' + $best.Index)
    return $best
}

function Test-HfHub([string]$PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe)) { return $false }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $PythonExe -c "import huggingface_hub" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Ensure-HfHub([string]$PythonExe) {
    if (Test-HfHub $PythonExe) { return $true }
    $m = Select-BestPyPIMirror
    Write-Host ('  installing huggingface_hub from ' + $m.Name)
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    try {
        & $PythonExe -m pip install huggingface_hub -i $m.Index --trusted-host $m.Host --timeout 15 --retries 2 --disable-pip-version-check
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($code -ne 0) {
        Write-Warn2 'mirror install failed, retrying official PyPI'
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $PythonExe -m pip install huggingface_hub -i https://pypi.org/simple --timeout 20 --retries 2 --disable-pip-version-check
        } finally {
            $ErrorActionPreference = $oldEap
        }
    }
    return (Test-HfHub $PythonExe)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpper()
}

function Restore-PatchedModelCode {
    $patch = Join-Path $Root 'vendor\sheetsage2\patched_model_py'
    if (-not (Test-Path -LiteralPath $patch)) {
        Write-Warn2 ('patched model code not found: ' + $patch)
        return $false
    }
    if (-not (Test-Path -LiteralPath $S2Dir)) {
        New-Item -ItemType Directory -Path $S2Dir -Force | Out-Null
    }
    $count = 0
    Get-ChildItem -LiteralPath $patch -Filter '*.py' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $S2Dir $_.Name) -Force
        $count++
    }
    Write-Ok ('restored ' + $count + ' patched model code file(s) from patched_model_py')
    return $true
}

function Get-RepoFileNames($Item) {
    if ($Curl) {
        $api = $Mirror + '/api/models/' + $Item.Repo
        $tmp = Join-Path $env:TEMP ('yijian_repo_' + $Item.Tag + '.json')
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $code = 1
        try {
            & $Curl.Source -L --fail --silent --show-error --max-time 15 -o $tmp $api
            $code = $LASTEXITCODE
        } catch { } finally {
            $ErrorActionPreference = $oldEap
        }
        if ($code -eq 0 -and (Test-Path -LiteralPath $tmp)) {
            try {
                $obj = Get-Content -LiteralPath $tmp -Raw | ConvertFrom-Json
                $names = @(
                    $obj.siblings |
                    ForEach-Object { $_.rfilename } |
                    Where-Object {
                        $_ -and ($_ -notmatch '/') -and
                        ($_ -ne 'model.safetensors') -and
                        ($_ -ne '.gitattributes')
                    }
                )
                if ($names.Count -gt 0) { return $names }
            } catch { }
        }
    }
    return @($Item.Files)
}

function Test-RepoFiles($Item, $Names) {
    foreach ($n in $Names) {
        $p = Join-Path $Item.Dir $n
        if (-not (Test-Path -LiteralPath $p)) { return $false }
        if ((Get-Item -LiteralPath $p).Length -le 0) { return $false }
    }
    return $true
}

function Invoke-MirrorRepoFiles($Item, $Names) {
    if (-not $Curl) { return $false }
    $ok = $true
    foreach ($n in $Names) {
        $dst = Join-Path $Item.Dir $n
        if ((Test-Path -LiteralPath $dst) -and ((Get-Item -LiteralPath $dst).Length -gt 0)) {
            continue
        }
        $url = $Mirror + '/' + $Item.Repo + '/resolve/main/' + $n
        $part = $dst + '.part'
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $code = 1
        try {
            & $Curl.Source -L --fail --retry 3 --retry-delay 2 --connect-timeout 30 --silent --show-error -o $part $url
            $code = $LASTEXITCODE
        } catch { } finally {
            $ErrorActionPreference = $oldEap
        }
        if ($code -ne 0 -or (-not (Test-Path -LiteralPath $part)) -or ((Get-Item -LiteralPath $part).Length -le 0)) {
            Write-Warn2 ('repo file failed: ' + $n)
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            $ok = $false
            continue
        }
        Move-Item -LiteralPath $part -Destination $dst -Force
    }
    return $ok
}

function Test-WeightSize($Item) {
    $w = Join-Path $Item.Dir 'model.safetensors'
    if (-not (Test-Path -LiteralPath $w)) { return $false }
    return ((Get-Item -LiteralPath $w).Length -eq $Item.Bytes)
}

function Test-WeightHash($Item) {
    $w = Join-Path $Item.Dir 'model.safetensors'
    if (-not (Test-Path -LiteralPath $w)) { return $false }
    if ((Get-Item -LiteralPath $w).Length -ne $Item.Bytes) { return $false }
    return ((Get-Sha256 $w) -eq $Item.Sha256)
}

function Show-Check {
    Write-Step 'Python'
    if (Test-Path -LiteralPath $Py) {
        Write-Ok ('venv python: ' + $Py)
    } else {
        Write-Bad ('venv python missing: ' + $Py)
    }
    if ((Test-Path -LiteralPath $Py) -and (Test-HfHub $Py)) {
        Write-Ok 'huggingface_hub installed'
    } elseif (Test-Path -LiteralPath $Py) {
        Write-Warn2 'huggingface_hub NOT installed (fallback only)'
    }

    Write-Step 'Weights'
    foreach ($item in $Repos) {
        $weight = Join-Path $item.Dir 'model.safetensors'
        if ((Test-Path -LiteralPath $weight) -and ((Get-Item -LiteralPath $weight).Length -gt 0)) {
            $gb = [math]::Round((Get-Item -LiteralPath $weight).Length / 1GB, 2)
            $hashOk = (Test-WeightHash $item)
            $mark = if ($hashOk) { 'SHA ok' } else { 'SHA MISMATCH' }
            if ($hashOk) {
                Write-Ok ($item.Repo + '  ->  ' + $weight + '  (' + $gb + ' GB, ' + $mark + ')')
            } else {
                Write-Bad ($item.Repo + '  ->  ' + $weight + '  (' + $gb + ' GB, ' + $mark + ')')
            }
        } else {
            Write-Bad ($item.Repo + '  ->  ' + $weight + '  (not downloaded)')
        }
    }
    Write-Step 'Mirror'
    Write-Host ('  direct URL base: ' + $Mirror + '   (no login)')
    Write-Step 'Patched model code'
    $patch = Join-Path $Root 'vendor\sheetsage2\patched_model_py'
    if (Test-Path -LiteralPath $patch) {
        $patched = @(Get-ChildItem -LiteralPath $patch -Filter '*.py')
        Write-Ok ('patched_model_py: ' + $patched.Count + ' file(s)')
    } else {
        Write-Warn2 ('patched_model_py missing: ' + $patch)
    }
    Write-Step 'License reminder'
    Write-Host '  SheetSage2 and MERT-v2-FullSong are CC BY-NC 4.0: NON-COMMERCIAL USE ONLY.'
}

if ($Check) {
    Show-Check
    Stop-FetchLog
    exit 0
}

# ------------------------------------------------------------------ self test
if ($SelfTest) {
    Write-Step 'Self test (probes only, nothing is downloaded)'
    $m = Select-BestPyPIMirror
    Write-Ok ('pypi mirror = ' + $m.Name + '  ' + $m.Index + '  (trusted-host ' + $m.Host + ')')
    Write-Step 'Direct mirror reachability (HEAD via curl, nothing downloaded)'
    Ensure-ClearDeadProxyForMirror
    foreach ($item in $Repos) {
        $url = $Mirror + '/' + $item.Repo + '/resolve/main/model.safetensors'
        if (Test-UrlViaCurl $url 15) {
            Write-Ok ('reachable: ' + $url)
        } else {
            Write-Warn2 ('unreachable: ' + $url)
        }
    }
    Write-Step 'huggingface_hub (fallback only)'
    if (Test-Path -LiteralPath $Py) {
        if (Test-HfHub $Py) {
            Write-Ok 'huggingface_hub already installed (fallback needs no pip run)'
        } else {
            Write-Warn2 'huggingface_hub not installed - only needed if the direct mirror fails'
        }
    } else {
        Write-Warn2 ('venv python missing: ' + $Py)
    }
    Restore-ProxyEnv
    Stop-FetchLog
    exit 0
}

# ------------------------------------------------------------------ download
Write-Step 'Downloading model weights'
if (-not (Test-Path -LiteralPath $ModelsDir)) {
    New-Item -ItemType Directory -Path $ModelsDir -Force | Out-Null
}
Ensure-ClearDeadProxyForMirror
Restore-PatchedModelCode | Out-Null

function Invoke-MirrorDownload($Item) {
    if (-not $Curl) {
        Write-Warn2 'curl.exe not found; skip mirror download'
        return $false
    }
    $weight = Join-Path $Item.Dir 'model.safetensors'
    if ((Test-Path -LiteralPath $weight) -and ((Get-Item -LiteralPath $weight).Length -lt $Item.Bytes)) {
        Remove-Item -LiteralPath $weight -Force -ErrorAction SilentlyContinue
    }
    $part = $weight + '.part'
    if ((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part).Length -ge $Item.Bytes -or (Get-Item -LiteralPath $part).Length -le 0)) {
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
    }
    $url = $Mirror + '/' + $Item.Repo + '/resolve/main/model.safetensors'
    Write-Host ('  mirror: ' + $url)

    $baseArgs = @('-L', '--fail', '--retry', '5', '--retry-delay', '3', '--connect-timeout', '30', '--progress-bar')
    $resume = Test-Path -LiteralPath $part
    $attempts = if ($resume) { @($true, $false) } else { @($false) }
    $ok = $false
    foreach ($useResume in $attempts) {
        $curlArgs = $baseArgs + @('-o', $part)
        if ($useResume) {
            $curlArgs += @('-C', '-')
        }
        $curlArgs += $url
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $code = 1
        try {
            & $Curl.Source @curlArgs
            $code = $LASTEXITCODE
        } catch { } finally {
            $ErrorActionPreference = $oldEap
        }
        if ($code -eq 0) {
            $ok = $true
            break
        }
        if ($useResume) {
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            Write-Warn2 'resume failed; restarting this file from 0'
        }
    }
    if (-not $ok) {
        Write-Warn2 'mirror download failed'
        return $false
    }
    if (-not (Test-Path -LiteralPath $part)) { return $false }
    $size = (Get-Item -LiteralPath $part).Length
    if ($size -ne $Item.Bytes) {
        Write-Warn2 ('size mismatch: got ' + $size + ', expected ' + $Item.Bytes)
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        return $false
    }
    $hash = Get-Sha256 $part
    if ($hash -ne $Item.Sha256) {
        Write-Warn2 ('SHA256 mismatch: got ' + $hash)
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        return $false
    }
    Move-Item -LiteralPath $part -Destination $weight -Force
    Write-Ok ('verified ' + $weight)
    return $true
}

function Invoke-HfDownload($Item) {
    if (-not (Test-Path -LiteralPath $Py)) {
        Write-Bad ('venv python missing: ' + $Py)
        return $false
    }
    if (-not (Ensure-HfHub $Py)) {
        Write-Bad 'huggingface_hub is missing and could not be installed (fallback needs it)'
        return $false
    }
    if ((-not $NoMirror) -and $Mirror) {
        $env:HF_ENDPOINT = $Mirror
    }
    $code = @'
import sys
from huggingface_hub import snapshot_download

repo_id, local_dir = sys.argv[1], sys.argv[2]
print("repo      :", repo_id)
print("local_dir :", local_dir)
try:
    path = snapshot_download(repo_id=repo_id, local_dir=local_dir)
except KeyboardInterrupt:
    print("interrupted -- rerun this script, finished files are kept")
    sys.exit(2)
except Exception as exc:
    msg = str(exc)
    print("FAILED:", type(exc).__name__, msg)
    if "401" in msg or "403" in msg or "gated" in msg.lower() or "token" in msg.lower():
        print("")
        print("This repo is GATED. Open the model page, accept the terms, then log in once with:")
        print("  " + sys.argv[3] + " login")
    sys.exit(1)
print("OK ->", path)
'@
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $exitCode = 1
    try {
        & $Py -c $code $Item.Repo $Item.Dir $LoginExe
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($exitCode -ne 0) { return $false }
    return (Test-WeightHash $Item)
}

foreach ($item in $Repos) {
    if ($Only -ne 'all' -and $Only -ne $item.Tag) { continue }
    Write-Step ('Fetching ' + $item.Repo + '  (~' + $item.SizeGb + ' GB)')
    if (-not (Test-Path -LiteralPath $item.Dir)) {
        New-Item -ItemType Directory -Path $item.Dir -Force | Out-Null
    }

    $names = Get-RepoFileNames $item
    $weightOk = Test-WeightHash $item
    $filesOk = Test-RepoFiles $item $names
    if ($weightOk -and $filesOk) {
        Write-Ok ('already present + SHA ok: ' + (Join-Path $item.Dir 'model.safetensors'))
        continue
    }

    if (-not $NoMirror) {
        if (-not $filesOk) {
            Write-Host ('  downloading ' + $names.Count + ' repo files (config/code)...')
            $filesOk = Invoke-MirrorRepoFiles $item $names
        }
        if (-not $weightOk) {
            $weightOk = Invoke-MirrorDownload $item
        }
    }

    if (-not ($weightOk -and $filesOk)) {
        Write-Warn2 'direct curl incomplete; falling back to huggingface_hub'
        if (Invoke-HfDownload $item) {
            $weightOk = Test-WeightHash $item
            $filesOk = Test-RepoFiles $item $names
        }
    }

    if (-not ($weightOk -and $filesOk)) {
        Write-Bad ('download failed: ' + $item.Repo)
        Write-Host '  If it is a gated-repo error, log in once and rerun:'
        Write-Host ('    ' + $LoginExe + ' login')
        Restore-PatchedModelCode | Out-Null
        Restore-ProxyEnv
        Stop-FetchLog
        exit 1
    }
}

Restore-PatchedModelCode | Out-Null

Write-Step 'Done'
Write-Host '  Verify with: powershell -NoProfile -ExecutionPolicy Bypass -File setup\fetch_models.ps1 -Check'
Write-Host '  Weights are CC BY-NC 4.0 (non-commercial only).'
Restore-ProxyEnv
Stop-FetchLog
exit 0