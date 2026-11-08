@echo off
rem Lab PC self-check: Python, packages, config, vendor paths, COM ports Windows sees,
rem runs folder, service ports, and a demo start of both modules + debug page.
rem Opens no instrument port and moves nothing. Report: runs\real-lab\selfcheck\
cd /d "%~dp0\.."
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
    echo No .venv yet. Run scripts\setup.bat first.
    exit /b 1
)
.venv\Scripts\python.exe -m qumus_lab.selfcheck %*
