@echo off
REM Local Windows build: same steps as the GitHub workflow. Needs Inno Setup 6 installed.
cd /d "%~dp0"
set VERSION=%1
if "%VERSION%"=="" set VERSION=0.0.0
py -3 -m venv .venv-build
".venv-build\Scripts\python.exe" -m pip install -q -r requirements-build.txt
echo __version__ = "%VERSION%"> fpsconv\_version.py
".venv-build\Scripts\pyinstaller.exe" --noconfirm --clean --distpath dist --workpath build packaging\FPSConv.spec || exit /b 1
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /DAppVersion=%VERSION% packaging\installer.iss || exit /b 1
echo Installer: dist\FPSConv-Setup-%VERSION%.exe
