@echo off
rem Kev (local, Jev-compatible decision model) - offline start. No admin rights, no network.
rem Usage: start-kev.cmd [kev-4b|kev-0.8b]   (default kev-4b)
setlocal
cd /d "%~dp0"
set MODEL=%1
if "%MODEL%"=="" set MODEL=kev-4b
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set HF_HUB_DISABLE_TELEMETRY=1
set PYTHONNOUSERSITE=1
rem bf16: ~10GB RAM instead of ~14GB, same answers on the kimeru eval, but only fast on CPUs with
rem native bf16 (AVX512-BF16 / AMX). Elsewhere bf16 is emulated and several times slower, so use fp32.
rem Set KEV_DTYPE=bf16 or fp32 before starting to override.
if "%KEV_DTYPE%"=="" (
  "%~dp0python\python.exe" "%~dp0pick-dtype.py" > "%TEMP%\kev-dtype.txt"
  set /p KEV_DTYPE=<"%TEMP%\kev-dtype.txt"
)
if "%KEV_DTYPE%"=="" set KEV_DTYPE=fp32
echo precision: %KEV_DTYPE%
"%~dp0python\python.exe" "%~dp0localize.py" %MODEL% || goto :err
echo Starting %MODEL% on http://127.0.0.1:8009 (first load takes a few minutes; keep this window open)
"%~dp0python\python.exe" -m kev.serve --run "%~dp0models\%MODEL%" --port 8009
goto :eof
:err
echo Kev could not start. See the message above.
pause
