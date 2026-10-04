@echo off
rem Start the iPad remote desktop service. Keep this window open (it can be minimized).
rem If the service crashes it is restarted automatically; the stop script ends it for good.
rem Uses the bundled runtime\python.exe (portable package) when present, otherwise python on PATH.
cd /d "%~dp0"
set "PY=python"
if exist "runtime\python.exe" set "PY=runtime\python.exe"
if exist data\stop.flag del data\stop.flag
:loop
"%PY%" app\main.py
set RC=%errorlevel%
if exist data\stop.flag goto end
if "%RC%"=="0" goto end
if "%RC%"=="2" goto failed
echo.
echo Service exited unexpectedly (code %RC%), restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
:failed
echo.
echo Startup failed. See dataun.log
pause
:end
if exist data\stop.flag del data\stop.flag
