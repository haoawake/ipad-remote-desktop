@echo off
cd /d "%~dp0"
if exist "runtime\python.exe" goto bundled
python -m pip install -r requirements.txt
pause
exit /b 0
:bundled
echo This portable package already includes Python and all dependencies.
echo Nothing to install - just run the start script.
pause
