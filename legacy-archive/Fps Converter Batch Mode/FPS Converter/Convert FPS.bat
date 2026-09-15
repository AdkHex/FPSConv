@echo off
setlocal EnableExtensions
title FPS Audio Batch Converter (GUI)
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
cls

echo ============================================
echo   FPS Audio Batch Converter (GUI) by Ionicboy
echo ============================================
echo.

set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

set "VENV_DIR=%APP_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"
set "PY_CMD="

echo [1/6] Checking Python...
call :set_python
if errorlevel 1 goto :python_error
echo Using Python via: %PY_CMD%

echo [2/6] Creating local virtual environment (if needed)...
if exist "%VENV_PY%" goto :venv_ready

call %PY_CMD% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo WARNING: Failed to create virtual environment using %PY_CMD%.
    if /I "%PY_CMD%"=="py -3" (
        call :try_python_fallback_venv
        if errorlevel 1 goto :venv_error
    ) else (
        goto :venv_error
    )
)

:venv_ready
echo [3/6] Upgrading pip...
call "%VENV_PY%" -m pip install --upgrade pip >nul
if errorlevel 1 echo WARNING: Could not upgrade pip. Continuing...

echo [4/6] Installing Python dependencies...
if exist "%APP_DIR%requirements.txt" (
    call "%VENV_PIP%" install -r "%APP_DIR%requirements.txt"
    if errorlevel 1 goto :deps_error
) else (
    echo WARNING: requirements.txt not found. Skipping dependency install.
)

echo [5/6] Checking ffmpeg / ffprobe...
where ffmpeg >nul 2>&1
if errorlevel 1 goto :ffmpeg_error
where ffprobe >nul 2>&1
if errorlevel 1 goto :ffprobe_error

echo [6/6] Starting GUI app...
echo.
call "%VENV_PY%" "%APP_DIR%converter.py"
set "APP_EXIT=%errorlevel%"
echo.
if not "%APP_EXIT%"=="0" echo Application exited with code %APP_EXIT%.
pause
exit /b %APP_EXIT%

:set_python
where py >nul 2>&1
if errorlevel 1 goto :check_python_exe
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    exit /b 0
)

:check_python_exe
where python >nul 2>&1
if errorlevel 1 exit /b 1
python -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY_CMD=python"
exit /b 0

:try_python_fallback_venv
where python >nul 2>&1
if errorlevel 1 exit /b 1
python -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY_CMD=python"
call python -m venv "%VENV_DIR%"
if errorlevel 1 exit /b 1
exit /b 0

:python_error
echo ERROR: Python 3 was not found, or the launcher points to a broken install.
echo.
echo Try these in Command Prompt:
echo   py -0p
echo   python --version
echo   where python
echo.
echo Install/repair Python 3 and enable PATH + launcher (py).
echo https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:venv_error
echo ERROR: Failed to create virtual environment.
echo Your Python launcher may be pointing to an old/deleted Python install.
echo.
echo Try these in Command Prompt:
echo   py -0p
echo   python --version
echo   where python
echo.
pause
exit /b 1

:deps_error
echo ERROR: Failed to install Python dependencies.
echo.
pause
exit /b 1

:ffmpeg_error
echo.
echo ERROR: ffmpeg was not found in PATH.
echo This app requires ffmpeg and ffprobe.
where winget >nul 2>&1
if not errorlevel 1 (
    echo.
    echo Tip: Install FFmpeg with:
    echo   winget install --id Gyan.FFmpeg -e
) else (
    echo.
    echo Install FFmpeg manually and add it to PATH:
    echo   https://ffmpeg.org/download.html
)
echo.
pause
exit /b 1

:ffprobe_error
echo.
echo ERROR: ffprobe was not found in PATH.
echo Your FFmpeg installation may be incomplete or not in PATH.
echo.
pause
exit /b 1
