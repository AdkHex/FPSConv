@echo off
setlocal EnableExtensions
title FPS Audio Converter
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo .venv is missing - run Install.bat first.
    pause
    exit /b 1
)
REM No arguments = GUI. Arguments are passed to the CLI:
REM   "Run FPS Converter.bat" doctor
REM   "Run FPS Converter.bat" convert D:\in -o D:\out --mode 23.976-25 -j 2
if "%~1"=="" (
    "%PY%" -m fpsdee gui
) else (
    "%PY%" -m fpsdee %*
)
if not "%errorlevel%"=="0" pause
