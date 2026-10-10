@echo off
rem kimeru: one-click fix for deliveries that ended as "unknown". Double-click to run. Asks nothing; see tools\fix-deliveries.ps1.
rem It reads the self chat once (nothing is typed or sent), settles every unknown delivery and writes numbers only to kimeru-fix-deliveries-result.txt.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\fix-deliveries.ps1"
echo.
pause
