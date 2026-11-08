@echo off
rem Same services and debug page with SIMULATED devices (safe on any PC).
cd /d "%~dp0\.."
set PYTHONUTF8=1
start "" http://127.0.0.1:8780/
.venv\Scripts\python.exe -m qumus_lab.launcher --demo %*
