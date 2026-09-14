@echo off
REM Run FPSConv from source on Windows (developers). End users: install FPSConv-Setup-x.y.z.exe from Releases.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv || python -m venv .venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if "%~1"=="" ( ".venv\Scripts\python.exe" -m fpsconv gui ) else ( ".venv\Scripts\python.exe" -m fpsconv %* )
