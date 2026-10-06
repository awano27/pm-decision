@echo off
rem kimeru managed PC setup (no admin). Usage: setup-managed.cmd install ^| remove ^| status
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup-managed.ps1" %*
echo.
pause
