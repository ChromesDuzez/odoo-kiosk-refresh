@echo off
setlocal
title Change Customer Display URL
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"

%PY% refresh.py set
if errorlevel 1 goto failed

echo.
pause
exit /b 0

:failed
echo.
echo Something went wrong -- the message above says what.
echo.
pause
exit /b 1
