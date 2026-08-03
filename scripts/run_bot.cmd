@echo off
rem Hourly runner for the payments email bot (invoked by Windows Task Scheduler).
rem Draft-only by design: unattended runs can create Gmail drafts and log
rem escalations, never send mail. Output goes to logs\payment-bot-YYYY-MM-DD.log
rem so unattended runs stay auditable.

cd /d "%~dp0.."
set PYTHONUTF8=1

if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOGFILE=logs\payment-bot-%TODAY%.log

echo. >> "%LOGFILE%"
echo ======== run started %DATE% %TIME% ======== >> "%LOGFILE%"
".venv\Scripts\payment-bot-local.exe" >> "%LOGFILE%" 2>&1
echo ======== run finished %DATE% %TIME% (exit %ERRORLEVEL%) ======== >> "%LOGFILE%"
