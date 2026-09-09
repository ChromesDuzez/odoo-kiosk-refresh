@echo off
setlocal
title Install Customer Display Agent
cd /d "%~dp0"

rem -ExecutionPolicy Bypass on this command line applies to THIS PowerShell
rem process and nothing else. It writes nothing to the registry and leaves the
rem machine's policy alone -- it is the launch-argument equivalent of
rem `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, minus the
rem need to type it first.
rem
rem The script asks for Administrator rights itself, so there is no need to
rem "Run as administrator" this file. Any arguments here are passed through,
rem e.g.  "Install Agent.cmd" -Port 9000
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-agent.ps1" %*

echo.
pause
exit /b %errorlevel%
