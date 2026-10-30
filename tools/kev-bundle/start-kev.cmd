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
rem the base model sits in the Hugging Face cache layout inside this folder, at the revision head.pt names
set "HF_HOME=%~dp0hf"
rem bf16: ~10GB RAM instead of ~14GB, same answers on the kimeru eval, but only fast on CPUs with
rem native bf16 (AVX512-BF16 / AMX). Elsewhere bf16 is emulated and several times slower, so use fp32.
rem Set KEV_DTYPE=bf16 or fp32 before starting to override.
if "%KEV_DTYPE%"=="" (
  "%~dp0python\python.exe" "%~dp0pick-dtype.py" > "%TEMP%\kev-dtype.txt"
  set /p KEV_DTYPE=<"%TEMP%\kev-dtype.txt"
)
if "%KEV_DTYPE%"=="" set KEV_DTYPE=fp32
echo precision: %KEV_DTYPE%
rem torch reads head.pt with its unpickler: check it (and that no other pickle or code file was added) before every start
"%~dp0python\python.exe" "%~dp0kev-models.py" verify --quick || goto :err
rem only callers that hold this key may use Kev (kimeru reads the same file); made once per Windows user
if not "%KEV_API_KEY%"=="" goto :serve
set "KEV_KEY_FILE=%LOCALAPPDATA%\kev\api-key"
if not exist "%KEV_KEY_FILE%" "%~dp0python\python.exe" -c "import os,secrets,sys; p=sys.argv[1]; os.makedirs(os.path.dirname(p), exist_ok=True); open(p, 'x').write(secrets.token_urlsafe(32))" "%KEV_KEY_FILE%" || goto :err
set /p KEV_API_KEY=<"%KEV_KEY_FILE%"
if "%KEV_API_KEY%"=="" goto :err
:serve
echo Starting %MODEL% on http://127.0.0.1:8009 (callers need the key in %%LOCALAPPDATA%%\kev\api-key; first load takes a few minutes; keep this window open)
"%~dp0python\python.exe" -m kev.serve --run "%~dp0models\%MODEL%" --port 8009
goto :eof
:err
echo Kev could not start. See the message above.
pause
