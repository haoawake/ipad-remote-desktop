@echo off
cd /d "%~dp0"
python app\main.py --stop
timeout /t 4 >nul
