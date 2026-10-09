@echo off
rem kimeru: one-click diagnosis. Double-click to run. Asks nothing and posts nothing; see tools\diagnose.ps1.
rem The result (numbers only, no message text) goes to kimeru-diagnose-result.txt, the clipboard and Google Drive\kimeru-release.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\diagnose.ps1"
echo.
pause
