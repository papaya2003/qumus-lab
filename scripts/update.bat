@echo off
rem Update this PC in one step: git pull main, self-check, restart the running services.
rem Refuses to restart while a module is busy, locked, heating, or someone holds control.
rem Options: --no-restart (pull and check only), --no-pull (restart only).
cd /d "%~dp0\.."
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
    echo No .venv yet. Run scripts\setup.bat first.
    exit /b 1
)
rem One line on purpose: git pull may rewrite this file while it runs.
.venv\Scripts\python.exe -m qumus_lab.update %* & exit /b
