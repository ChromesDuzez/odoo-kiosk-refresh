@echo off
setlocal
title Check Customer Display Setup
cd /d "%~dp0"

rem Read-only. Changes nothing, starts nothing, stops nothing.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-setup.ps1"

echo.
pause
