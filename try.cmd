@echo off
rem kimeru: the try run in one double-click. Needs Python 3.10 or later; says how to get it when it is missing.
rem Messages are printed by tools\try.ps1 (keeps this file ASCII, so no garbled text).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\try.ps1"
pause
