@echo off
rem One-time setup on the Windows lab PC (Python 3.13 from python.org).
rem Creates .venv in the repo and installs qumus-lab with the lab extras (OpenCV, numpy, pythonnet).
cd /d "%~dp0\.."
if not exist .venv (
    py -3.13 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -e ".[lab,dev]"
echo.
echo Done. Next: scripts\probe.bat ports
