@echo off
setlocal EnableExtensions
title FPS Audio Converter - installer
cd /d "%~dp0"

echo == Looking for Python 3.10+ ==
set "PYEXE="
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PYEXE py -%%V -c "import sys" >nul 2>&1 && set "PYEXE=py -%%V"
)
if not defined PYEXE python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
    echo Python 3.10 or newer was not found. Install it with:
    echo     winget install --id Python.Python.3.12 -e
    echo then close this window, open a new one and run Install.bat again.
    pause
    exit /b 1
)
echo using: %PYEXE%

echo == Creating .venv ==
if not exist ".venv\Scripts\python.exe" %PYEXE% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo pip install failed. & pause & exit /b 1 )
echo == deezy (optional: DDP Atmos output) ==
".venv\Scripts\python.exe" -m pip install deezy || echo deezy could not be installed - DD / DDP still work, DDP Atmos will not.

echo == ffmpeg ==
where ffmpeg >nul 2>&1 && (echo ffmpeg already on PATH) || (
    where winget >nul 2>&1 && winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements --silent || echo Install ffmpeg manually: https://www.gyan.dev/ffmpeg/builds/
)

echo == mediainfo (detects Dolby Atmos for the audio-encode task) ==
where mediainfo >nul 2>&1 && (echo mediainfo already on PATH) || (
    where winget >nul 2>&1 && winget install --id MediaArea.MediaInfo.CLI -e --accept-package-agreements --accept-source-agreements --silent || echo Install MediaInfo CLI manually: https://mediaarea.net/en/MediaInfo/Download/Windows
)

echo.
echo == Dolby Encoding Engine (AC-3 / E-AC-3 / TrueHD fps jobs, and every audio-encode job) ==
echo DEE is proprietary and is NOT installed by this script. If you have it:
echo   1. run:  ".venv\Scripts\python.exe" -m deew      (creates deew's config.toml)
echo   2. put the path to dee.exe in that config, plus ffmpeg/ffprobe paths.
echo.
echo == DDP Atmos (audio-encode task, TrueHD Atmos sources) ==
echo   3. DEE must be 5.2.0 or 5.2.1 for Atmos.
echo   4. get truehdd from https://github.com/truehdd/truehdd and put it on PATH.
echo   5. run:  ".venv\Scripts\deezy.exe" config generate   and set dee / ffmpeg / truehdd in deezy-conf.toml
echo.
".venv\Scripts\python.exe" -m fpsdee doctor
echo.
echo Done. Open a NEW terminal if ffmpeg was just installed, then double-click "Run FPS Converter.bat".
pause
