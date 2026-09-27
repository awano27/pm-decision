@echo off
rem kimeru: one double-click on a company PC. Prep (Kev start, az sign-in) then the short check.
rem Messages are printed by tools\start.ps1 (keeps this file ASCII, so no garbled text).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start.ps1"
pause
