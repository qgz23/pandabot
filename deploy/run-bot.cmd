@echo off
REM ============================================================
REM  PandaBot launcher for Windows
REM  Run bot.py in a loop: if the process dies, wait 10s and restart.
REM  Logs go to logs\bot.log (append mode).
REM
REM  Do NOT put Chinese comments in this file - cmd.exe reads .cmd
REM  with the OEM codepage and non-ASCII text can corrupt parsing.
REM ============================================================

chcp 65001 >nul
cd /d "%~dp0.."

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

if not exist logs mkdir logs

:loop
echo. >> logs\bot.log
echo ========== %date% %time% start ========== >> logs\bot.log
".venv\Scripts\python.exe" bot.py >> logs\bot.log 2>&1
echo ========== %date% %time% exited code=%errorlevel%, retry in 10s ========== >> logs\bot.log
timeout /t 10 /nobreak >nul
goto loop
