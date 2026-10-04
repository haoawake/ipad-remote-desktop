@echo off
cd /d "%~dp0"
set "PY=python"
if exist "runtime\python.exe" set "PY=runtime\python.exe"
"%PY%" app\main.py --new-password
pause
