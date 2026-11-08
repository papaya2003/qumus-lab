@echo off
rem Start both module services (8781 exfoliation, 8782 stacking) and the debug page (8780) on REAL instruments.
rem Connecting only queries the instruments (no motion, no homing). Close with Ctrl+C = stop axes, heat off.
cd /d "%~dp0\.."
set PYTHONUTF8=1
start "" http://127.0.0.1:8780/
.venv\Scripts\python.exe -m qumus_lab.launcher %*
