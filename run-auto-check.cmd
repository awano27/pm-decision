@echo off
rem kimeru: every check that needs nobody and sends nothing, in one go. Double-click to run.
rem Nothing is sent or pasted. The self chat may be opened and Teams brought to the front (the previous chat is not restored).
rem Teams and Kev are not started; see tools\check-auto.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check-auto.ps1"
echo.
pause
