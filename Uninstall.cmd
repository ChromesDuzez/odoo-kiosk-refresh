@echo off
setlocal
title Uninstall Customer Display Agent
cd /d "%~dp0"

rem See the note in "Install Agent.cmd": the execution policy set here is
rem scoped to this one PowerShell process, not the machine.
rem
rem Pass -WhatIf to see what it would do without changing anything, or
rem -Purge to also delete the settings and the pairing code:
rem   Uninstall.cmd -WhatIf
rem   Uninstall.cmd -Purge
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*

echo.
pause
exit /b %errorlevel%
