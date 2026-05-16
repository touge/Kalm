# Kalm 启动脚本
# 用法: .\run.ps1

param(
    [int]$Port = 7000
)

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Kalm — AI 中转控制站" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 检查 Python 环境
$PythonPath = "$ScriptDir\python3.11\python.exe"
if (Test-Path $PythonPath) {
    Write-Host "[OK] Using bundled Python: $PythonPath" -ForegroundColor Green
    $PythonExe = $PythonPath
} else {
    Write-Host "[INFO] Using system Python" -ForegroundColor Yellow
    $PythonExe = "python"
}

Write-Host "[INFO] Starting Kalm on port $Port..." -ForegroundColor White
& $PythonExe main.py

pause
