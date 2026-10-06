@echo off
rem kimeru: check the latest changes on a managed PC. Double-click to run.
rem Nothing is sent and Teams is not touched; see tools\check-update.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check-update.ps1"
echo.
pause
