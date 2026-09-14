"""Persistent settings and job history.

Stored per user, outside the app folder, so an upgrade never wipes them:

* Windows : %APPDATA%\\FPSConv\\settings.json / history.json
* macOS   : ~/Library/Application Support/FPSConv/
* Linux   : ~/.config/FPSConv/
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "out_dir": "",
    "conv_type": "23.976-25",
    "mode": "single",          # single | batch
    "workers": 2,
    "bitrate": 0,              # 0 = use the source bitrate (fps.py behaviour)
    "overwrite": "overwrite",  # overwrite | skip | rename
    "auto_update": True,       # install new releases automatically when the queue is idle
    "native_window": True,     # open in a desktop window (pywebview) instead of the browser
    "tools": {"ffmpeg": "", "ffprobe": "", "deew_python": ""},
}

_lock = threading.Lock()


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "FPSConv"


def _read(name: str, default: Any) -> Any:
    path = config_dir() / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, payload: Any) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{name}.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, directory / name)


def load_settings() -> dict[str, Any]:
    with _lock:
        data = _read("settings.json", {})
    merged = json.loads(json.dumps(DEFAULTS))
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "tools" and isinstance(value, dict):
                merged["tools"].update({k: str(v or "") for k, v in value.items()})
            elif key in merged:
                merged[key] = value
    return merged


def save_settings(update: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    for key, value in update.items():
        if key == "tools" and isinstance(value, dict):
            current["tools"].update({k: str(v or "").strip() for k, v in value.items()})
        elif key in DEFAULTS:
            current[key] = value
    with _lock:
        _write("settings.json", current)
    return current


def load_history() -> list[dict[str, Any]]:
    with _lock:
        data = _read("history.json", [])
    return data if isinstance(data, list) else []


def save_history(entries: list[dict[str, Any]], keep: int = 200) -> None:
    with _lock:
        _write("history.json", entries[-keep:])
