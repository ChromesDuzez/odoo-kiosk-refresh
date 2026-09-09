@echo off
setlocal
title Stop The Customer Display
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"

echo.
echo  This stops the agent on the display laptop. The screen goes blank
echo  and STAYS blank until someone starts it again over there.
echo.
echo  For a display that is just misbehaving, use Refresh Display instead.
echo.

set /p "ANSWER=Type YES to stop it: "
if /i not "%ANSWER%"=="YES" (
    echo.
    echo Cancelled -- nothing was stopped.
    echo.
    pause
    exit /b 1
)

echo.
%PY% refresh.py stop

echo.
pause
