# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one folder with two executables.

* FPSConv.exe      — windowed (no console); opens the desktop window
* fpsconv-cli.exe  — console build of the same program for `convert`, `doctor`, `dee`

deew and its dependencies are collected so the installed app can run
`FPSConv.exe deew …` without a Python install on the machine.
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(ROOT, "fpsconv", "static"), os.path.join("fpsconv", "static"))]
binaries = []
hiddenimports = ["fpsconv", "fpsconv.server", "fpsconv.window", "fpsconv.updater"]

for pkg in ("deew", "rich", "toml", "platformdirs", "xmltodict", "unidecode", "packaging", "requests"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:  # package not installed in the build env
        pass
try:
    hiddenimports += collect_submodules("webview")
    d, b, h = collect_all("webview")
    datas += d; binaries += b; hiddenimports += h
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

ICON = os.path.join(ROOT, "assets", "icon.ico")

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FPSConv",
    icon=ICON,
    console=False,
    disable_windowed_traceback=True,   # never show PyInstaller's "Unhandled exception" dialog
    upx=False,
)
exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="fpsconv-cli",
    icon=ICON,
    console=True,
    upx=False,
)
coll = COLLECT(
    exe_gui, exe_cli, a.binaries, a.datas,
    strip=False, upx=False, name="FPSConv",
)
