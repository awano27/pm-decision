@echo off
rem kimeru: one double-click on a company PC. Prep (Kev start, az sign-in) then the short check.
rem Folder layout: <any folder>\kimeru (this), <same folder>\kev, <same folder>\az. No admin.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare-company.ps1"
echo.
echo ===== 続けて動作確認をします（10 分ほど） =====
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\company-check.ps1" monday
echo.
echo 終わりました。結果はクリップボードにコピー済みです。チャットに貼り付けてください。
pause
