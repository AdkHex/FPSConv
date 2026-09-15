@echo off
setlocal EnableExtensions
title fpsaudio installer
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
cls

echo ============================================
echo   fpsaudio - installer
echo ============================================
echo.
echo This installs Python packages and the external tools
echo fpsaudio needs, then runs "fpsaudio doctor" to show you
echo exactly what is present and what is missing.
echo.

cd /d "%~dp0"

REM Prefer PowerShell 7 (pwsh) when it is present, and fall back to the
REM Windows PowerShell that ships with the OS. The installer needs 5.0+ for
REM Get-FileHash and Expand-Archive, and it checks that itself and says so.
set "PS_EXE="
where pwsh >nul 2>&1 && set "PS_EXE=pwsh"
if not defined PS_EXE (
    if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
        REM Use the full path rather than whatever "powershell" resolves to on
        REM PATH, so an old 2.0 engine cannot be picked up by accident.
        set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
)
if not defined PS_EXE (
    where powershell >nul 2>&1 && set "PS_EXE=powershell"
)

if not defined PS_EXE (
    echo ERROR: PowerShell was not found on this system.
    echo.
    echo fpsaudio's installer is a PowerShell script. Windows 10 and 11 include
    echo PowerShell by default at:
    echo   %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
    echo.
    pause
    exit /b 1
)

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo Done. Start the app with "Run FPS Audio.bat".
) else (
    echo Installation did not complete ^(exit code %RC%^).
    echo.
    echo Scroll up: the last message printed says exactly what stopped it.
    echo Common causes:
    echo   - Python 3.11 or newer is not installed
    echo   - PowerShell is older than 5.0
    echo   - pip could not reach the network
    echo.
    echo You can re-run this installer safely; it picks up where it left off.
)
echo.
pause
exit /b %RC%
