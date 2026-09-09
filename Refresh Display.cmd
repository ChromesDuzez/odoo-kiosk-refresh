@echo off
setlocal
title Refresh Customer Display
cd /d "%~dp0"

rem The py launcher is the reliable way to pick Python 3 on Windows; fall
rem back to plain python for installs that skipped it.
set "PY=py -3"
where py >nul 2>nul || set "PY=python"

%PY% refresh.py
if errorlevel 1 goto failed

rem Held briefly so staff see the confirmation, then it closes itself.
timeout /t 4 >nul
exit /b 0

:failed
echo.
echo Something went wrong -- the message above says what.
echo.
pause
exit /b 1
