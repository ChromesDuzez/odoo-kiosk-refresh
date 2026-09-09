@echo off
setlocal
title Stop Customer Display Agent
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"

rem The agent runs under pythonw.exe with no window, so this is the way to end
rem it -- alt+F4 on the Chrome window only closes the browser, and the agent
rem puts it straight back a few seconds later.
%PY% display_agent.py stop

echo.
pause
