@echo off
rem kimeru managed PC check: double-click to run. See docs/managed-pc-check.md
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check.ps1" %*
echo.
pause
