@echo off
rem Shows which precision start-kev.cmd will pick on this PC (bf16 only with native CPU support).
"%~dp0python\python.exe" "%~dp0pick-dtype.py"
