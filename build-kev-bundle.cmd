@echo off
rem Builds the Kev folder (kev-bundle) on the development PC. Needs git and uv, and about 10GB of download.
rem It asks before it sends anything and before it downloads. Options: -Out, -KevRepo, -Yes, -DryRun, -NoStartCheck.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build-kev-bundle.ps1" %*
echo.
pause
