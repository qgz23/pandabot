# ============================================================
#  Register PandaBot as a Windows scheduled task.
#
#  Why a scheduled task instead of a Windows service:
#    - no extra tool to download (NSSM / WinSW are third-party)
#    - "onstart" + SYSTEM account = starts at boot, no login needed
#    - the .cmd launcher already restarts the process on crash
#
#  Run this in PowerShell **as Administrator**:
#      cd C:\pandabot\deploy
#      powershell -ExecutionPolicy Bypass -File .\install-service.ps1
#
#  Check / stop / remove:
#      schtasks /query  /tn PandaBot /v /fo LIST
#      schtasks /end    /tn PandaBot
#      schtasks /delete /tn PandaBot /f
# ============================================================

$ErrorActionPreference = "Stop"

# 需要管理员权限，否则 schtasks 会失败
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "ERROR: please run PowerShell as Administrator." -ForegroundColor Red
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $PSScriptRoot "run-bot.cmd"
$venv = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env.prod"

if (-not (Test-Path $cmd)) { Write-Host "ERROR: run-bot.cmd not found: $cmd" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $venv)) { Write-Host "ERROR: venv not ready: $venv" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $envFile)) { Write-Host "ERROR: .env.prod not found: $envFile" -ForegroundColor Red; exit 1 }

Write-Host "Project root : $root"
Write-Host "Launcher     : $cmd"

# 先删旧的，避免同名任务报错
schtasks /delete /tn "PandaBot" /f 2>$null | Out-Null

# onstart = 开机启动；SYSTEM = 不需要登录、不需要存密码
schtasks /create /tn "PandaBot" /tr "`"$cmd`"" /sc onstart /ru SYSTEM /rl HIGHEST /f

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: failed to create task (exit $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Task created. Starting it now..." -ForegroundColor Green
schtasks /run /tn "PandaBot"

Start-Sleep -Seconds 3
Write-Host ""
Write-Host "Last 20 lines of logs\bot.log:" -ForegroundColor Cyan
$log = Join-Path $root "logs\bot.log"
if (Test-Path $log) { Get-Content $log -Tail 20 } else { Write-Host "(no log yet, wait a few seconds)" }
