@echo off
rem kimeru company PC setup (no admin). Usage: setup-company.cmd install ^| remove ^| status
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup-company.ps1" %*
echo.
pause
