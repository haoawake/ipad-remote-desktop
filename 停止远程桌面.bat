@echo off
cd /d "%~dp0"
set "PY=python"
if exist "runtime\python.exe" set "PY=runtime\python.exe"
"%PY%" app\main.py --stop
timeout /t 4 >nul
