param([switch]$DryRun)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot
$apartmentPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $apartmentPython)) {
    throw '缺少项目 Python 环境 .venv，请先按 README 安装。启动时不会自动安装或替换软件。'
}

Write-Host '正在检查项目环境...'
& $apartmentPython -c "import sys, django, openpyxl, PIL; print('Python:', sys.executable)"
if ($LASTEXITCODE -ne 0) { throw '依赖不完整，请用项目 .venv 中的 Python 安装 requirements.txt。' }
& $apartmentPython manage.py check
if ($LASTEXITCODE -ne 0) { throw '项目检查未通过，请根据上方提示处理。' }
if ($DryRun) { return }

Write-Host '正在更新数据结构...'
& $apartmentPython manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw '数据结构更新未完成，服务未启动。' }
Write-Host '请在浏览器打开 http://127.0.0.1:8000/ 。按 Ctrl+C 停止服务。'
& $apartmentPython manage.py runserver 0.0.0.0:8000
