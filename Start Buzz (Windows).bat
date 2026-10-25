@echo off
rem Double-click to start the phone buzzer server.
rem Ctrl+C or closing the window stops the server - all held keys are released.
rem The first start sets everything up (needs Python 3.9 or newer).
setlocal
cd /d "%~dp0"
title Buzz! Server
set "VPY=%~dp0.venv\Scripts\python.exe"

rem 1. Set up by an earlier start?
if exist "%VPY%" (
    "%VPY%" -c "import aiohttp, pynput" >nul 2>&1
    if not errorlevel 1 goto run
)

rem 2. First start: find Python 3.9+, create .venv and install the requirements
set "BASE="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "BASE=py -3"
if not defined BASE (
    python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
    if not errorlevel 1 set "BASE=python"
)
if not defined BASE (
    echo Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer. Then double-click this file again.
    echo.
    pause
    exit /b 1
)
echo First start: setting things up, this takes about a minute ...
%BASE% -m venv --clear .venv
if errorlevel 1 goto setup_failed
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
if errorlevel 1 goto setup_failed
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto setup_failed

:run
"%VPY%" buzz.py %*
if errorlevel 1 pause
exit /b

:setup_failed
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
