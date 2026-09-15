@echo off
setlocal EnableExtensions
title fpsaudio
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"

cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo fpsaudio is not installed yet - .venv is missing.
    echo.
    echo Run Install.bat first.
    echo.
    pause
    exit /b 1
)

REM No arguments launches the TUI. Any arguments are passed straight through to
REM the CLI, so this .bat is also a working command-line entry point:
REM     "Run FPS Audio.bat" doctor
REM     "Run FPS Audio.bat" convert D:\in -o D:\out --preset 23.976_to_25
if "%~1"=="" (
    call "%VENV_PY%" -m fpsaudio tui
) else (
    call "%VENV_PY%" -m fpsaudio %*
)
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    REM Exit codes are meaningful: 3 = refused, 2 = verification failed.
    if "%RC%"=="3" (
        echo fpsaudio REFUSED an operation rather than losing information silently.
        echo The message above says what would have been lost and how to proceed.
    ) else if "%RC%"=="2" (
        echo A verification check FAILED. The output exists but did not meet the
        echo acceptance criteria - see the check that failed above.
    ) else (
        echo fpsaudio exited with code %RC%.
    )
    echo.
    pause
)
exit /b %RC%
