@echo off
rem kimeru company PC check: double-click to run. See docs/company-pc-test.md
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\company-check.ps1"
echo.
pause
