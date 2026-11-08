@echo off
rem L1-L2 probe for both modules. Examples:
rem   scripts\probe.bat ports
rem   scripts\probe.bat read all
rem   scripts\probe.bat l1
rem   scripts\probe.bat jog arm_left z 0.5
rem   scripts\probe.bat switch exfoliation_vacuum on
rem   scripts\probe.bat heat exfoliation_heater 30 --timeout 300
rem   scripts\probe.bat l2
rem   scripts\probe.bat --demo l2        (simulated devices)
rem Ctrl+C at any time = stop axes, heat off. Logs: runs\real-lab\probe\
cd /d "%~dp0\.."
set PYTHONUTF8=1
.venv\Scripts\python.exe -m qumus_lab.probe.cli %*
