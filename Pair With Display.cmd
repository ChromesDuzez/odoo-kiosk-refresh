@echo off
setlocal
title Pair With Customer Display
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"

echo.
echo  Look at the customer display screen. It is showing an address
echo  and a 12-character pairing code. Type them in below.
echo.

%PY% refresh.py pair
if errorlevel 1 goto failed

echo.
pause
exit /b 0

:failed
echo.
echo Pairing did not complete -- the message above says why.
echo.
pause
exit /b 1
