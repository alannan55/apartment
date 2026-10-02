param(
    [switch]$DryRun,
    [string]$BindAddress = '0.0.0.0',
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot
$apartmentConda = $env:CONDA_EXE
if (-not $apartmentConda -or -not (Test-Path -LiteralPath $apartmentConda)) {
    $apartmentCondaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($apartmentCondaCommand) { $apartmentConda = $apartmentCondaCommand.Source }
}
if (-not $apartmentConda -or -not (Test-Path -LiteralPath $apartmentConda)) {
    foreach ($apartmentCondaRoot in @("$env:USERPROFILE/miniconda3", "$env:USERPROFILE/anaconda3", "$env:ProgramData/miniconda3", "$env:ProgramData/anaconda3")) {
        $apartmentCondaCandidate = Join-Path $apartmentCondaRoot 'Scripts/conda.exe'
        if (Test-Path -LiteralPath $apartmentCondaCandidate) {
            $apartmentConda = $apartmentCondaCandidate
            break
        }
    }
}
if (-not $apartmentConda -or -not (Test-Path -LiteralPath $apartmentConda)) {
    throw '未找到 conda。请将 conda.exe 加入 PATH，或设置 CONDA_EXE 为其完整路径。'
}

Write-Host '正在启用 conda 环境 utils...'
$apartmentCondaHook = & $apartmentConda shell.powershell hook
if ($LASTEXITCODE -ne 0) { throw '无法初始化 conda，请检查安装。' }
Invoke-Expression ($apartmentCondaHook | Out-String)
conda activate utils
if ($LASTEXITCODE -ne 0 -or $env:CONDA_DEFAULT_ENV -ne 'utils') {
    throw '无法启用 conda 环境 utils，请先按 README 创建环境。'
}
$apartmentPython = Join-Path $env:CONDA_PREFIX 'python.exe'
if (-not (Test-Path -LiteralPath $apartmentPython)) { throw 'utils 环境中缺少 Python。' }

# This entry point is for Windows debugging; R4S uses the Docker entry point.
$env:DJANGO_DEBUG = '1'
$env:DJANGO_HTTPS = '0'
$env:APARTMENT_TRUST_PROXY = '0'
$env:DJANGO_ALLOWED_HOSTS = '*'

Write-Host '正在检查项目环境...'
& $apartmentPython -c "import sys, django, openpyxl, PIL, whitenoise; print('Python:', sys.executable)"
if ($LASTEXITCODE -ne 0) { throw '依赖不完整，请执行 conda run -n utils python -m pip install -r requirements.txt。' }
& $apartmentPython manage.py check
if ($LASTEXITCODE -ne 0) { throw '项目检查未通过，请根据上方提示处理。' }
& $apartmentPython manage.py check_runtime
if ($LASTEXITCODE -ne 0) { throw '账号或运行环境未准备好，请根据上方提示处理。' }
if ($DryRun) { return }

Write-Host '正在更新数据结构...'
& $apartmentPython manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw '数据结构更新未完成，服务未启动。' }
Write-Host "请在浏览器打开 http://127.0.0.1:$Port/ 。按 Ctrl+C 停止服务。"
$apartmentServerArguments = @('manage.py', 'runserver', "${BindAddress}:$Port")
if ($NoReload) { $apartmentServerArguments += '--noreload' }
& $apartmentPython @apartmentServerArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
