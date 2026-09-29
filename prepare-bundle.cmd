@echo off
rem kimeru Monday prep: checks the carried-in folders, starts Kev, signs in to az. No admin.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare-bundle.ps1" %*
echo.
pause
