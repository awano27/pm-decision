@echo off
rem kimeru Monday prep: checks the carried-in folders, starts Kev, signs in to az. No admin.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare-company.ps1" %*
echo.
pause
