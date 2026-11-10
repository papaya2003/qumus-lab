@echo off
rem L6 scripted Hall bar (no AI) over HTTP. Start the demo first: scripts\start-demo.bat --demo-reset --demo-speed 50
rem Another computer: pass the tunnel ends, e.g. --exfoliation http://127.0.0.1:18781 --stacking http://127.0.0.1:18782
cd /d "%~dp0\.."
set PYTHONUTF8=1
.venv\Scripts\python.exe -m qumus_lab.hallbar_script %*
