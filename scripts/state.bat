@echo off
rem Show the lab state tables in runs\real-lab\qumus.db (devices, samples, events, zones).
rem   scripts\state.bat devices
rem   scripts\state.bat events --limit 30
rem   scripts\state.bat sample-set chip_001 blank_chip left_board.L3
cd /d "%~dp0\.."
set PYTHONUTF8=1
.venv\Scripts\python.exe -m qumus_lab.state %*
